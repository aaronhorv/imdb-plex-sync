"""Playwright-based IMDb list scraping."""

from __future__ import annotations

import csv
import io
import re
import time
from http.cookies import SimpleCookie
from typing import Any, Callable
from urllib.parse import urljoin

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


IMDB_ID_RE = re.compile(r"tt\d+")
TITLE_PATH_RE = re.compile(r"/title/(tt\d+)")
MAX_SCROLL_ATTEMPTS = 80
MAX_STAGNANT_SCROLLS = 6
MAX_PAGINATION_PAGES = 250
DOWNLOAD_TIMEOUT_MS = 120_000

ID_COLUMNS = ("Const", "IMDb ID", "Title ID", "URL")
TITLE_COLUMNS = ("Title", "Original Title", "Title Name")


class ImdbExportUnavailable(RuntimeError):
    """Raised when IMDb CSV export is not available from the rendered page."""


class ImdbCsvParseError(RuntimeError):
    """Raised when a downloaded IMDb CSV cannot be parsed into valid IDs."""


class ImdbAccessBlockedError(RuntimeError):
    """Raised when IMDb shows an interstitial instead of the requested list."""


def scrape_imdb_watchlist(
    list_url: str,
    imdb_cookie: str = "",
    logger=None,
) -> list[dict]:
    """Scrape an IMDb watchlist or custom list URL.

    Preferred order:
      1. Official IMDb CSV export via Playwright download handling.
      2. Paginated rendered DOM extraction.
      3. Scrolling rendered DOM plus structured response extraction.

    Args:
        list_url: IMDb watchlist or list URL to scrape.
        imdb_cookie: Optional IMDb ``at-main`` value or full Cookie header.
        logger: Optional callable logger. Supports ``logger(message)`` and
            ``logger(message, level)`` styles.

    Returns:
        A stable list of dictionaries containing ``title``, ``imdb_id``, and
        ``link`` keys.
    """

    if not list_url:
        return []

    structured_items: list[dict[str, str]] = []

    def handle_response(response) -> None:
        status = response.status
        url = response.url.lower()
        content_type = response.headers.get("content-type", "").lower()

        if status == 202 and "imdb" in url:
            _log(logger, f"IMDb returned HTTP 202 while preparing content: {response.url}", "info")

        if "graphql" not in url and "json" not in content_type:
            return

        try:
            data = response.json()
        except Exception:
            return

        before = len(structured_items)
        structured_items.extend(_extract_structured_items(data))
        found = len(structured_items) - before
        if found:
            _log(logger, f"IMDb structured response yielded {found} title candidates", "info")

    _log(logger, "Opening IMDb page with Playwright", "info")

    browser = None
    context = None
    page = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                accept_downloads=True,
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )

            imdb_cookies = parse_imdb_cookie_string(imdb_cookie)
            if imdb_cookies:
                context.add_cookies(imdb_cookies)
                _log(logger, f"IMDb cookie authentication enabled ({len(imdb_cookies)} cookies)", "info")

            page = context.new_page()
            page.set_default_timeout(15_000)
            page.on("response", handle_response)

            page.goto(list_url, wait_until="domcontentloaded", timeout=60_000)
            _wait_for_page_settle(page, logger)
            _log_page_diagnostics(page, logger)
            _raise_for_blocking_interstitial(page, logger)

            try:
                export_items, export_mode = _scrape_via_csv_export(page, logger)
                if export_items:
                    _log(
                        logger,
                        f"IMDb CSV export successful via {export_mode}: {len(export_items)} valid items",
                        "success",
                    )
                    return export_items
                _log(logger, "IMDb CSV export returned no valid items; using paginated extraction", "warning")
            except Exception as exc:
                _log(logger, f"IMDb CSV export unavailable or failed: {exc}", "warning")

            try:
                paginated_items = _scrape_paginated_dom(page, logger)
                if paginated_items:
                    _log(logger, f"IMDb paginated extraction successful: {len(paginated_items)} items", "success")
                    return paginated_items
                _log(logger, "IMDb paginated extraction returned no items; using scrolling fallback", "warning")
            except Exception as exc:
                _log(logger, f"IMDb paginated extraction failed: {exc}", "warning")

            scroll_items = _scrape_scrolling_dom(page, structured_items, logger)
            _log(logger, f"IMDb scrape completed with {len(scroll_items)} items", "success")
            return scroll_items

    except Exception as exc:
        _log(logger, f"IMDb Playwright scraping error: {exc}", "error")
        raise
    finally:
        for resource in (page, context, browser):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception:
                pass


