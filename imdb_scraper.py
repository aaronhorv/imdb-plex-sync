"""Playwright-based IMDb list scraping."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import threading
import time
from datetime import datetime
from http.cookies import SimpleCookie
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


IMDB_ID_RE = re.compile(r"tt\d+")
TITLE_PATH_RE = re.compile(r"/title/(tt\d+)")
MAX_SCROLL_ATTEMPTS = 80
MAX_STAGNANT_SCROLLS = 6
MAX_PAGINATION_PAGES = 250
DOWNLOAD_TIMEOUT_MS = 120_000
DIRECT_DOWNLOAD_TIMEOUT_MS = 15_000
EXPORT_STATUS_URL = "https://www.imdb.com/exports/"
PERSONAL_WATCHLIST_URL = "https://www.imdb.com/list/watchlist?sort=date_added%2Cdesc"
DEFAULT_BROWSER_PROFILE_DIR = "/config/imdb-browser-profile"
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)
IMDB_PROFILE_LOCK = threading.Lock()

ID_COLUMNS = ("Const", "IMDb ID", "Title ID", "URL")
TITLE_COLUMNS = ("Title", "Original Title", "Title Name")
DATE_ADDED_COLUMNS = ("Created", "Date Added", "Added", "Created At")
WATCHLIST_TITLE_LINK_SELECTOR = (
    '[data-testid*="title-list-item"] a[href*="/title/tt"], '
    '.ipc-metadata-list-summary-item a[href*="/title/tt"]'
)


class ImdbExportUnavailable(RuntimeError):
    """Raised when IMDb CSV export is not available from the rendered page."""


class ImdbCsvParseError(RuntimeError):
    """Raised when a downloaded IMDb CSV cannot be parsed into valid IDs."""


class ImdbAccessBlockedError(RuntimeError):
    """Raised when IMDb shows an interstitial instead of the requested list."""


def scrape_imdb_watchlist(
    list_url: str,
    imdb_cookie: str = "",
    browser_profile_dir: str | None = None,
    logger=None,
) -> list[dict]:
    """Scrape an IMDb watchlist or custom list URL.

    Preferred order:
      1. Official IMDb CSV export via Playwright download handling.
      2. Paginated rendered DOM extraction.
      3. Scrolling rendered DOM plus structured response extraction.

    Args:
        list_url: IMDb watchlist or list URL to scrape.
        imdb_cookie: Optional IMDb cookie header used to seed or refresh the profile.
        browser_profile_dir: Persistent Chromium profile directory.
        logger: Optional callable logger. Supports ``logger(message)`` and
            ``logger(message, level)`` styles.

    Returns:
        A stable list of dictionaries containing ``title``, ``imdb_id``, and
        ``link`` keys.
    """

    if not list_url:
        return []

    normalized_list_url = _normalize_imdb_list_url(list_url)
    if normalized_list_url != list_url:
        _log(logger, f"IMDb normalized list URL: {normalized_list_url}", "info")
    list_url = normalized_list_url
    browser_profile_dir = browser_profile_dir or os.environ.get(
        "IMDB_BROWSER_PROFILE_DIR",
        DEFAULT_BROWSER_PROFILE_DIR,
    )
    os.makedirs(browser_profile_dir, exist_ok=True)

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

    context = None
    page = None
    profile_locked = False

    try:
        profile_locked = IMDB_PROFILE_LOCK.acquire(timeout=300)
        if not profile_locked:
            raise RuntimeError("Timed out waiting for the IMDb browser profile")

        with sync_playwright() as playwright:
            _log(logger, f"IMDb persistent browser profile: {browser_profile_dir}", "info")
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=browser_profile_dir,
                headless=True,
                accept_downloads=True,
                locale="en-US",
                user_agent=os.environ.get("IMDB_BROWSER_USER_AGENT", DEFAULT_BROWSER_USER_AGENT),
                viewport={"width": 1920, "height": 1080},
                ignore_default_args=["--enable-automation"],
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-notifications",
                    "--disable-third-party-cookies",
                    "--disable-extensions",
                    "--window-size=1920,1080",
                ],
            )
            _seed_imdb_profile_cookies(
                context,
                browser_profile_dir,
                imdb_cookie,
                logger,
            )

            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15_000)
            page.on("response", handle_response)

            page.goto(list_url, wait_until="domcontentloaded", timeout=60_000)
            _wait_for_page_settle(page, logger)
            _log_page_diagnostics(page, logger)
            _raise_for_blocking_interstitial(page, logger)

            redirected_url = _normalize_imdb_list_url(page.url)
            if redirected_url != page.url:
                _log(logger, f"IMDb reapplying newest-first sort after redirect: {redirected_url}", "info")
                page.goto(redirected_url, wait_until="domcontentloaded", timeout=60_000)
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
                paginated_items = _scrape_paginated_dom(page, structured_items, logger)
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
        for resource in (page, context):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception:
                pass
        if profile_locked:
            IMDB_PROFILE_LOCK.release()


def parse_imdb_csv(csv_text: str, logger=None) -> list[dict]:
    """Parse IMDb CSV export text into the app's internal item format."""
    if not csv_text.strip():
        raise ImdbCsvParseError("CSV was empty")

    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    headers = reader.fieldnames or []
    _log(logger, f"IMDb CSV headers: {headers}", "info")

    if not headers:
        raise ImdbCsvParseError("CSV had no headers")

    parsed_items: list[tuple[dict, float | None, int]] = []
    seen_ids: set[str] = set()
    parsed_rows = 0

    for row in reader:
        parsed_rows += 1
        imdb_id = _extract_csv_imdb_id(row)
        if not imdb_id or imdb_id in seen_ids:
            continue

        title = _extract_csv_title(row) or f"IMDB:{imdb_id}"
        date_added = _extract_csv_date_added(row)
        seen_ids.add(imdb_id)
        item = _build_item(imdb_id, title)
        if date_added:
            item["date_added"] = date_added
        parsed_items.append((item, _date_sort_value(date_added), parsed_rows))

    if any(sort_value is not None for _, sort_value, _ in parsed_items):
        parsed_items.sort(
            key=lambda record: (
                record[1] is None,
                -(record[1] or 0),
                record[2],
            )
        )
        _log(logger, "IMDb CSV rows sorted by date added (newest first)", "info")

    items = [item for item, _, _ in parsed_items]

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


