from flask import Flask, render_template, request, jsonify
import requests
from bs4 import BeautifulSoup
import json
import os
import time
from datetime import datetime
import schedule
import threading
import re

from imdb_scraper import (
    ImdbAccessBlockedError,
    parse_imdb_cookie_string,
    parse_imdb_csv,
    scrape_imdb_watchlist,
)

app = Flask(__name__)

CONFIG_FILE = '/config/config.json'
LOGS_FILE = '/config/logs.json'
RESULTS_FILE = '/config/sync_results.json'
STATS_FILE = '/config/sync_stats.json'
IMDB_CACHE_FILE = '/config/imdb_items_cache.json'
VERSION_FILE = '/app/VERSION'
LOCAL_VERSION_FILE = os.path.join(os.path.dirname(__file__), 'VERSION')
PLEX_REQUEST_TIMEOUT = (5, 30)
PLEX_REQUEST_RETRIES = 3
PLEX_CLIENT_IDENTIFIER = 'watchlist-plex-sync'

def get_app_version():
    """Return the deployed app version."""
    env_version = os.environ.get('APP_VERSION', '').strip()
    if env_version:
        return env_version
    for version_file in (VERSION_FILE, LOCAL_VERSION_FILE):
        if os.path.exists(version_file):
            with open(version_file, 'r') as f:
                return f.read().strip()
    return 'dev'

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {
        'listSource': 'imdb',
        'imdbListUrl': '',
        'tmdbListId': '',
        'tmdbAccountId': '',
        'tmdbSessionId': '',
        'traktListUrl': '',
        'traktApiKey': '',
        'traktClientSecret': '',
        'traktAccessToken': '',
        'traktRefreshToken': '',
        'plexToken': '',
        'tmdbApiKey': '',
        'streamingServices': [],  # Now stores [{"id": 8, "region": "DE"}, ...]
        'imdbCookie': '',
        'cleanupUnlistedEnabled': False
    }

def save_config(config):
    os.makedirs('/config', exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

def load_sync_results():
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, 'r') as f:
            return json.load(f)
    return []

def save_sync_results(results):
    os.makedirs('/config', exist_ok=True)
    with open(RESULTS_FILE, 'w') as f:
        json.dump(results, f, indent=2)

def load_imdb_items_cache(list_url):
    if not os.path.exists(IMDB_CACHE_FILE):
        return []

    try:
        with open(IMDB_CACHE_FILE, 'r') as f:
            cache = json.load(f)
        if cache.get('list_url') != list_url:
            add_log("Cached IMDB watchlist ignored because the configured URL changed", 'warning')
            return []
        items = cache.get('items', [])
        if not isinstance(items, list):
            return []
        saved_at = cache.get('saved_at', 'unknown time')
        add_log(f"Using cached IMDB watchlist from {saved_at}: {len(items)} items", 'warning')
        return items
    except Exception as e:
        add_log(f"Could not read cached IMDB watchlist: {str(e)}", 'warning')
        return []

def save_imdb_items_cache(list_url, items):
    if not items:
        return

    try:
        os.makedirs('/config', exist_ok=True)
        with open(IMDB_CACHE_FILE, 'w') as f:
            json.dump({
                'list_url': list_url,
                'saved_at': datetime.now().isoformat(),
                'count': len(items),
                'items': items,
            }, f, indent=2)
        add_log(f"Cached IMDB watchlist: {len(items)} items", 'info')
    except Exception as e:
        add_log(f"Could not cache IMDB watchlist: {str(e)}", 'warning')

def save_sync_stats(stats):
    """Save sync statistics including removed count"""
    os.makedirs('/config', exist_ok=True)
    with open(STATS_FILE, 'w') as f:
        json.dump(stats, f, indent=2)

def load_sync_stats():
    """Load sync statistics"""
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, 'r') as f:
            return json.load(f)
    return {'removed': 0, 'last_sync': None}

def add_log(message, log_type='info'):
    logs = []
    if os.path.exists(LOGS_FILE):
        with open(LOGS_FILE, 'r') as f:
            logs = json.load(f)
    
    logs.insert(0, {
        'timestamp': datetime.now().isoformat(),
        'message': message,
        'type': log_type
    })
    
    logs = logs[:100]
    
    with open(LOGS_FILE, 'w') as f:
        json.dump(logs, f, indent=2)
    
    print(f"[{log_type.upper()}] {message}")

def extract_user_id(url):
    """Extract user ID from IMDB watchlist URL"""
    match = re.search(r'user/(ur\d+)', url)
    if match:
        return match.group(1)
    return None

def apply_imdb_cookies(session, imdb_cookie):
    """Apply configured IMDb cookies to a requests session."""
    cookies = parse_imdb_cookie_string(imdb_cookie)
    for cookie in cookies:
        session.cookies.set(
            cookie['name'],
            cookie['value'],
            domain=cookie.get('domain', '.imdb.com'),
            path=cookie.get('path', '/')
        )
    return len(cookies)