def parse_imdb_csv(csv_text: str, logger=None) -> list[dict]:
    """Parse IMDb CSV export text into the app's internal item format."""
    if not csv_text.strip():
        raise ImdbCsvParseError("CSV was empty")

    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    headers = reader.fieldnames or []
    _log(logger, f"IMDb CSV headers: {headers}", "info")

    if not headers:
        raise ImdbCsvParseError("CSV had no headers")

    items: list[dict] = []
    seen_ids: set[str] = set()
    parsed_rows = 0

    for row in reader:
        parsed_rows += 1
        imdb_id = _extract_csv_imdb_id(row)
        if not imdb_id or imdb_id in seen_ids:
            continue

        title = _extract_csv_title(row) or f"IMDB:{imdb_id}"
        seen_ids.add(imdb_id)
        items.append(_build_item(imdb_id, title))

    _log(logger, f"IMDb CSV parsed rows: {parsed_rows}", "info")
    _log(logger, f"IMDb CSV valid IMDb IDs: {len(items)}", "info")

    if not items:
        raise ImdbCsvParseError(f"CSV contained no valid IMDb IDs; headers were {headers}")

    return items


def parse_imdb_cookie_string(cookie_value: str) -> list[dict[str, Any]]:
    """Parse an IMDb at-main value or full Cookie header into Playwright cookies."""
    cookie_value = (cookie_value or "").strip()
    if not cookie_value:
        return []

    if cookie_value.lower().startswith("cookie:"):
        cookie_value = cookie_value.split(":", 1)[1].strip()

    parsed: dict[str, str] = {}

    if "=" not in cookie_value:
        parsed["at-main"] = cookie_value
    else:
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_value)
            parsed = {key: morsel.value for key, morsel in cookie.items() if morsel.value}
        except Exception:
            for part in cookie_value.split(";"):
                if "=" not in part:
                    continue
                name, value = part.split("=", 1)
                name = name.strip()
                value = value.strip()
                if name and value:
                    parsed[name] = value

    return [
        {
            "name": name,
            "value": value,
            "domain": ".imdb.com",
            "path": "/",
            "secure": True,
            "httpOnly": name in {"at-main", "session-id", "ubid-main"},
            "sameSite": "Lax",
        }
        for name, value in parsed.items()
    ]


def _scrape_via_csv_export(page: Page, logger) -> tuple[list[dict], str]:
    export_control = _find_export_control(page, logger)
    if export_control is None:
        _log(logger, "IMDb Export control found: no", "info")
        raise ImdbExportUnavailable("Export control was not found")

    _log(logger, "IMDb Export control found: yes", "info")

    try:
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
            export_control.click(timeout=10_000)
        download = download_info.value
        _log(logger, "IMDb CSV download started: yes", "info")
        filename = download.suggested_filename or "imdb-export.csv"
        _log(logger, f"IMDb CSV filename: {filename}", "info")
        csv_text = _read_download_text(download)
        return parse_imdb_csv(csv_text, logger), "direct download"
    except PlaywrightTimeoutError:
        _log(logger, "IMDb CSV download started: no", "warning")

    generated_download = _wait_for_generated_download_link(page, logger)
    if generated_download is None:
        raise ImdbExportUnavailable("Export did not produce a direct download or generated download link")

    with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
        generated_download.click(timeout=10_000)
    download = download_info.value
    filename = download.suggested_filename or "imdb-export.csv"
    _log(logger, "IMDb CSV download started from generated link: yes", "info")
    _log(logger, f"IMDb CSV filename: {filename}", "info")
    csv_text = _read_download_text(download)
    return parse_imdb_csv(csv_text, logger), "generated download link"