def _seed_imdb_profile_cookies(context, profile_dir: str, cookie_value: str, logger=None) -> bool:
    imdb_cookies = parse_imdb_cookie_string(cookie_value)
    if not imdb_cookies:
        _log(logger, "IMDb persistent profile will be used without a cookie refresh", "info")
        return False

    cookie_fingerprint = hashlib.sha256(cookie_value.strip().encode("utf-8")).hexdigest()
    marker_path = os.path.join(profile_dir, ".cookie-seed.sha256")
    previous_fingerprint = ""
    try:
        with open(marker_path, "r", encoding="ascii") as marker:
            previous_fingerprint = marker.read().strip()
    except OSError:
        pass

    try:
        profile_cookie_names = {
            cookie.get("name")
            for cookie in context.cookies("https://www.imdb.com")
        }
    except Exception:
        profile_cookie_names = set()

    has_authentication = bool(profile_cookie_names & {"at-main", "session-id", "session-token"})
    if previous_fingerprint == cookie_fingerprint and has_authentication:
        _log(logger, "IMDb authentication loaded from the persistent browser profile", "info")
        return False

    context.add_cookies(imdb_cookies)
    try:
        with open(marker_path, "w", encoding="ascii") as marker:
            marker.write(cookie_fingerprint)
    except OSError as exc:
        _log(logger, f"Could not save IMDb cookie refresh marker: {exc}", "warning")
    _log(logger, f"IMDb persistent profile refreshed with {len(imdb_cookies)} configured cookies", "info")
    return True