def scrape_watchlist_page(soup, url, html_content=None):
    """Scrape watchlist page for IMDB IDs using JSON extraction - gets ALL titles"""
    items = []
    seen_ids = set()
    
    # Use the HTML content if provided, otherwise get it from soup
    if html_content is None:
        html_content = str(soup)
    
    # PRIMARY METHOD: Extract title+ID pairs from JSON structure
    # The JSON has objects with both titleText and an ID reference
    # Pattern: find sections that contain both titleText and title ID
    
    # Look for patterns like: {"titleText":{"text":"TITLE"}...some json..."id":"tt1234567"}
    # or reverse: {"id":"tt1234567"...some json..."titleText":{"text":"TITLE"}}
    
    add_log("DEBUG: Attempting JSON extraction...", 'info')
    
    # Method 1: Try to extract from structured JSON blocks
    # Find all title/ID pairs in proximity
    import json as json_module
    
    # Try to find JSON-LD or embedded JSON with full objects
    script_tags = soup.find_all('script', type='application/json')
    
    for script in script_tags:
        try:
            data = json_module.loads(script.string)
            # Navigate through the nested structure
            if isinstance(data, dict):
                # Look for lists that might contain our movies
                def extract_from_dict(obj, items_list, seen):
                    if isinstance(obj, dict):
                        # Check if this object has both titleText and an ID
                        if 'titleText' in obj and 'id' in obj:
                            title_obj = obj.get('titleText', {})
                            title = title_obj.get('text', '') if isinstance(title_obj, dict) else str(title_obj)
                            imdb_id = obj.get('id', '')
                            
                            if title and imdb_id.startswith('tt') and imdb_id not in seen:
                                seen.add(imdb_id)
                                items_list.append({
                                    'title': title,
                                    'imdb_id': imdb_id,
                                    'link': f"https://www.imdb.com/title/{imdb_id}/"
                                })
                        
                        # Recursively search nested dicts and lists
                        for value in obj.values():
                            extract_from_dict(value, items_list, seen)
                    elif isinstance(obj, list):
                        for item in obj:
                            extract_from_dict(item, items_list, seen)
                
                extract_from_dict(data, items, seen_ids)
        except:
            continue
    
    add_log(f"DEBUG: JSON parsing found {len(items)} items", 'info')
    
    # Method 2: If JSON parsing didn't work, use regex on the HTML
    if len(items) < 50:
        add_log("DEBUG: Trying regex-based extraction...", 'info')
        
        # Look for the pattern where titleText and id are close together
        # Example: "titleText":{"text":"Movie Name"}...some stuff..."id":"tt1234567"
        pattern = r'"titleText":\s*\{\s*"text":\s*"([^"]+)"\s*\}[^}]*?"id":\s*"(tt\d+)"'
        matches = re.findall(pattern, html_content, re.DOTALL)
        
        for title, imdb_id in matches:
            if imdb_id not in seen_ids:
                seen_ids.add(imdb_id)
                items.append({
                    'title': title,
                    'imdb_id': imdb_id,
                    'link': f"https://www.imdb.com/title/{imdb_id}/"
                })
        
        add_log(f"DEBUG: Regex extraction found {len(items)} items", 'info')
    
    # Method 3: If still not enough, try reverse pattern (id before titleText)
    if len(items) < 50:
        add_log("DEBUG: Trying reverse regex pattern...", 'info')
        
        pattern = r'"id":\s*"(tt\d+)"[^}]*?"titleText":\s*\{\s*"text":\s*"([^"]+)"\s*\}'
        matches = re.findall(pattern, html_content, re.DOTALL)
        
        for imdb_id, title in matches:
            if imdb_id not in seen_ids:
                seen_ids.add(imdb_id)
                items.append({
                    'title': title,
                    'imdb_id': imdb_id,
                    'link': f"https://www.imdb.com/title/{imdb_id}/"
                })
        
        add_log(f"DEBUG: Reverse pattern found {len(items)} total items", 'info')
    
    add_log(f"DEBUG: Extracted {len(items)} unique items from JSON extraction", 'info')
    
    # FALLBACK METHOD: If JSON extraction failed, try traditional scraping
    if not items:
        add_log("DEBUG: JSON extraction failed, trying traditional scraping", 'warning')
        
        # Method 1: Find all title links
        title_links = soup.find_all('a', href=re.compile(r'/title/tt\d+'))
        add_log(f"DEBUG: Found {len(title_links)} title links", 'info')
        
        for link in title_links:
            href = link.get('href', '')
            imdb_match = re.search(r'/title/(tt\d+)', href)
            
            if imdb_match:
                imdb_id = imdb_match.group(1)
                
                if imdb_id in seen_ids:
                    continue
                seen_ids.add(imdb_id)
                
                # Try to get title text
                title = link.get_text(strip=True)
                
                if not title or len(title) < 2:
                    parent = link.parent
                    if parent:
                        heading = parent.find(['h3', 'h2', 'h1'])
                        if heading:
                            title = heading.get_text(strip=True)
                
                if not title or len(title) < 2:
                    title = f"IMDB:{imdb_id}"
                
                items.append({
                    'title': title,
                    'imdb_id': imdb_id,
                    'link': f"https://www.imdb.com/title/{imdb_id}/"
                })
        
        add_log(f"DEBUG: Fallback method extracted {len(items)} items", 'info')
    
    return items

def get_imdb_export_data(user_id):
    """Get IMDB watchlist data - JSON extraction gets ALL 248+ items!"""
    try:
        session = requests.Session()
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'max-age=0',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
        }

        # Try both URLs with JSON extraction
        watchlist_url = f"https://www.imdb.com/user/{user_id}/watchlist"

        imdb_cookie = load_config().get('imdbCookie', '').strip()
        if imdb_cookie:
            cookie_count = apply_imdb_cookies(session, imdb_cookie)
            add_log(f"Using IMDB cookies for authenticated request ({cookie_count} cookies)", 'info')

        add_log(f"Fetching watchlist page for {user_id}...", 'info')
        response = session.get(watchlist_url, headers=headers, timeout=20)

        if response.status_code >= 400:
            add_log(f"Failed to fetch watchlist: {response.status_code}", 'error')
            return []
        if response.status_code != 200:
            add_log(f"Watchlist returned {response.status_code}, attempting extraction anyway...", 'warning')
        
        # JSON EXTRACTION - This gets ALL items!
        html_content = response.text
        soup = BeautifulSoup(response.content, 'html.parser')
        
        add_log("Attempting JSON extraction from watchlist page...", 'info')
        items = scrape_watchlist_page(soup, watchlist_url, html_content)
        
        if items:
            add_log(f"✓ JSON extraction successful: {len(items)} items found!", 'success')
            return items
        
        # If JSON extraction completely failed, try to find list ID and use that
        add_log("JSON extraction returned no items, looking for list ID...", 'warning')
        
        list_id = None
        for element in soup.find_all(True):
            if element.has_attr('data-list-id'):
                list_id = element['data-list-id']
                break
        
        if not list_id:
            scripts = soup.find_all('script')
            for script in scripts:
                script_content = script.string if script.string else ""
                match = re.search(r'(ls\d{8,})', script_content)
                if match:
                    list_id = match.group(1)
                    break
        
        if list_id:
            add_log(f"Found list ID {list_id}, trying direct list URL...", 'info')
            return get_imdb_list_data(list_id)
        
        add_log("Could not extract any items", 'error')
        return []
        
    except Exception as e:
        add_log(f"Error in get_imdb_export_data: {str(e)}", 'error')
        import traceback
        add_log(f"Traceback: {traceback.format_exc()}", 'error')
        return []

def parse_csv_export(csv_text):
    """Parse IMDB CSV export"""
    try:
        return parse_imdb_csv(csv_text, logger=add_log)
    except Exception as e:
        add_log(f"Error parsing IMDB CSV export: {str(e)}", 'error')
        return []

def get_imdb_watchlist(list_url):
    """Main function to get IMDB watchlist items"""
    try:
        config = load_config()
        imdb_cookie = config.get('imdbCookie', '').strip()

        try:
            items = scrape_imdb_watchlist(
                list_url=list_url,
                imdb_cookie=imdb_cookie,
                logger=add_log,
            )
            if items:
                add_log(f"Playwright IMDB scrape successful: {len(items)} items found", 'success')
                save_imdb_items_cache(list_url, items)
                return items
            add_log("Playwright IMDB scrape returned no items; using fallback scraper", 'warning')
        except ImdbAccessBlockedError as e:
            add_log(str(e), 'error')
            cached_items = load_imdb_items_cache(list_url)
            if cached_items:
                add_log("Continuing with cached IMDB watchlist because IMDb is currently blocked", 'warning')
                return cached_items
            return []
        except Exception as e:
            add_log(f"Playwright IMDB scrape failed; using fallback scraper: {str(e)}", 'warning')

        # Check if it's a list URL (either /list/lsXXX or /user/urXXX/watchlist)
        list_match = re.search(r'/list/(ls\d+)', list_url)
        user_match = re.search(r'user/(ur\d+)', list_url)
        
        if list_match:
            # Direct list URL provided
            list_id = list_match.group(1)
            add_log(f"Detected direct list URL with ID: {list_id}", 'info')
            items = get_imdb_list_data(list_id)
            save_imdb_items_cache(list_url, items)
            return items
        elif user_match:
            # User watchlist URL
            user_id = user_match.group(1)
            add_log(f"Detected personal watchlist for user: {user_id}", 'info')
            items = get_imdb_export_data(user_id)
            save_imdb_items_cache(list_url, items)
            return items
        else:
            add_log(f"Could not parse URL: {list_url}", 'error')
            return []
        
    except Exception as e:
        add_log(f"Error fetching IMDB watchlist: {str(e)}", 'error')
        import traceback
        add_log(f"Traceback: {traceback.format_exc()}", 'error')
        return []