def _find_export_control(page: Page, logger):
    selectors = [
        'a[download]',
        'a[href*="export" i]',
        'button[aria-label*="Export" i]',
        'a[aria-label*="Export" i]',
        '[role="button"][aria-label*="Export" i]',
        '[role="menuitem"][aria-label*="Export" i]',
        'button:has-text("Export")',
        'a:has-text("Export")',
        '[role="button"]:has-text("Export")',
        '[role="menuitem"]:has-text("Export")',
    ]

    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:
            continue
        if count:
            _log(logger, f"IMDb Export selector matched: {selector}", "info")
            return locator.nth(0)

    menu_selectors = [
        'button[aria-label*="More" i]',
        'button[aria-label*="Menu" i]',
        '[role="button"][aria-label*="More" i]',
        '[role="button"]:has-text("More")',
        'button:has-text("More")',
    ]

    for selector in menu_selectors:
        menu = page.locator(selector)
        try:
            count = menu.count()
        except Exception:
            continue
        if not count:
            continue
        try:
            menu.nth(0).click(timeout=5_000)
            page.wait_for_timeout(500)
        except Exception:
            continue
        for export_selector in selectors[2:]:
            export = page.locator(export_selector)
            try:
                if export.count():
                    _log(logger, f"IMDb Export control found after opening menu: {selector}", "info")
                    return export.nth(0)
            except Exception:
                continue

    return None


def _wait_for_generated_download_link(page: Page, logger):
    _log(logger, "Waiting for generated IMDb export download link", "info")
    deadline = time.monotonic() + 90
    selectors = [
        'a[download]',
        'a[href*="export" i]',
        'a[href*="csv" i]',
        'a:has-text("Download")',
        'button:has-text("Download")',
    ]

    while time.monotonic() < deadline:
        _raise_for_blocking_interstitial(page, logger)
        for selector in selectors:
            locator = page.locator(selector)
            try:
                if locator.count():
                    _log(logger, f"IMDb generated download link found with selector: {selector}", "info")
                    return locator.nth(0)
            except Exception:
                continue
        page.wait_for_timeout(2_000)

    _log(logger, "IMDb generated download link was not found before timeout", "warning")
    return None


def _read_download_text(download) -> str:
    path = download.path()
    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        return file.read()


def _scrape_paginated_dom(page: Page, logger) -> list[dict]:
    all_items: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for page_number in range(1, MAX_PAGINATION_PAGES + 1):
        _wait_for_initial_titles(page, logger)
        page_items = _collect_dom_items(page)
        new_count = 0
        for item in page_items:
            imdb_id = item.get("imdb_id", "")
            if not IMDB_ID_RE.fullmatch(imdb_id) or imdb_id in seen_ids:
                continue
            seen_ids.add(imdb_id)
            all_items.append(item)
            new_count += 1

        _log(logger, f"IMDb pagination page {page_number}: collected {new_count} new titles", "info")

        next_button = _find_next_control(page)
        _log(logger, f"IMDb pagination page {page_number}: Next button found: {bool(next_button)}", "info")
        if next_button is None:
            break

        previous_url = page.url
        previous_first = _first_title_id(page)
        if not _go_to_next_page(page, next_button, logger):
            break

        _wait_for_pagination_change(page, previous_url, previous_first, logger)

    return _merge_items(all_items, [])


def _find_next_control(page: Page):
    selectors = [
        'a[rel="next"]',
        'a[aria-label*="Next" i]',
        'button[aria-label*="Next" i]',
        '[role="button"][aria-label*="Next" i]',
        '[role="link"][aria-label*="Next" i]',
        'a:has-text("Next page")',
        'button:has-text("Next page")',
        '[role="link"]:has-text("Next page")',
        '[role="button"]:has-text("Next page")',
        'a:has-text("Next")',
        'button:has-text("Next")',
        '[role="link"]:has-text("Next")',
        '[role="button"]:has-text("Next")',
    ]

    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            if _is_enabled_next_candidate(candidate):
                return candidate

    controls = page.locator('a, button, [role="button"], [role="link"]')
    try:
        count = controls.count()
    except Exception:
        return None

    for index in range(count):
        candidate = controls.nth(index)
        try:
            label = " ".join(
                part
                for part in (
                    candidate.get_attribute("aria-label", timeout=500),
                    candidate.get_attribute("title", timeout=500),
                    candidate.inner_text(timeout=500),
                )
                if part
            ).lower()
        except Exception:
            continue

        if "next" in label and _is_enabled_next_candidate(candidate):
            return candidate

    return None


