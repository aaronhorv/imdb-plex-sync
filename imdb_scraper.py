"""Playwright-based IMDb list scraping."""

from __future__ import annotations

import re
from typing import Any, Callable

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


IMDB_ID_RE = re.compile(r"tt\d+")
TITLE_PATH_RE = re.compile(r"/title/(tt\d+)")
MAX_SCROLL_ATTEMPTS = 160
MAX_STAGNANT_SCROLLS = 8


def scrape_imdb_watchlist(
    list_url: str,
    imdb_cookie: str = "",
    logger=None,
) -> list[dict]:
    """Scrape an IMDb watchlist or custom list URL.

    Args:
        list_url: IMDb watchlist or list URL to scrape.
        imdb_cookie: Optional IMDb ``at-main`` cookie value for private lists.
        logger: Optional callable logger. Supports ``logger(message)`` and
            ``logger(message, level)`` styles.

    Returns:
        A stable list of dictionaries containing ``title``, ``imdb_id``, and
        ``link`` keys.
    """

    if not list_url:
        return []

    dom_items: list[dict[str, str]] = []
    structured_items: list[dict[str, str]] = []

    def handle_response(response) -> None:
        url = response.url.lower()
        content_type = response.headers.get("content-type", "").lower()
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
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )

            if imdb_cookie:
                context.add_cookies(
                    [
                        {
                            "name": "at-main",
                            "value": imdb_cookie,
                            "domain": ".imdb.com",
                            "path": "/",
                            "secure": True,
                            "httpOnly": True,
                            "sameSite": "Lax",
                        }
                    ]
                )
                _log(logger, "IMDb cookie authentication enabled", "info")

            page = context.new_page()
            page.set_default_timeout(15_000)
            page.on("response", handle_response)

            page.goto(list_url, wait_until="domcontentloaded", timeout=45_000)
            _log(logger, "IMDb page opened", "info")
            _wait_for_initial_titles(page, logger)

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
                    _log(logger, f"IMDb collected {seen_count} unique titles", "info")
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

            results = _merge_items(dom_items, structured_items)
            _log(logger, f"IMDb scrape completed with {len(results)} items", "success")
            return results

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


def _log(logger: Callable[..., Any] | None, message: str, level: str = "info") -> None:
    if logger is None:
        return
    try:
        logger(message, level)
    except TypeError:
        logger(message)
    except Exception:
        pass


def _wait_for_initial_titles(page: Page, logger) -> None:
    try:
        page.wait_for_selector('a[href*="/title/tt"]', timeout=15_000)
    except PlaywrightTimeoutError:
        _log(logger, "IMDb title links were not visible after initial load", "warning")

    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except PlaywrightTimeoutError:
        _log(logger, "IMDb network did not become idle; continuing with rendered DOM", "info")


def _wait_for_scroll_progress(page: Page, previous_height: int) -> None:
    try:
        page.wait_for_function(
            "(height) => document.body.scrollHeight > height",
            arg=previous_height,
            timeout=2_500,
        )
    except PlaywrightTimeoutError:
        pass

    try:
        page.wait_for_load_state("networkidle", timeout=3_000)
    except PlaywrightTimeoutError:
        pass

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


def _merge_items(dom_items: list[dict[str, str]], structured_items: list[dict[str, str]]) -> list[dict]:
    merged: dict[str, dict[str, str]] = {}

    for item in dom_items:
        imdb_id = item.get("imdb_id", "")
        if not IMDB_ID_RE.fullmatch(imdb_id):
            continue

        title = _clean_title(item.get("title", ""))
        if imdb_id not in merged:
            merged[imdb_id] = {
                "title": title or f"IMDB:{imdb_id}",
                "imdb_id": imdb_id,
                "link": f"https://www.imdb.com/title/{imdb_id}/",
            }
        elif title and merged[imdb_id]["title"] == f"IMDB:{imdb_id}":
            merged[imdb_id]["title"] = title

    for item in structured_items:
        imdb_id = item.get("imdb_id", "")
        if not IMDB_ID_RE.fullmatch(imdb_id):
            continue

        title = _clean_title(item.get("title", ""))
        if imdb_id not in merged:
            merged[imdb_id] = {
                "title": title or f"IMDB:{imdb_id}",
                "imdb_id": imdb_id,
                "link": f"https://www.imdb.com/title/{imdb_id}/",
            }
        elif title and merged[imdb_id]["title"] == f"IMDB:{imdb_id}":
            merged[imdb_id]["title"] = title

    return list(merged.values())