def get_imdb_list_data(list_id):
    """Get IMDB list data directly using list ID - JSON extraction gets ALL items!"""
    try:
        session = requests.Session()
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'max-age=0',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
        }

        imdb_cookie = load_config().get('imdbCookie', '').strip()
        if imdb_cookie:
            cookie_count = apply_imdb_cookies(session, imdb_cookie)
            add_log(f"Using IMDB cookies for authenticated request ({cookie_count} cookies)", 'info')

        add_log(f"Using list ID: {list_id}", 'info')

        # JSON EXTRACTION - Gets ALL items in one request!
        add_log(f"Attempting JSON extraction from list page...", 'info')
        list_url = f"https://www.imdb.com/list/{list_id}/"

        try:
            response = session.get(list_url, headers=headers, timeout=20)

            if response.status_code < 400:
                html_content = response.text
                soup = BeautifulSoup(response.content, 'html.parser')
                
                # Use the JSON extraction method
                items = scrape_watchlist_page(soup, list_url, html_content)
                
                if items:
                    add_log(f"✓ JSON extraction successful: {len(items)} items from list page!", 'success')
                    return items
                else:
                    add_log("JSON extraction returned no items", 'warning')
            else:
                add_log(f"List page returned {response.status_code}", 'error')
        except Exception as e:
            add_log(f"JSON extraction error: {e}", 'warning')
        
        # If JSON extraction failed, return empty
        add_log("Could not extract items from list", 'error')
        return []
        
    except Exception as e:
        add_log(f"Error in get_imdb_list_data: {str(e)}", 'error')
        import traceback
        add_log(f"Traceback: {traceback.format_exc()}", 'error')
        return []