def _is_enabled_next_candidate(candidate) -> bool:
    try:
        if not candidate.is_visible(timeout=1_000):
            return False
        aria_disabled = candidate.get_attribute("aria-disabled", timeout=1_000)
        disabled = candidate.get_attribute("disabled", timeout=1_000)
        class_name = candidate.get_attribute("class", timeout=1_000) or ""
        if aria_disabled == "true" or disabled is not None or "disabled" in class_name.lower():
            return False
        return True
    except Exception:
        return False


def _go_to_next_page(page: Page, next_button, logger) -> bool:
    href = _next_control_href(next_button)
    if href and not href.startswith("#") and not href.lower().startswith("javascript:"):
        next_url = urljoin(page.url, href)
        _log(logger, f"IMDb pagination navigating to next URL: {next_url}", "info")
        try:
            page.goto(next_url, wait_until="domcontentloaded", timeout=60_000)
            _wait_for_page_settle(page, logger)
            return True
        except Exception as exc:
            _log(logger, f"IMDb pagination direct navigation failed: {exc}", "warning")

    try:
        next_button.click(timeout=10_000)
        return True
    except Exception as exc:
        _log(logger, f"IMDb pagination Next click failed: {exc}", "warning")
        return False


def _next_control_href(next_button) -> str:
    try:
        href = next_button.get_attribute("href", timeout=1_000)
        if href:
            return href
    except Exception:
        pass

    try:
        return next_button.evaluate(
            """
            (element) => {
              const anchor = element.closest && element.closest("a[href]");
              return anchor ? anchor.getAttribute("href") : "";
            }
            """
        ) or ""
    except Exception:
        return ""


def _wait_for_pagination_change(page: Page, previous_url: str, previous_first: str, logger) -> None:
    try:
        page.wait_for_function(
            "([oldUrl, oldFirst]) => {"
            "const first = document.querySelector('a[href*=\"/title/tt\"]');"
            "const href = first ? first.getAttribute('href') : '';"
            "return window.location.href !== oldUrl || href !== oldFirst;"
            "}",
            arg=[previous_url, previous_first],
            timeout=15_000,
        )
    except PlaywrightTimeoutError:
        _log(logger, "IMDb pagination did not show a URL or first-title change before timeout", "warning")

    _wait_for_page_settle(page, logger)


def _first_title_id(page: Page) -> str:
    try:
        return page.locator('a[href*="/title/tt"]').nth(0).get_attribute("href", timeout=2_000) or ""
    except Exception:
        return ""


def _scrape_scrolling_dom(page: Page, structured_items: list[dict[str, str]], logger) -> list[dict]:
    dom_items: list[dict[str, str]] = []
    seen_count = 0
    stagnant_scrolls = 0

    for attempt in range(1, MAX_SCROLL_ATTEMPTS + 1):
        current_dom_items = _collect_dom_items(page)
        if current_dom_items:
            dom_items.extend(current_dom_items)

        merged_count = len(_merge_items(dom_items, structured_items))
        if merged_count > seen_count:
            seen_count = merged_count
            stagnant_scrolls = 0
            _log(logger, f"IMDb scrolling fallback collected {seen_count} unique titles", "info")
        else:
            stagnant_scrolls += 1
            _log(
                logger,
                f"IMDb scroll {attempt}: no new titles detected "
                f"({stagnant_scrolls}/{MAX_STAGNANT_SCROLLS})",
                "info",
            )

        if stagnant_scrolls >= MAX_STAGNANT_SCROLLS:
            _log(logger, "IMDb scrolling stopped after repeated empty scrolls", "info")
            break

        previous_height = _get_scroll_height(page)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        _wait_for_scroll_progress(page, previous_height)

    return _merge_items(dom_items, structured_items)


def _extract_csv_imdb_id(row: dict[str, str]) -> str:
    for column in ID_COLUMNS:
        value = _row_value(row, column)
        if value:
            match = IMDB_ID_RE.search(value)
            if match:
                return match.group(0)

    for value in row.values():
        if value:
            match = IMDB_ID_RE.search(str(value))
            if match:
                return match.group(0)

    return ""


def _extract_csv_title(row: dict[str, str]) -> str:
    for column in TITLE_COLUMNS:
        value = _row_value(row, column)
        if value:
            return _clean_title(value)
    return ""


def _row_value(row: dict[str, str], wanted_column: str) -> str:
    wanted = wanted_column.strip().lower()
    for key, value in row.items():
        if key and key.strip().lower() == wanted and value:
            return str(value).strip()
    return ""