def _scrape_via_csv_export(page: Page, logger) -> tuple[list[dict], str]:
    export_control = _find_export_control(page, logger)
    if export_control is None and "/watchlist" in page.url.lower():
        _log(logger, f"IMDb opening authenticated watchlist export page: {PERSONAL_WATCHLIST_URL}", "info")
        page.goto(PERSONAL_WATCHLIST_URL, wait_until="domcontentloaded", timeout=60_000)
        _wait_for_page_settle(page, logger)
        _log_page_diagnostics(page, logger)
        _raise_for_blocking_interstitial(page, logger)
        export_control = _find_export_control(page, logger)

    if export_control is None:
        _log(logger, "IMDb Export control found: no", "info")
        raise ImdbExportUnavailable("Export control was not found")

    _log(logger, "IMDb Export control found: yes", "info")

    try:
        with page.expect_download(timeout=DIRECT_DOWNLOAD_TIMEOUT_MS) as download_info:
            export_control.click(timeout=10_000)
        download = download_info.value
        _log(logger, "IMDb CSV download started: yes", "info")
        filename = download.suggested_filename or "imdb-export.csv"
        _log(logger, f"IMDb CSV filename: {filename}", "info")
        csv_text = _read_download_text(download)
        return parse_imdb_csv(csv_text, logger), "direct download"
    except PlaywrightTimeoutError:
        _log(logger, "IMDb CSV download started directly: no; checking asynchronous exports", "info")

    download = _wait_for_watchlist_export_download(page, logger)
    filename = download.suggested_filename or "imdb-export.csv"
    _log(logger, "IMDb CSV download started from asynchronous export: yes", "info")
    _log(logger, f"IMDb CSV filename: {filename}", "info")
    csv_text = _read_download_text(download)
    return parse_imdb_csv(csv_text, logger), "asynchronous export job"