def scrape_custom_list(list_url):
    """Scrape a custom IMDB list"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
        
        response = requests.get(list_url, headers=headers, timeout=15)
        response.raise_for_status()
        
        html_content = response.text
        soup = BeautifulSoup(response.content, 'html.parser')
        return scrape_watchlist_page(soup, list_url, html_content)
        
    except Exception as e:
        add_log(f"Error scraping custom list: {str(e)}", 'error')
        return []

def get_tmdb_list(list_id, api_key, session_id=None):
    """Fetch items from a TMDB list by list ID"""
    items = []
    try:
        url = f"https://api.themoviedb.org/3/list/{list_id}"
        params = {'api_key': api_key}
        if session_id:
            params['session_id'] = session_id
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        for entry in data.get('items', []):
            media_type = entry.get('media_type', 'movie')
            tmdb_id = entry.get('id')
            title = entry.get('title') or entry.get('name', '')
            year = (entry.get('release_date') or entry.get('first_air_date') or '')[:4]
            items.append({
                'title': title,
                'tmdb_id': tmdb_id,
                'media_type': media_type,
                'year': year,
                'imdb_id': None,
            })
            add_log(f"TMDB list item: {title} ({tmdb_id})", 'info')

    except Exception as e:
        add_log(f"Error fetching TMDB list {list_id}: {str(e)}", 'error')

    return items


def get_tmdb_watchlist(account_id, session_id, api_key):
    """Fetch movies and TV shows from a TMDB account watchlist.

    Requires:
      account_id  – numeric TMDB account ID (visible in TMDB account settings)
      session_id  – TMDB session ID (create at themoviedb.org/settings/api)
      api_key     – TMDB API v3 key (already required for streaming checks)
    """
    items = []
    try:
        for media_type in ('movies', 'tv'):
            page = 1
            while True:
                url = f"https://api.themoviedb.org/3/account/{account_id}/watchlist/{media_type}"
                params = {
                    'api_key': api_key,
                    'session_id': session_id,
                    'page': page,
                    'sort_by': 'created_at.asc',
                }
                response = requests.get(url, params=params, timeout=10)
                response.raise_for_status()
                data = response.json()

                for entry in data.get('results', []):
                    tmdb_id = entry.get('id')
                    plex_type = 'tv' if media_type == 'tv' else 'movie'
                    title = entry.get('title') or entry.get('name', '')
                    year = (entry.get('release_date') or entry.get('first_air_date') or '')[:4]
                    items.append({
                        'title': title,
                        'tmdb_id': tmdb_id,
                        'media_type': plex_type,
                        'year': year,
                        'imdb_id': None,
                    })
                    add_log(f"TMDB watchlist item: {title} ({tmdb_id})", 'info')

                total_pages = data.get('total_pages', 1)
                if page >= total_pages:
                    break
                page += 1

        add_log(f"TMDB watchlist: {len(items)} total items fetched", 'info')
    except Exception as e:
        add_log(f"Error fetching TMDB watchlist: {str(e)}", 'error')
        import traceback
        add_log(f"Traceback: {traceback.format_exc()}", 'error')

    return items


def get_trakt_list(list_url, trakt_api_key, trakt_access_token=None):
    """Fetch items from a Trakt list URL using the Trakt API.

    Supported URL formats:
      https://trakt.tv/users/<username>/watchlist
      https://trakt.tv/users/<username>/lists/<list-slug>
    When trakt_access_token is provided it is sent as a Bearer token,
    which allows access to private lists and resolves 'me' as the
    authenticated user.
    """
    items = []
    try:
        match_watchlist = re.search(r'(?:app\.)?trakt\.tv/users/([^/]+)/watchlist', list_url)
        match_custom = re.search(r'(?:app\.)?trakt\.tv/users/([^/]+)/lists/([^/?]+)', list_url)

        if match_watchlist:
            username = match_watchlist.group(1)
            api_url = f"https://api.trakt.tv/users/{username}/watchlist"
        elif match_custom:
            username = match_custom.group(1)
            list_slug = match_custom.group(2)
            api_url = f"https://api.trakt.tv/users/{username}/lists/{list_slug}/items"
        else:
            add_log(f"Unrecognised Trakt URL format: {list_url}", 'error')
            return items

        headers = {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': trakt_api_key,
        }
        if trakt_access_token:
            headers['Authorization'] = f'Bearer {trakt_access_token}'

        page = 1
        while True:
            params = {'page': page, 'limit': 100, 'extended': 'full'}
            response = requests.get(api_url, headers=headers, params=params, timeout=15)
            response.raise_for_status()
            entries = response.json()

            if not entries:
                break

            for entry in entries:
                media_type = entry.get('type', 'movie')
                obj = entry.get(media_type) or entry.get('movie') or entry.get('show')
                if not obj:
                    continue
                ids = obj.get('ids', {})
                imdb_id = ids.get('imdb')
                tmdb_id = ids.get('tmdb')
                title = obj.get('title', '')
                year = str(obj.get('year', ''))
                plex_type = 'tv' if media_type == 'show' else 'movie'
                items.append({
                    'title': title,
                    'imdb_id': imdb_id,
                    'tmdb_id': tmdb_id,
                    'media_type': plex_type,
                    'year': year,
                })
                add_log(f"Trakt list item: {title} ({imdb_id or tmdb_id})", 'info')

            total_pages = int(response.headers.get('X-Pagination-Page-Count', 1))
            if page >= total_pages:
                break
            page += 1

    except Exception as e:
        add_log(f"Error fetching Trakt list: {str(e)}", 'error')
        import traceback
        add_log(f"Traceback: {traceback.format_exc()}", 'error')

    return items


def get_tmdb_data(imdb_id, api_key):
    """Get TMDB data from IMDB ID"""
    try:
        url = f"https://api.themoviedb.org/3/find/{imdb_id}"
        params = {
            'api_key': api_key,
            'external_source': 'imdb_id'
        }
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get('movie_results'):
            result = data['movie_results'][0]
            return result['id'], 'movie', result.get('title', ''), result.get('release_date', '')[:4]
        elif data.get('tv_results'):
            result = data['tv_results'][0]
            return result['id'], 'tv', result.get('name', ''), result.get('first_air_date', '')[:4]
        
        return None, None, None, None
    except Exception as e:
        add_log(f"Error getting TMDB data for {imdb_id}: {str(e)}", 'error')
        return None, None, None, None

def check_streaming_availability(tmdb_id, media_type, api_key, streaming_services):
    """Check streaming availability with per-service regions - FIXED VERSION"""
    try:
        available_services = []
        
        # Log what we're checking
        add_log(f"Checking streaming for TMDB ID {tmdb_id} ({media_type})", 'info')
        add_log(f"Configured services: {streaming_services}", 'info')
        
        # Group services by region for efficiency
        regions_to_check = {}
        for service in streaming_services:
            # FIX: Handle both old format (int) and new format (dict with id/region)
            if isinstance(service, int):
                # Old format: just provider IDs as integers
                region = 'US'
                service_obj = {'id': service, 'region': region}
            elif isinstance(service, dict):
                # New format: {id: X, region: Y}
                region = service.get('region', 'US')
                service_obj = service
            else:
                add_log(f"Warning: Invalid service format: {service}", 'warning')
                continue
            
            if region not in regions_to_check:
                regions_to_check[region] = []
            regions_to_check[region].append(service_obj)
        
        add_log(f"Regions to check: {list(regions_to_check.keys())}", 'info')
        
        # Check each region
        for region, services in regions_to_check.items():
            url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/watch/providers"
            params = {'api_key': api_key}
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            add_log(f"Checking region {region} - Available regions in response: {list(data.get('results', {}).keys())}", 'info')
            
            if 'results' not in data or region not in data['results']:
                add_log(f"No data for region {region}", 'info')
                continue
            
            region_data = data['results'][region]
            add_log(f"Region {region} providers: {region_data.get('flatrate', [])}", 'info')
            
            # Check flatrate (subscription) services
            if 'flatrate' in region_data:
                for provider in region_data['flatrate']:
                    # FIX: Ensure provider is a dictionary before accessing keys
                    if not isinstance(provider, dict):
                        add_log(f"Warning: Provider is not a dict: {provider}", 'warning')
                        continue
                    
                    provider_id = provider.get('provider_id')
                    provider_name = provider.get('provider_name', 'Unknown')
                    
                    add_log(f"Found provider: {provider_name} (ID: {provider_id})", 'info')
                    
                    if not provider_id:
                        continue
                    
                    # Check if this provider matches any of our configured services
                    for service in services:
                        service_id = service.get('id')
                        add_log(f"Comparing provider {provider_id} with configured service {service_id}", 'info')
                        if provider_id == service_id:
                            service_name = f"{provider_name} ({region})"
                            if service_name not in available_services:
                                available_services.append(service_name)
                                add_log(f"MATCH! Added {service_name}", 'success')
        
        add_log(f"Final available services: {available_services}", 'info')
        return len(available_services) > 0, available_services
    except Exception as e:
        add_log(f"Error checking streaming availability: {str(e)}", 'warning')
        import traceback
        add_log(f"Traceback: {traceback.format_exc()}", 'error')
        return False, []

def plex_request(method, url, **kwargs):
    """Make a Plex Discover request with retries for transient slow responses."""
    kwargs.setdefault('timeout', PLEX_REQUEST_TIMEOUT)
    last_error = None

    for attempt in range(1, PLEX_REQUEST_RETRIES + 1):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code in (429, 500, 502, 503, 504):
                add_log(
                    f"Plex request returned HTTP {response.status_code} "
                    f"(attempt {attempt}/{PLEX_REQUEST_RETRIES})",
                    'warning'
                )
                last_error = requests.HTTPError(f"HTTP {response.status_code}", response=response)
            else:
                return response
        except requests.exceptions.RequestException as e:
            last_error = e
            add_log(
                f"Plex request failed (attempt {attempt}/{PLEX_REQUEST_RETRIES}): {str(e)}",
                'warning'
            )

        if attempt < PLEX_REQUEST_RETRIES:
            time.sleep(2 * attempt)

    if last_error:
        raise last_error
    raise RuntimeError("Plex request failed")

def plex_action_params(rating_key, plex_token):
    """Build Plex action params; action endpoints require token in query params."""
    return {
        'ratingKey': rating_key,
        'X-Plex-Token': plex_token,
    }

def plex_watchlist_action_key(metadata, fallback_rating_key):
    """Return the Plex online metadata key expected by watchlist actions."""
    guid = metadata.get('guid', '')
    if guid and '/' in guid:
        return guid.rsplit('/', 1)[-1]
    return fallback_rating_key

def extract_imdb_id_from_plex_metadata(item):
    """Extract an IMDB ID from Plex metadata in any known GUID shape."""
    candidates = []

    for key in ('guid', 'key'):
        value = item.get(key)
        if isinstance(value, str):
            candidates.append(value)

    for guid_obj in item.get('Guid', []) or []:
        guid_id = guid_obj.get('id')
        if isinstance(guid_id, str):
            candidates.append(guid_id)

    for candidate in candidates:
        match = re.search(r'(tt\d+)', candidate)
        if match:
            return match.group(1)

    return None

def normalize_match_title(title):
    """Normalize titles for cleanup comparison between Plex and IMDB."""
    if not title:
        return ''
    title = re.sub(r'\s+', ' ', str(title)).strip().lower()
    title = re.sub(r'^(the|a|an)\s+', '', title)
    title = re.sub(r'[^a-z0-9]+', '', title)
    return title

def item_title_year_key(item):
    """Build a title/year key for cleanup fallback matching."""
    title = normalize_match_title(item.get('title'))
    year = str(item.get('year') or '').strip()
    if not title:
        return None
    return (title, year)

def plex_item_exists_in_source(plex_item, imdb_ids, imdb_title_keys):
    """Return True when a Plex watchlist item is present in the IMDB source list."""
    imdb_id = plex_item.get('imdb_id')
    if imdb_id and imdb_id in imdb_ids:
        return True

    title_key = item_title_year_key(plex_item)
    if title_key and title_key in imdb_title_keys:
        return True

    if title_key:
        title_only_key = (title_key[0], '')
        return title_only_key in imdb_title_keys

    return False

def get_plex_metadata_by_rating_key(rating_key, plex_token):
    """Fetch full Plex metadata for a rating key."""
    if not rating_key:
        return None

    response = plex_request(
        'GET',
        f"https://discover.provider.plex.tv/library/metadata/{rating_key}",
        headers=plex_headers(plex_token),
    )

    if response.status_code != 200:
        add_log(f"Could not fetch Plex metadata for rating key {rating_key}: HTTP {response.status_code}", 'warning')
        return None

    metadata_items = response.json().get('MediaContainer', {}).get('Metadata', [])
    return metadata_items[0] if metadata_items else None

def plex_headers(plex_token):
    """Build Plex headers used by Discover and provider action endpoints."""
    return {
        'X-Plex-Token': plex_token,
        'X-Plex-Product': 'Watchlist Plex Sync',
        'X-Plex-Version': '1.0',
        'X-Plex-Client-Identifier': PLEX_CLIENT_IDENTIFIER,
        'X-Plex-Platform': 'Docker',
        'X-Plex-Device': 'watchlist-plex-sync',
        'X-Plex-Device-Name': 'watchlist-plex-sync',
        'X-Plex-Provider-Version': '6.5.0',
        'X-Plex-Language': 'en',
        'Accept-Language': 'en',
        'Accept': 'application/json',
    }

def check_plex_watchlist_access(plex_token):
    """Fail fast when the Plex token is invalid or cannot access Plex Discover."""
    headers = plex_headers(plex_token)

    try:
        add_log("Checking Plex watchlist access...", 'info')
        account_response = plex_request(
            'GET',
            "https://plex.tv/api/v2/user",
            headers=headers,
        )

        if account_response.status_code in (401, 403):
            add_log(
                f"Plex token is not accepted by plex.tv: HTTP {account_response.status_code}",
                'error'
            )
            return False
        if account_response.status_code != 200:
            add_log(
                f"Plex account token check failed: HTTP {account_response.status_code}",
                'error'
            )
            return False

        search_response = plex_request(
            'GET',
            "https://discover.provider.plex.tv/library/search",
            headers=headers,
            params={
                'query': 'test',
                'limit': 1,
                'searchTypes': 'movies,tv',
                'includeMetadata': 1,
                'searchProviders': 'discover',
            },
        )

        if search_response.status_code in (401, 403):
            add_log(
                f"Plex token cannot access Discover search: HTTP {search_response.status_code}",
                'error'
            )
            return False
        if search_response.status_code != 200:
            add_log(
                f"Plex Discover search check was inconclusive: HTTP {search_response.status_code}",
                'warning'
            )
            if search_response.text:
                add_log(f"Plex Discover search response: {search_response.text[:200]}", 'warning')

        watchlist_response = plex_request(
            'GET',
            "https://discover.provider.plex.tv/library/sections/watchlist/all",
            headers=headers,
            params={
                'includeCollections': 1,
                'includeExternalMedia': 1,
            },
        )

        if watchlist_response.status_code in (401, 403):
            add_log(
                f"Plex token cannot access the watchlist: HTTP {watchlist_response.status_code}",
                'error'
            )
            return False
        if watchlist_response.status_code != 200:
            add_log(
                f"Plex watchlist read check was inconclusive: HTTP {watchlist_response.status_code}",
                'warning'
            )
            if watchlist_response.text:
                add_log(f"Plex watchlist read response: {watchlist_response.text[:200]}", 'warning')
            add_log("Plex account and Discover checks passed; continuing sync", 'warning')
            return True

        data = watchlist_response.json()
        if 'MediaContainer' not in data:
            add_log("Plex watchlist read returned an unexpected response", 'warning')
            add_log("Plex account and Discover checks passed; continuing sync", 'warning')
            return True

        add_log("Plex watchlist access check passed", 'success')
        return True

    except Exception as e:
        add_log(f"Plex watchlist access check failed: {str(e)}", 'error')
        return False

def search_and_verify_plex(imdb_id, title, year, plex_token):
    """Search Plex and verify IMDB ID matches"""
    try:
        headers = plex_headers(plex_token)
        
        search_url = "https://discover.provider.plex.tv/library/search"
        
        search_queries = [
            f"{title} {year}" if year else title,
            title
        ]
        
        for search_query in search_queries:
            params = {
                'query': search_query,
                'limit': 20,
                'searchTypes': 'movies,tv',
                'includeMetadata': 1,
                'searchProviders': 'discover,plexAVOD'
            }
            
            response = plex_request('GET', search_url, headers=headers, params=params)
            
            if response.status_code != 200:
                continue
            
            data = response.json()
            search_results = data.get('MediaContainer', {}).get('SearchResults', [])
            
            for result_group in search_results:
                if 'SearchResult' not in result_group:
                    continue
                    
                for item in result_group['SearchResult']:
                    metadata = item.get('Metadata', {})
                    if not metadata:
                        continue
                    
                    rating_key = metadata.get('ratingKey')
                    if not rating_key:
                        continue
                    
                    full_metadata = get_plex_metadata_by_rating_key(rating_key, plex_token)
                    if full_metadata and extract_imdb_id_from_plex_metadata(full_metadata) == imdb_id:
                        found_title = full_metadata.get('title', '')
                        found_year = full_metadata.get('year', '')
                        action_key = plex_watchlist_action_key(full_metadata, rating_key)
                        add_log(f"✓ MATCH: '{found_title}' ({found_year})", 'success')
                        return action_key, found_title
        
        return None, None
        
    except Exception as e:
        add_log(f"Error searching Plex: {str(e)}", 'error')
        return None, None

def add_to_plex_watchlist(imdb_id, title, year, plex_token):
    """Add item to Plex watchlist"""
    try:
        rating_key, verified_title = search_and_verify_plex(imdb_id, title, year, plex_token)
        
        if not rating_key:
            return False
        
        headers = plex_headers(plex_token)
        
        watchlist_url = f"https://discover.provider.plex.tv/actions/addToWatchlist"
        params = plex_action_params(rating_key, plex_token)
        
        response = plex_request('PUT', watchlist_url, headers=headers, params=params)
        
        if response.status_code in [200, 204]:
            add_log(f"✓ Added '{verified_title}'", 'success')
            return True

        add_log(
            f"Failed to add '{title}': HTTP {response.status_code} "
            f"(Plex action key length: {len(str(rating_key))})",
            'error'
        )
        
        return False
        
    except Exception as e:
        add_log(f"Error adding to Plex: {str(e)}", 'error')
        return False

def remove_from_plex_watchlist(imdb_id, title, year, plex_token):
    """Remove item from Plex watchlist - uses same search method as add"""
    try:
        rating_key, verified_title = search_and_verify_plex(imdb_id, title, year, plex_token)
        
        if not rating_key:
            add_log(f"Could not find '{title}' in Plex library", 'info')
            return False
        
        headers = plex_headers(plex_token)
        
        # Note: Plex uses PUT (not DELETE) for removeFromWatchlist
        watchlist_url = f"https://discover.provider.plex.tv/actions/removeFromWatchlist"
        params = plex_action_params(rating_key, plex_token)
        
        response = plex_request('PUT', watchlist_url, headers=headers, params=params)
        
        if response.status_code in [200, 204]:
            add_log(f"✓ Removed '{verified_title}' from Plex watchlist", 'success')
            return True
        elif response.status_code == 404:
            # Item exists in Plex but not on watchlist - this is fine
            add_log(f"'{verified_title}' not on Plex watchlist (was never added)", 'info')
            return False
        else:
            add_log(
                f"Failed to remove '{title}': HTTP {response.status_code} "
                f"(Plex action key length: {len(str(rating_key))})",
                'error'
            )
            return False
        
    except Exception as e:
        add_log(f"Error removing from Plex: {str(e)}", 'error')
        return False

def get_plex_watchlist(plex_token):
    """Get all items currently in Plex watchlist"""
    try:
        headers = plex_headers(plex_token)
        
        # Get watchlist from Plex (discover endpoint is correct)
        watchlist_url = "https://discover.provider.plex.tv/library/sections/watchlist/all"
        add_log(f"Fetching Plex watchlist from: {watchlist_url}", 'info')
        
        # Fetch all pages (Plex paginates results)
        all_items = []
        offset = 0
        page_size = 50
        
        while True:
            params = {
                'includeCollections': 1,
                'includeExternalMedia': 1,
                'X-Plex-Container-Start': offset,
                'X-Plex-Container-Size': page_size
            }
            
            response = plex_request('GET', watchlist_url, headers=headers, params=params)
            
            add_log(f"Plex watchlist response: {response.status_code}", 'info')
            
            if response.status_code != 200:
                add_log(f"Failed to fetch Plex watchlist: {response.status_code}", 'error')
                add_log(f"Response: {response.text[:200]}", 'error')
                break
            
            data = response.json()
            
            if 'MediaContainer' not in data:
                add_log("No MediaContainer in response", 'warning')
                break
            
            container = data['MediaContainer']
            total_size = container.get('totalSize', 0)
            current_size = container.get('size', 0)
            
            add_log(f"Page: offset={offset}, size={current_size}, total={total_size}", 'info')
            
            if 'Metadata' not in container or not container['Metadata']:
                break
            
            # Process items from this page
            for item in container['Metadata']:
                imdb_id = extract_imdb_id_from_plex_metadata(item)
                full_item = item

                if not imdb_id and item.get('ratingKey'):
                    full_metadata = get_plex_metadata_by_rating_key(item.get('ratingKey'), plex_token)
                    if full_metadata:
                        full_item = full_metadata
                        imdb_id = extract_imdb_id_from_plex_metadata(full_metadata)
                
                if imdb_id:
                    all_items.append({
                        'imdb_id': imdb_id,
                        'title': full_item.get('title') or item.get('title'),
                        'year': full_item.get('year') or item.get('year'),
                        'rating_key': full_item.get('ratingKey') or item.get('ratingKey')
                    })
                    add_log(f"Found in Plex watchlist: {item.get('title')} ({item.get('year')}) - IMDB: {imdb_id}", 'info')
                else:
                    # No IMDB ID but still store it (we can match by title later if needed)
                    all_items.append({
                        'imdb_id': None,
                        'title': item.get('title'),
                        'year': item.get('year'),
                        'rating_key': item.get('ratingKey')
                    })
                    add_log(f"Found in Plex watchlist: {item.get('title')} ({item.get('year')}) - No IMDB ID, guid: {item.get('guid', 'none')}", 'info')
            
            # Check if we've got all items
            offset += current_size
            if offset >= total_size:
                break
        
        with_imdb_id = len([item for item in all_items if item.get('imdb_id')])
        add_log(f"Total Plex watchlist items: {len(all_items)} ({with_imdb_id} with IMDB IDs)", 'info')
        return all_items
        
    except Exception as e:
        add_log(f"Error fetching Plex watchlist: {str(e)}", 'error')
        import traceback
        add_log(f"Traceback: {traceback.format_exc()}", 'error')
        return []

def cleanup_unlisted_plex_watchlist():
    """Remove Plex watchlist items that are no longer present in the IMDB source list."""
    config = load_config()

    if not config.get('cleanupUnlistedEnabled'):
        add_log("Weekly cleanup skipped because it is disabled", 'info')
        return

    if config.get('listSource', 'imdb') != 'imdb':
        add_log("Cleanup skipped because the configured source is not IMDB", 'warning')
        return

    if not config.get('plexToken') or not config.get('imdbListUrl'):
        add_log("Cleanup skipped. Plex Token and IMDB List URL are required.", 'error')
        return

    add_log("=" * 50, 'info')
    add_log("Starting Plex cleanup for items not in IMDB watchlist", 'info')
    add_log("=" * 50, 'info')

    if not check_plex_watchlist_access(config['plexToken']):
        add_log("Cleanup aborted because Plex watchlist access is not available", 'error')
        return

    imdb_items = get_imdb_watchlist(config['imdbListUrl'])
    imdb_ids = {item.get('imdb_id') for item in imdb_items if item.get('imdb_id')}
    imdb_title_keys = {
        item_title_year_key(item)
        for item in imdb_items
        if item_title_year_key(item)
    }

    if not imdb_ids:
        add_log("Cleanup aborted because no IMDB source items were found", 'error')
        return

    plex_items = get_plex_watchlist(config['plexToken'])
    if not plex_items:
        add_log("Cleanup found no readable Plex watchlist items", 'warning')
        return

    stale_items = [
        item for item in plex_items
        if not plex_item_exists_in_source(item, imdb_ids, imdb_title_keys)
    ]
    unknown_id_count = len([item for item in plex_items if not item.get('imdb_id')])
    matched_by_id_count = len([
        item for item in plex_items
        if item.get('imdb_id') and item.get('imdb_id') in imdb_ids
    ])
    matched_by_title_count = len([
        item for item in plex_items
        if not (item.get('imdb_id') and item.get('imdb_id') in imdb_ids)
        and item_title_year_key(item)
        and item_title_year_key(item) in imdb_title_keys
    ])

    add_log(
        f"Cleanup comparison: {len(imdb_ids)} IMDB IDs, {len(imdb_title_keys)} IMDB title keys, "
        f"{len(plex_items)} Plex items, {len(stale_items)} stale items",
        'info'
    )
    add_log(
        f"Cleanup matches: {matched_by_id_count} by IMDB ID, "
        f"{matched_by_title_count} by title/year",
        'info'
    )
    if unknown_id_count:
        add_log(f"Cleanup skipped {unknown_id_count} Plex items without IMDB IDs", 'warning')
    if stale_items:
        preview = ", ".join((item.get('title') or item.get('imdb_id')) for item in stale_items[:10])
        add_log(f"Cleanup stale item preview: {preview}", 'warning')
    else:
        add_log("Cleanup found no Plex items with IMDB IDs missing from the IMDB source", 'info')

    removed = 0
    failed = 0

    for index, item in enumerate(stale_items, start=1):
        title = item.get('title') or item.get('imdb_id')
        year = item.get('year')
        imdb_id = item.get('imdb_id')

        add_log(f"[cleanup {index}/{len(stale_items)}] Removing '{title}' not found in IMDB source", 'warning')
        if remove_from_plex_watchlist(imdb_id, title, year, config['plexToken']):
            removed += 1
        else:
            failed += 1
        time.sleep(1.0)

    stats = load_sync_stats()
    stats.update({
        'last_cleanup': datetime.now().isoformat(),
        'cleanup_removed': removed,
    })
    save_sync_stats(stats)

    add_log("=" * 50, 'info')
    add_log(f"Cleanup complete: {removed} removed, {failed} failed", 'success')
    add_log("=" * 50, 'info')

def sync_watchlist():
    config = load_config()

    list_source = config.get('listSource', 'imdb')

    # Validate required fields
    if not config.get('plexToken') or not config.get('tmdbApiKey'):
        add_log("Configuration incomplete. Plex Token and TMDB API Key are required.", 'error')
        return

    if list_source == 'imdb' and not config.get('imdbListUrl'):
        add_log("IMDB List URL is required for IMDB source.", 'error')
        return
    if list_source == 'tmdb' and not config.get('tmdbListId'):
        add_log("TMDB List ID is required for TMDB source.", 'error')
        return
    if list_source == 'tmdb_watchlist' and not (config.get('tmdbAccountId') and config.get('tmdbSessionId')):
        add_log("TMDB Account ID and Session ID are required for TMDB Watchlist source.", 'error')
        return
    if list_source == 'trakt' and not (config.get('traktListUrl') and config.get('traktApiKey')):
        add_log("Trakt List URL and Trakt API Key are required for Trakt source.", 'error')
        return

    add_log("=" * 50, 'info')
    add_log("Starting sync", 'info')
    add_log(f"List source: {list_source}", 'info')
    add_log("=" * 50, 'info')

    if not check_plex_watchlist_access(config['plexToken']):
        add_log("Sync aborted because Plex watchlist access is not available", 'error')
        return

    # Step 1: Fetch items from the configured source
    if list_source == 'imdb':
        add_log(f"IMDB List URL: {config['imdbListUrl']}", 'info')
        items = get_imdb_watchlist(config['imdbListUrl'])
        if not items:
            add_log("No items found in IMDB watchlist", 'warning')
            return
        add_log(f"Found {len(items)} items in IMDB watchlist", 'info')

    elif list_source == 'tmdb':
        add_log(f"TMDB List ID: {config['tmdbListId']}", 'info')
        items = get_tmdb_list(config['tmdbListId'], config['tmdbApiKey'], config.get('tmdbSessionId'))
        if not items:
            add_log("No items found in TMDB list. Check that the list is public.", 'warning')
            return
        add_log(f"Found {len(items)} items in TMDB list", 'info')

    elif list_source == 'tmdb_watchlist':
        add_log(f"TMDB Account Watchlist (account: {config['tmdbAccountId']})", 'info')
        items = get_tmdb_watchlist(config['tmdbAccountId'], config['tmdbSessionId'], config['tmdbApiKey'])
        if not items:
            add_log("No items found in TMDB watchlist. Check Account ID and Session ID.", 'warning')
            return
        add_log(f"Found {len(items)} items in TMDB watchlist", 'info')

    elif list_source == 'trakt':
        add_log(f"Trakt List URL: {config['traktListUrl']}", 'info')
        items = get_trakt_list(config['traktListUrl'], config['traktApiKey'], config.get('traktAccessToken'))
        if not items:
            add_log("No items found in Trakt list. Check that the list is public and API key is valid.", 'warning')
            return
        add_log(f"Found {len(items)} items in Trakt list", 'info')

    else:
        add_log(f"Unknown list source: {list_source}", 'error')
        return

    # Step 2: Process each item
    processed = 0
    added = 0
    skipped = 0
    removed = 0
    results = []

    for item in items:
        processed += 1

        result = {
            'imdb_id': item.get('imdb_id'),
            'original_title': item['title'],
            'title': None,
            'year': None,
            'status': 'processing',
            'streaming_services': [],
            'error': None
        }

        # Resolve TMDB ID and media type
        tmdb_id = item.get('tmdb_id')
        media_type = item.get('media_type')
        title = item.get('title', '')
        year = item.get('year', '')
        imdb_id = item.get('imdb_id')

        if tmdb_id and media_type:
            # TMDB/Trakt items already have this info
            if media_type not in ('movie', 'tv'):
                media_type = 'movie'
        else:
            # IMDB source: look up via TMDB find endpoint
            tmdb_id, media_type, title, year = get_tmdb_data(imdb_id, config['tmdbApiKey'])
            if not tmdb_id:
                result['status'] = 'failed'
                result['error'] = 'Not in TMDB'
                results.append(result)
                continue

        result['title'] = title
        result['year'] = year

        add_log(f"[{processed}/{len(items)}] {title} ({year})", 'info')

        # Check if available on streaming
        is_available, providers = check_streaming_availability(
            tmdb_id,
            media_type,
            config['tmdbApiKey'],
            config['streamingServices']
        )

        # For Plex we need an IMDB ID; fetch it from TMDB if missing
        if not imdb_id:
            try:
                ext_url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/external_ids"
                ext_resp = requests.get(ext_url, params={'api_key': config['tmdbApiKey']}, timeout=10)
                ext_resp.raise_for_status()
                imdb_id = ext_resp.json().get('imdb_id')
                result['imdb_id'] = imdb_id
            except Exception as e:
                add_log(f"Could not fetch external IDs for TMDB {tmdb_id}: {str(e)}", 'warning')

        if not imdb_id:
            result['status'] = 'failed'
            result['error'] = 'No IMDB ID available'
            results.append(result)
            continue

        if is_available:
            # ON STREAMING
            result['streaming_services'] = providers
            add_log(f"  Available on {', '.join(providers)}", 'warning')

            add_log(f"  🗑️  Attempting to remove from Plex watchlist", 'warning')
            if remove_from_plex_watchlist(imdb_id, title, year, config['plexToken']):
                removed += 1
                result['status'] = 'removed'
            else:
                skipped += 1
                result['status'] = 'skipped'
                add_log(f"  ⏭️  Skipped (not in Plex or couldn't remove)", 'info')
        else:
            # NOT ON STREAMING
            add_log(f"  Not on streaming services", 'info')
            add_log(f"  ➕ Adding to Plex watchlist", 'success')
            if add_to_plex_watchlist(imdb_id, title, year, config['plexToken']):
                added += 1
                result['status'] = 'added'
            else:
                result['status'] = 'failed'
                result['error'] = 'Not in Plex or no IMDB match'

        results.append(result)
        time.sleep(1.0)

    save_sync_results(results)
    save_sync_stats({
        'removed': removed,
        'last_sync': datetime.now().isoformat()
    })

    add_log("=" * 50, 'info')
    add_log(f"Complete: {processed} processed, {added} added, {skipped} skipped, {removed} removed", 'success')
    add_log("=" * 50, 'info')

def schedule_sync():
    schedule.every(6).hours.do(sync_watchlist)
    schedule.every().sunday.at("03:00").do(cleanup_unlisted_plex_watchlist)
    
    while True:
        schedule.run_pending()
        time.sleep(60)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(load_config())

@app.route('/api/config', methods=['POST'])
def update_config():
    config = request.json
    save_config(config)
    add_log("Configuration updated", 'success')
    return jsonify({'success': True})

@app.route('/api/tmdb/account', methods=['GET'])
def get_tmdb_account():
    session_id = request.args.get('session_id', '').strip()
    config = load_config()
    api_key = config.get('tmdbApiKey', '')

    if not session_id:
        return jsonify({'error': 'session_id is required'}), 400
    if not api_key:
        return jsonify({'error': 'TMDB API Key not configured — save it in the Plex & TMDB tab first'}), 400

    try:
        resp = requests.get('https://api.themoviedb.org/3/account',
                            params={'api_key': api_key, 'session_id': session_id}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return jsonify({'account_id': data.get('id'), 'username': data.get('username', '')})
    except requests.exceptions.HTTPError as e:
        return jsonify({'error': f'TMDB error: {e.response.status_code}'}), e.response.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trakt/device-code', methods=['POST'])
def trakt_device_code():
    config = load_config()
    client_id = config.get('traktApiKey', '')
    if not client_id:
        return jsonify({'error': 'Trakt Client ID not configured — save it first'}), 400
    try:
        resp = requests.post('https://api.trakt.tv/oauth/device/code',
                             json={'client_id': client_id}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return jsonify({
            'device_code': data.get('device_code'),
            'user_code': data.get('user_code'),
            'verification_url': data.get('verification_url'),
            'expires_in': data.get('expires_in'),
            'interval': data.get('interval'),
        })
    except requests.exceptions.HTTPError as e:
        return jsonify({'error': f'Trakt error: {e.response.status_code}'}), e.response.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trakt/device-token', methods=['POST'])
def trakt_device_token():
    config = load_config()
    client_id = config.get('traktApiKey', '')
    body = request.json or {}
    client_secret = body.get('client_secret', '').strip() or config.get('traktClientSecret', '')
    device_code = body.get('device_code', '').strip()
    if not client_id or not client_secret or not device_code:
        return jsonify({'error': 'client_id, client_secret and device_code are required'}), 422
    try:
        resp = requests.post('https://api.trakt.tv/oauth/device/token',
                             json={'code': device_code, 'client_id': client_id, 'client_secret': client_secret},
                             timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            config['traktAccessToken'] = data.get('access_token')
            config['traktRefreshToken'] = data.get('refresh_token')
            save_config(config)
            add_log("Trakt OAuth access token obtained and saved", 'success')
            return jsonify({'status': 'success'})
        elif resp.status_code == 400:
            return jsonify({'status': 'pending'}), 400
        elif resp.status_code == 418:
            return jsonify({'status': 'denied', 'error': 'You denied the authorization request'}), 418
        elif resp.status_code == 410:
            return jsonify({'status': 'expired', 'error': 'Device code expired — restart the flow'}), 410
        elif resp.status_code == 429:
            return jsonify({'status': 'slow_down'}), 429
        else:
            return jsonify({'error': f'Trakt error: {resp.status_code}'}), resp.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/tmdb/token', methods=['GET'])
def get_tmdb_token():
    config = load_config()
    api_key = config.get('tmdbApiKey', '')
    if not api_key:
        return jsonify({'error': 'TMDB API Key not configured — save it in the Plex & TMDB tab first'}), 400
    try:
        resp = requests.get('https://api.themoviedb.org/3/authentication/token/new',
                            params={'api_key': api_key}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        request_token = data.get('request_token')
        return jsonify({
            'request_token': request_token,
            'expires_at': data.get('expires_at'),
            'approve_url': f"https://www.themoviedb.org/authenticate/{request_token}"
        })
    except requests.exceptions.HTTPError as e:
        return jsonify({'error': f'TMDB error: {e.response.status_code}'}), e.response.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/tmdb/session', methods=['POST'])
def create_tmdb_session():
    config = load_config()
    api_key = config.get('tmdbApiKey', '')
    request_token = (request.json or {}).get('request_token', '').strip()
    if not api_key:
        return jsonify({'error': 'TMDB API Key not configured'}), 400
    if not request_token:
        return jsonify({'error': 'request_token is required'}), 400
    try:
        resp = requests.post('https://api.themoviedb.org/3/authentication/session/new',
                             params={'api_key': api_key},
                             json={'request_token': request_token}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        session_id = data.get('session_id')
        config['tmdbSessionId'] = session_id
        save_config(config)
        add_log("TMDB session created and saved", 'success')
        return jsonify({'session_id': session_id})
    except requests.exceptions.HTTPError as e:
        return jsonify({'error': f'TMDB error: {e.response.status_code} — did you approve the token at TMDB?'}), e.response.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/logs', methods=['GET'])
def get_logs():
    if os.path.exists(LOGS_FILE):
        with open(LOGS_FILE, 'r') as f:
            return jsonify(json.load(f))
    return jsonify([])

@app.route('/api/results', methods=['GET'])
def get_results():
    return jsonify(load_sync_results())

@app.route('/api/version', methods=['GET'])
def get_version():
    return jsonify({
        'version': get_app_version(),
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/sync', methods=['POST'])
def trigger_sync():
    threading.Thread(target=sync_watchlist, daemon=True).start()
    return jsonify({'success': True, 'message': 'Sync started'})

@app.route('/api/cleanup', methods=['POST'])
def trigger_cleanup():
    threading.Thread(target=cleanup_unlisted_plex_watchlist, daemon=True).start()
    return jsonify({'success': True, 'message': 'Cleanup started'})

@app.route('/api/status', methods=['GET'])
def get_status():
    results = load_sync_results()
    stats = load_sync_stats()
    
    if not results:
        return jsonify({
            'lastSync': stats.get('last_sync'),
            'status': 'idle',
            'processed': 0,
            'added': 0,
            'skipped': 0,
            'removed': stats.get('removed', 0)
        })
    
    added = len([r for r in results if r['status'] == 'added'])
    skipped = len([r for r in results if r['status'] == 'skipped'])
    removed = len([r for r in results if r['status'] == 'removed'])

    return jsonify({
        'lastSync': stats.get('last_sync', datetime.now().isoformat()),
        'status': 'completed',
        'processed': len(results),
        'added': added,
        'skipped': skipped,
        'removed': removed
    })

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for Docker"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat()
    }), 200

if __name__ == '__main__':
    add_log(f"Application starting (version {get_app_version()})", 'info')
    threading.Thread(target=schedule_sync, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)