def _log(logger: Callable[..., Any] | None, message: str, level: str = "info") -> None:
    if logger is None:
        return
    try:
        logger(message, level)
    except TypeError:
        logger(message)
    except Exception:
        pass


def _log_page_diagnostics(page: Page, logger) -> None:
    try:
        _log(logger, f"IMDb final URL: {page.url}", "info")
    except Exception:
        pass
    try:
        _log(logger, f"IMDb page title: {page.title()}", "info")
    except Exception:
        pass


def _detect_interstitials(page: Page, logger) -> list[str]:
    url = page.url.lower()
    try:
        title = page.title().lower()
    except Exception:
        title = ""

    page_text = ""
    try:
        page_text = page.locator("body").inner_text(timeout=2_000).lower()[:2000]
    except Exception:
        pass

    detected = []
    checks = {
        "login": (
            ("/registration/signin", "/ap/signin", "/signin"),
            ("sign in", "imdb sign in", "login"),
            (),
        ),
        "consent": (
            ("consent",),
            ("consent", "privacy preferences"),
            ("accept cookies", "privacy preferences", "cookie preferences"),
        ),
        "challenge": (
            ("captcha", "challenge"),
            ("captcha", "robot", "challenge", "human verification"),
            ("captcha", "robot", "challenge", "human verification"),
        ),
        "error": (
            (),
            ("404", "not found", "unavailable"),
            ("page not found", "this page could not be found"),
        ),
    }

    for label, (url_patterns, title_patterns, body_patterns) in checks.items():
        if (
            any(pattern in url for pattern in url_patterns)
            or any(pattern in title for pattern in title_patterns)
            or any(pattern in page_text for pattern in body_patterns)
        ):
            _log(logger, f"IMDb {label} page signal detected", "warning")
            detected.append(label)

    return detected


def _raise_for_blocking_interstitial(page: Page, logger) -> None:
    detected = _detect_interstitials(page, logger)
    blocking_labels = [label for label in detected if label in {"login", "consent", "challenge", "error"}]
    if not blocking_labels:
        return

    label_text = ", ".join(blocking_labels)
    if "challenge" in blocking_labels:
        guidance = "IMDb is showing Human Verification. Open IMDb in a normal browser, solve it, then update the IMDb Cookie setting."
    elif "login" in blocking_labels:
        guidance = "IMDb is asking for sign-in. Update the IMDb Cookie setting with fresh logged-in cookies."
    elif "consent" in blocking_labels:
        guidance = "IMDb is showing a consent page. Accept it in a browser, then update the IMDb Cookie setting with fresh cookies."
    else:
        guidance = "IMDb is showing an error page instead of the configured list. Check that the list URL is correct and accessible."

    raise ImdbAccessBlockedError(f"IMDb access blocked by {label_text} page. {guidance}")


def _wait_for_page_settle(page: Page, logger) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except PlaywrightTimeoutError:
        _log(logger, "IMDb network did not become idle; continuing", "info")


def _wait_for_initial_titles(page: Page, logger) -> None:
    try:
        page.wait_for_selector('a[href*="/title/tt"]', timeout=15_000)
    except PlaywrightTimeoutError:
        _log(logger, "IMDb title links were not visible after initial load", "warning")

    _wait_for_page_settle(page, logger)


def _wait_for_scroll_progress(page: Page, previous_height: int) -> None:
    try:
        page.wait_for_function(
            "(height) => document.body.scrollHeight > height",
            arg=previous_height,
            timeout=2_500,
        )
    except PlaywrightTimeoutError:
        pass

    _wait_for_page_settle(page, None)
    page.wait_for_timeout(400)


def _get_scroll_height(page: Page) -> int:
    try:
        return int(page.evaluate("document.body.scrollHeight") or 0)
    except Exception:
        return 0