def _find_export_control(page: Page, logger):
    selectors = [
        'div[data-testid*="hero-list-subnav-export-button"] button',
        'button[data-testid*="hero-list-subnav-export-button"]',
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

    for index, selector in enumerate(selectors):
        locator = page.locator(selector)
        if index < 2:
            try:
                locator.nth(0).wait_for(state="visible", timeout=10_000)
                _log(logger, f"IMDb Export selector matched: {selector}", "info")
                return locator.nth(0)
            except Exception:
                continue
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


def _wait_for_watchlist_export_download(page: Page, logger):
    _log(logger, f"IMDb polling asynchronous exports page: {EXPORT_STATUS_URL}", "info")
    deadline = time.monotonic() + (DOWNLOAD_TIMEOUT_MS / 1000)

    while time.monotonic() < deadline:
        page.goto(EXPORT_STATUS_URL, wait_until="domcontentloaded", timeout=60_000)
        _wait_for_page_settle(page, logger)
        _raise_for_blocking_interstitial(page, logger)

        export_rows = page.locator(".ipc-metadata-list-summary-item")
        try:
            row_count = export_rows.count()
        except Exception:
            row_count = 0

        _log(logger, f"IMDb exports page contains {row_count} export rows", "info")
        for index in range(row_count):
            row = export_rows.nth(index)
            try:
                row_text = row.inner_text(timeout=2_000)
            except Exception:
                continue
            if "watchlist" not in row_text.lower():
                continue
            if "in progress" in row_text.lower():
                _log(logger, "IMDb watchlist export is still in progress", "info")
                break

            download_control = row.locator(
                'button[data-testid*="export-status-button"], '
                'a[download], a[href*="csv" i], '
                'button:has-text("Download"), a:has-text("Download")'
            )
            try:
                if not download_control.count():
                    _log(logger, "IMDb watchlist export row is ready but has no download control", "warning")
                    break
                with page.expect_download(timeout=30_000) as download_info:
                    download_control.nth(0).click(timeout=10_000)
                return download_info.value
            except PlaywrightTimeoutError:
                _log(logger, "IMDb watchlist export control did not start a download", "warning")
                break

        page.wait_for_timeout(5_000)

    raise ImdbExportUnavailable("IMDb watchlist export did not become downloadable before timeout")


def _read_download_text(download) -> str:
    path = download.path()
    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        return file.read()


def _scrape_paginated_dom(page: Page, structured_items: list[dict[str, str]], logger) -> list[dict]:
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

        _scroll_to_pagination_controls(page, logger)
        next_button = _find_next_control(page)
        _log(logger, f"IMDb pagination page {page_number}: Next button found: {bool(next_button)}", "info")
        if next_button is None:
            previous_first = _first_title_id(page)
            if new_count and _go_to_numbered_page(page, page_number + 1, previous_first, logger):
                continue
            break

        previous_url = page.url
        previous_first = _first_title_id(page)
        if not _go_to_next_page(page, next_button, logger):
            break

        _wait_for_pagination_change(page, previous_url, previous_first, logger)

    merged_items = _merge_items(all_items, structured_items)
    if structured_items:
        _log(
            logger,
            f"IMDb pagination merged {len(merged_items)} unique titles from DOM and structured responses",
            "info",
        )
    return merged_items


def _scroll_to_pagination_controls(page: Page, logger) -> None:
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(750)
        _wait_for_page_settle(page, logger)
    except Exception as exc:
        _log(logger, f"IMDb pagination footer scroll failed: {exc}", "info")


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


def _go_to_numbered_page(page: Page, page_number: int, previous_first: str, logger) -> bool:
    next_url = _with_page_number(page.url, page_number)
    _log(logger, f"IMDb pagination trying numbered page URL: {next_url}", "info")

    try:
        page.goto(next_url, wait_until="domcontentloaded", timeout=60_000)
        _wait_for_page_settle(page, logger)
    except Exception as exc:
        _log(logger, f"IMDb pagination numbered page navigation failed: {exc}", "warning")
        return False

    _raise_for_blocking_interstitial(page, logger)
    current_first = _first_title_id(page)
    if current_first and current_first != previous_first:
        _log(logger, f"IMDb pagination numbered page {page_number} loaded", "info")
        return True

    _log(logger, f"IMDb pagination numbered page {page_number} did not expose new titles", "info")
    return False


def _with_page_number(url: str, page_number: int) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["page"] = str(page_number)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _normalize_imdb_list_url(url: str) -> str:
    parsed = urlparse(url)
    if "imdb." not in parsed.netloc.lower() or not (
        "/watchlist" in parsed.path.lower() or "/list/" in parsed.path.lower()
    ):
        return url
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["sort"] = "date_added,desc"
    query.pop("page", None)
    return urlunparse(parsed._replace(query=urlencode(query)))


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


def _extract_csv_date_added(row: dict[str, str]) -> str:
    for column in DATE_ADDED_COLUMNS:
        value = _row_value(row, column)
        if value:
            return value
    return ""


def _date_sort_value(value: str) -> float | None:
    if not value:
        return None

    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        pass

    for date_format in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(normalized, date_format).timestamp()
        except ValueError:
            continue
    return None


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
            ("403", "404", "forbidden", "access denied", "not found", "unavailable"),
            ("403 forbidden", "access denied", "page not found", "this page could not be found"),
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
        guidance = (
            "IMDb rejected the browser request or returned an error page. "
            "The saved session may need to be refreshed from a successful IMDb login."
        )

    raise ImdbAccessBlockedError(f"IMDb access blocked by {label_text} page. {guidance}")


def _wait_for_page_settle(page: Page, logger) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except PlaywrightTimeoutError:
        _log(logger, "IMDb network did not become idle; continuing", "info")


def _wait_for_initial_titles(page: Page, logger) -> None:
    try:
        page.wait_for_selector(WATCHLIST_TITLE_LINK_SELECTOR, timeout=15_000)
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

          const scopedSelector = [
            '[data-testid*="title-list-item"] a[href*="/title/tt"]',
            '.ipc-metadata-list-summary-item a[href*="/title/tt"]'
          ].join(', ');
          let links = Array.from(document.querySelectorAll(scopedSelector));

          if (!links.length) {
            links = Array.from(document.querySelectorAll('a[href*="/title/tt"]')).filter((link) => {
              const section = link.closest('section, [data-testid*="section"]');
              if (!section) return true;
              const heading = section.querySelector('h1, h2, h3, [role="heading"]');
              const sectionLabel = cleanTitle(
                (heading && (heading.innerText || heading.textContent)) ||
                section.getAttribute('aria-label') ||
                section.getAttribute('data-testid') ||
                ''
              );
              return !/recently viewed/i.test(sectionLabel);
            });
          }

          const items = [];
          for (const link of links) {
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
    """Extract only IMDb title-list connection entries, excluding page recommendations."""
    connections: list[Any] = []

    def find_connections(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).lower() == "titlelistitemsearch":
                    connections.append(value)
                else:
                    find_connections(value)
        elif isinstance(obj, list):
            for value in obj:
                find_connections(value)

    find_connections(data)

    items: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for connection in connections:
        entries = []
        if isinstance(connection, dict):
            for key in ("edges", "items", "results"):
                value = connection.get(key)
                if isinstance(value, list):
                    entries = value
                    break
        elif isinstance(connection, list):
            entries = connection

        for entry in entries:
            candidate = entry.get("node", entry) if isinstance(entry, dict) else entry
            imdb_id = _find_id_in_tree(candidate)
            if not imdb_id or imdb_id in seen_ids:
                continue
            seen_ids.add(imdb_id)
            item = {"imdb_id": imdb_id, "title": _find_title_in_tree(candidate)}
            date_added = _find_date_added_in_tree(candidate)
            if date_added:
                item["date_added"] = date_added
            items.append(item)

    return items


def _find_id_in_tree(obj: Any) -> str:
    if isinstance(obj, dict):
        imdb_id = _find_id_in_dict(obj)
        if imdb_id:
            return imdb_id
        for value in obj.values():
            imdb_id = _find_id_in_tree(value)
            if imdb_id:
                return imdb_id
    elif isinstance(obj, list):
        for value in obj:
            imdb_id = _find_id_in_tree(value)
            if imdb_id:
                return imdb_id
    return ""


def _find_title_in_tree(obj: Any) -> str:
    if isinstance(obj, dict):
        title = _find_title_in_dict(obj)
        if title:
            return title
        for value in obj.values():
            title = _find_title_in_tree(value)
            if title:
                return title
    elif isinstance(obj, list):
        for value in obj:
            title = _find_title_in_tree(value)
            if title:
                return title
    return ""


def _find_date_added_in_tree(obj: Any) -> str:
    if isinstance(obj, dict):
        for key, value in obj.items():
            normalized_key = str(key).replace("_", "").lower()
            if normalized_key in {"created", "createdat", "dateadded", "addedat"} and value:
                return str(value)
        for value in obj.values():
            date_added = _find_date_added_in_tree(value)
            if date_added:
                return date_added
    elif isinstance(obj, list):
        for value in obj:
            date_added = _find_date_added_in_tree(value)
            if date_added:
                return date_added
    return ""


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

        date_added = item.get("date_added")
        if date_added:
            merged[imdb_id]["date_added"] = date_added

    items = list(merged.values())
    if any(_date_sort_value(item.get("date_added", "")) is not None for item in items):
        items.sort(
            key=lambda item: (
                _date_sort_value(item.get("date_added", "")) is None,
                -(_date_sort_value(item.get("date_added", "")) or 0),
            )
        )
    return items