def _collect_dom_items(page: Page) -> list[dict[str, str]]:
    return page.evaluate(
        """
        () => {
          const titlePattern = /\\/title\\/(tt\\d+)/;
          const cleanTitle = (text) => {
            if (!text) return "";
            return text
              .replace(/\\s+/g, " ")
              .replace(/^\\d+\\.\\s*/, "")
              .replace(/^(Watch options|Trailer|Video|See more).*$/i, "")
              .trim();
          };

          const candidateTitle = (link) => {
            const label = cleanTitle(link.getAttribute("aria-label"));
            if (label && !/^tt\\d+$/.test(label)) return label;

            const linkText = cleanTitle(link.innerText || link.textContent);
            if (linkText && !/^tt\\d+$/.test(linkText)) return linkText;

            const container = link.closest(
              "li, article, [data-testid*='title'], [data-testid*='list'], .ipc-metadata-list-summary-item"
            );
            if (container) {
              const heading = container.querySelector("h1, h2, h3, [role='heading']");
              const headingText = cleanTitle(heading && (heading.innerText || heading.textContent));
              if (headingText && !/^tt\\d+$/.test(headingText)) return headingText;

              const image = container.querySelector("img[alt]");
              const imageText = cleanTitle(image && image.getAttribute("alt"));
              if (imageText && !/^tt\\d+$/.test(imageText)) return imageText;
            }

            const image = link.querySelector("img[alt]");
            const imageText = cleanTitle(image && image.getAttribute("alt"));
            if (imageText && !/^tt\\d+$/.test(imageText)) return imageText;

            return "";
          };

          const items = [];
          for (const link of document.querySelectorAll('a[href*="/title/tt"]')) {
            const href = link.getAttribute("href") || "";
            const match = href.match(titlePattern);
            if (!match) continue;
            items.push({
              imdb_id: match[1],
              title: candidateTitle(link),
            });
          }
          return items;
        }
        """
    )


def _extract_structured_items(data: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            imdb_id = _find_id_in_dict(obj)
            title = _find_title_in_dict(obj)
            if imdb_id:
                items.append({"imdb_id": imdb_id, "title": title or ""})

            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
        elif isinstance(obj, str):
            for imdb_id in IMDB_ID_RE.findall(obj):
                items.append({"imdb_id": imdb_id, "title": ""})

    walk(data)
    return items


def _find_id_in_dict(obj: dict) -> str:
    for key in ("id", "titleId", "tconst", "const"):
        value = obj.get(key)
        if isinstance(value, str):
            match = IMDB_ID_RE.fullmatch(value) or IMDB_ID_RE.search(value)
            if match:
                return match.group(0)

    for value in obj.values():
        if isinstance(value, str):
            match = TITLE_PATH_RE.search(value)
            if match:
                return match.group(1)

    return ""


def _find_title_in_dict(obj: dict) -> str:
    for key in ("titleText", "originalTitleText", "primaryTitle", "title", "name"):
        value = obj.get(key)
        title = _coerce_title(value)
        if title:
            return title
    return ""


def _coerce_title(value: Any) -> str:
    if isinstance(value, str):
        return _clean_title(value)
    if isinstance(value, dict):
        for key in ("text", "title", "name", "plainText"):
            title = _coerce_title(value.get(key))
            if title:
                return title
    return ""


def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"^\d+\.\s*", "", title)
    if not title or IMDB_ID_RE.fullmatch(title):
        return ""
    return title


def _build_item(imdb_id: str, title: str) -> dict:
    return {
        "title": _clean_title(title) or f"IMDB:{imdb_id}",
        "imdb_id": imdb_id,
        "link": f"https://www.imdb.com/title/{imdb_id}/",
    }


def _merge_items(dom_items: list[dict[str, str]], structured_items: list[dict[str, str]]) -> list[dict]:
    merged: dict[str, dict[str, str]] = {}

    for item in dom_items:
        imdb_id = item.get("imdb_id", "")
        if not IMDB_ID_RE.fullmatch(imdb_id):
            continue

        title = _clean_title(item.get("title", ""))
        if imdb_id not in merged:
            merged[imdb_id] = _build_item(imdb_id, title)
        elif title and merged[imdb_id]["title"] == f"IMDB:{imdb_id}":
            merged[imdb_id]["title"] = title

    for item in structured_items:
        imdb_id = item.get("imdb_id", "")
        if not IMDB_ID_RE.fullmatch(imdb_id):
            continue

        title = _clean_title(item.get("title", ""))
        if imdb_id not in merged:
            merged[imdb_id] = _build_item(imdb_id, title)
        elif title and merged[imdb_id]["title"] == f"IMDB:{imdb_id}":
            merged[imdb_id]["title"] = title

    return list(merged.values())
