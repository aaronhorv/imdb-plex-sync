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

app = Flask(__name__)

CONFIG_FILE = '/config/config.json'
LOGS_FILE = '/config/logs.json'
RESULTS_FILE = '/config/sync_results.json'

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {
        'imdbListUrl': '',
        'plexToken': '',
        'tmdbApiKey': '',
        'streamingServices': []  # Now stores [{"id": 8, "region": "DE"}, ...]
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

def scrape_watchlist_page(soup, url):
    """Scrape watchlist page for IMDB IDs - improved for modern IMDB"""
    items = []
    seen_ids = set()
    
    # Method 1: Find all title links (most reliable)
    title_links = soup.find_all('a', href=re.compile(r'/title/tt\d+'))
    add_log(f"DEBUG: Found {len(title_links)} title links on page", 'info')
    
    for link in title_links:
        href = link.get('href', '')
        imdb_match = re.search(r'/title/(tt\d+)', href)
        
        if imdb_match:
            imdb_id = imdb_match.group(1)
            
            if imdb_id in seen_ids:
                continue
            seen_ids.add(imdb_id)
            
            # Try to get title text from the link or nearby elements
            title = link.get_text(strip=True)
            
            # If link text is empty, look for title in parent elements
            if not title or len(title) < 2:
                parent = link.parent
                if parent:
                    # Look for h3 or other heading elements
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
    
    add_log(f"DEBUG: Extracted {len(items)} unique items from title links", 'info')
    
    # Method 2: Look for JSON-LD structured data (backup method)
    if not items:
        add_log("DEBUG: No items from title links, trying JSON-LD", 'info')
        scripts = soup.find_all('script', type='application/ld+json')
        add_log(f"DEBUG: Found {len(scripts)} JSON-LD scripts", 'info')
        
        for script in scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, list):
                    for item in data:
                        if item.get('@type') in ['Movie', 'TVSeries']:
                            url = item.get('url', '')
                            imdb_match = re.search(r'/title/(tt\d+)', url)
                            if imdb_match:
                                imdb_id = imdb_match.group(1)
                                if imdb_id not in seen_ids:
                                    seen_ids.add(imdb_id)
                                    items.append({
                                        'title': item.get('name', f"IMDB:{imdb_id}"),
                                        'imdb_id': imdb_id,
                                        'link': f"https://www.imdb.com/title/{imdb_id}/"
                                    })
            except Exception as e:
                continue
        
        add_log(f"DEBUG: Extracted {len(items)} items from JSON-LD", 'info')
    
    return items

def get_imdb_export_data(user_id):
    """Get IMDB watchlist data - optimized for 248+ items"""
    try:
        session = requests.Session()
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        
        # Step 1: Get the watchlist page to find the list ID
        watchlist_url = f"https://www.imdb.com/user/{user_id}/watchlist"
        add_log(f"Fetching watchlist page for {user_id}...", 'info')
        
        response = session.get(watchlist_url, headers=headers, timeout=20)
        if response.status_code != 200:
            add_log(f"Failed to fetch watchlist: {response.status_code}", 'error')
            return []
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Step 2: Find the list ID (required for all methods)
        list_id = None
        
        # Method A: Look in data attributes
        for element in soup.find_all(True):
            if element.has_attr('data-list-id'):
                list_id = element['data-list-id']
                add_log(f"Found list ID in data attribute: {list_id}", 'info')
                break
        
        # Method B: Look in all script tags
        if not list_id:
            scripts = soup.find_all('script')
            add_log(f"Searching {len(scripts)} script tags for list ID...", 'info')
            
            for script in scripts:
                script_content = script.string if script.string else ""
                
                # Try multiple patterns
                patterns = [
                    r'"watchlistId":"(ls\d+)"',
                    r'"listId":"(ls\d+)"',
                    r'"list":.*?"id":"(ls\d+)"',
                    r'data-list-id="(ls\d+)"',
                    r'/list/(ls\d+)',
                    r'"id":"(ls\d{8,})"',  # List IDs are typically 8+ digits
                ]
                
                for pattern in patterns:
                    match = re.search(pattern, script_content)
                    if match:
                        potential_id = match.group(1)
                        if potential_id.startswith('ls'):
                            list_id = potential_id
                            add_log(f"Found list ID in script: {list_id}", 'info')
                            break
                
                if list_id:
                    break
        
        # Method C: Look in the HTML for any ls#### pattern
        if not list_id:
            html_content = str(soup)
            all_ls_ids = re.findall(r'ls\d{8,}', html_content)
            if all_ls_ids:
                list_id = all_ls_ids[0]  # Take the first one found
                add_log(f"Found list ID in HTML: {list_id}", 'info')
        
        if not list_id:
            add_log("ERROR: Could not find list ID. Watchlist may be private or URL invalid.", 'error')
            return scrape_watchlist_page(soup, watchlist_url)
        
        # Step 3: Try CSV export first (gets ALL items in one request)
        add_log(f"Attempting CSV export for list {list_id}...", 'info')
        export_url = f"https://www.imdb.com/list/{list_id}/export"
        
        try:
            csv_response = session.get(export_url, headers=headers, timeout=20)
            add_log(f"CSV response: {csv_response.status_code}, {len(csv_response.text)} bytes", 'info')
            
            if csv_response.status_code == 200 and len(csv_response.text) > 200:
                csv_items = parse_csv_export(csv_response.text)
                if csv_items and len(csv_items) > 25:  # CSV should get all items
                    add_log(f"✓ CSV export successful: {len(csv_items)} items", 'success')
                    return csv_items
                else:
                    add_log(f"CSV parse failed or returned too few items: {len(csv_items)}", 'warning')
        except Exception as e:
            add_log(f"CSV export error: {e}", 'warning')
        
        # Step 4: Use the list page URL with export view (shows all items on one page)
        add_log(f"Trying list export view...", 'info')
        export_view_url = f"https://www.imdb.com/list/{list_id}/export"
        
        try:
            response = session.get(export_view_url, headers=headers, timeout=20)
            if response.status_code == 200:
                # Try to parse as CSV first
                if 'text/csv' in response.headers.get('Content-Type', ''):
                    csv_items = parse_csv_export(response.text)
                    if csv_items:
                        add_log(f"✓ Export view successful: {len(csv_items)} items", 'success')
                        return csv_items
        except Exception as e:
            add_log(f"Export view error: {e}", 'warning')
        
        # Step 5: Paginate through the list page with ?start= parameter
        # IMDB list pages support pagination with start parameter
        add_log(f"Attempting pagination on list {list_id}...", 'info')
        
        all_items = []
        seen_ids = set()
        start = 1
        max_iterations = 10  # 10 pages * 25-50 items = 250-500 items max
        
        while start < 500:  # Safety limit
            list_url = f"https://www.imdb.com/list/{list_id}/"
            params = {
                'sort': 'list_order,asc',
                'start': start,
                'view': 'detail'  # Detail view might show more items per page
            }
            
            add_log(f"Fetching list page start={start}...", 'info')
            
            try:
                page_response = session.get(list_url, params=params, headers=headers, timeout=20)
                
                if page_response.status_code != 200:
                    add_log(f"List page returned {page_response.status_code}, stopping", 'warning')
                    break
                
                page_soup = BeautifulSoup(page_response.content, 'html.parser')
                page_items = scrape_watchlist_page(page_soup, list_url)
                
                # Filter duplicates
                new_items = [item for item in page_items if item['imdb_id'] not in seen_ids]
                for item in new_items:
                    seen_ids.add(item['imdb_id'])
                
                add_log(f"  Found {len(page_items)} items, {len(new_items)} new (total: {len(all_items) + len(new_items)})", 'info')
                
                if not new_items:
                    add_log(f"No new items at start={start}, stopping pagination", 'info')
                    break
                
                all_items.extend(new_items)
                
                # Check if we've likely got everything (you have 248 items)
                if len(all_items) >= 250:
                    add_log(f"Reached 250+ items, assuming we have everything", 'info')
                    break
                
                # Move to next page (IMDB typically shows 25-50 items per page)
                start += 50
                
            except Exception as e:
                add_log(f"Error fetching list page: {e}", 'error')
                break
            
            time.sleep(0.5)  # Be nice to IMDB
        
        if all_items:
            add_log(f"✓ List pagination successful: {len(all_items)} items", 'success')
            return all_items
        
        # Step 6: Last resort - try compact mode which might show more
        add_log(f"Trying compact mode as fallback...", 'warning')
        compact_url = f"https://www.imdb.com/list/{list_id}/?view=compact&sort=list_order,asc"
        
        try:
            response = session.get(compact_url, headers=headers, timeout=20)
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, 'html.parser')
                items = scrape_watchlist_page(soup, compact_url)
                if items:
                    add_log(f"Compact mode found {len(items)} items", 'info')
                    return items
        except Exception as e:
            add_log(f"Compact mode error: {e}", 'error')
        
        # Absolute last resort
        add_log("All methods failed, returning initial scrape", 'error')
        initial_items = scrape_watchlist_page(soup, watchlist_url)
        return initial_items
        
    except Exception as e:
        add_log(f"Error in get_imdb_export_data: {str(e)}", 'error')
        import traceback
        add_log(f"Traceback: {traceback.format_exc()}", 'error')
        return []

def parse_csv_export(csv_text):
    """Parse IMDB CSV export"""
    items = []
    lines = csv_text.strip().split('\n')
    
    if len(lines) < 2:
        return items
    
    headers = lines[0].split(',')
    
    const_idx = None
    title_idx = None
    
    for i, header in enumerate(headers):
        if 'Const' in header:
            const_idx = i
        if 'Title' in header:
            title_idx = i
    
    if const_idx is None:
        add_log("Could not find 'Const' column in CSV", 'error')
        return items
    
    for line in lines[1:]:
        parts = line.split(',')
        if len(parts) > const_idx:
            imdb_id = parts[const_idx].strip('"')
            title = parts[title_idx].strip('"') if title_idx and len(parts) > title_idx else imdb_id
            
            if imdb_id.startswith('tt'):
                items.append({
                    'title': title,
                    'imdb_id': imdb_id,
                    'link': f"https://www.imdb.com/title/{imdb_id}/"
                })
    
    return items

def get_imdb_watchlist(list_url):
    """Main function to get IMDB watchlist items"""
    try:
        # Check if it's a list URL (either /list/lsXXX or /user/urXXX/watchlist)
        list_match = re.search(r'/list/(ls\d+)', list_url)
        user_match = re.search(r'user/(ur\d+)', list_url)
        
        if list_match:
            # Direct list URL provided
            list_id = list_match.group(1)
            add_log(f"Detected direct list URL with ID: {list_id}", 'info')
            return get_imdb_list_data(list_id)
        elif user_match:
            # User watchlist URL
            user_id = user_match.group(1)
            add_log(f"Detected personal watchlist for user: {user_id}", 'info')
            return get_imdb_export_data(user_id)
        else:
            add_log(f"Could not parse URL: {list_url}", 'error')
            return []
        
    except Exception as e:
        add_log(f"Error fetching IMDB watchlist: {str(e)}", 'error')
        import traceback
        add_log(f"Traceback: {traceback.format_exc()}", 'error')
        return []

def get_imdb_list_data(list_id):
    """Get IMDB list data directly using list ID - MOST RELIABLE METHOD"""
    try:
        session = requests.Session()
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        
        add_log(f"Using list ID: {list_id}", 'info')
        
        # Method 1: CSV Export (gets ALL items in one request - BEST!)
        export_url = f"https://www.imdb.com/list/{list_id}/export"
        add_log(f"Attempting CSV export: {export_url}", 'info')
        
        try:
            csv_response = session.get(export_url, headers=headers, timeout=20)
            add_log(f"CSV response: {csv_response.status_code}, {len(csv_response.text)} bytes", 'info')
            
            if csv_response.status_code == 200 and len(csv_response.text) > 200:
                csv_items = parse_csv_export(csv_response.text)
                if csv_items and len(csv_items) > 20:  # Should get all items
                    add_log(f"✓ CSV export successful: {len(csv_items)} items", 'success')
                    return csv_items
                else:
                    add_log(f"CSV parse returned only {len(csv_items)} items", 'warning')
        except Exception as e:
            add_log(f"CSV export error: {e}", 'warning')
        
        # Method 2: Paginate through list with ?start= parameter
        add_log(f"CSV failed, attempting list pagination...", 'info')
        
        all_items = []
        seen_ids = set()
        start = 1
        
        while start <= 500:  # Safety: max 10 pages * 50 items = 500 items
            list_url = f"https://www.imdb.com/list/{list_id}/"
            params = {
                'sort': 'list_order,asc',
                'start': start,
                'view': 'detail'
            }
            
            add_log(f"Fetching list page start={start}...", 'info')
            
            try:
                page_response = session.get(list_url, params=params, headers=headers, timeout=20)
                
                if page_response.status_code != 200:
                    add_log(f"List page returned {page_response.status_code}", 'warning')
                    break
                
                page_soup = BeautifulSoup(page_response.content, 'html.parser')
                page_items = scrape_watchlist_page(page_soup, list_url)
                
                # Filter duplicates
                new_items = [item for item in page_items if item['imdb_id'] not in seen_ids]
                for item in new_items:
                    seen_ids.add(item['imdb_id'])
                
                add_log(f"  start={start}: {len(page_items)} items, {len(new_items)} new (total: {len(all_items) + len(new_items)})", 'info')
                
                if not new_items:
                    add_log(f"No new items at start={start}, stopping", 'info')
                    break
                
                all_items.extend(new_items)
                
                # IMDB shows about 50 items per page on list views
                start += 50
                
                # If we got fewer than 50 items, we're probably on the last page
                if len(page_items) < 50:
                    add_log(f"Got {len(page_items)} items (less than 50), likely last page", 'info')
                    break
                
            except Exception as e:
                add_log(f"Error fetching list page: {e}", 'error')
                break
            
            time.sleep(0.5)
        
        if all_items:
            add_log(f"✓ List pagination successful: {len(all_items)} items", 'success')
            return all_items
        
        # Method 3: Try compact view as last resort
        add_log(f"Trying compact view...", 'warning')
        compact_url = f"https://www.imdb.com/list/{list_id}/?view=compact&sort=list_order,asc"
        
        try:
            response = session.get(compact_url, headers=headers, timeout=20)
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, 'html.parser')
                items = scrape_watchlist_page(soup, compact_url)
                if items:
                    add_log(f"Compact view found {len(items)} items", 'info')
                    return items
        except Exception as e:
            add_log(f"Compact mode error: {e}", 'error')
        
        add_log("All methods failed for list", 'error')
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
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        }
        
        response = requests.get(list_url, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        return scrape_watchlist_page(soup, list_url)
        
    except Exception as e:
        add_log(f"Error scraping custom list: {str(e)}", 'error')
        return []

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
        
        # Check each region
        for region, services in regions_to_check.items():
            url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/watch/providers"
            params = {'api_key': api_key}
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if 'results' not in data or region not in data['results']:
                continue
            
            region_data = data['results'][region]
            
            # Check flatrate (subscription) services
            if 'flatrate' in region_data:
                for provider in region_data['flatrate']:
                    # FIX: Ensure provider is a dictionary before accessing keys
                    if not isinstance(provider, dict):
                        add_log(f"Warning: Provider is not a dict: {provider}", 'warning')
                        continue
                    
                    provider_id = provider.get('provider_id')
                    provider_name = provider.get('provider_name', 'Unknown')
                    
                    if not provider_id:
                        continue
                    
                    # Check if this provider matches any of our configured services
                    for service in services:
                        service_id = service.get('id')
                        if provider_id == service_id:
                            service_name = f"{provider_name} ({region})"
                            if service_name not in available_services:
                                available_services.append(service_name)
        
        return len(available_services) > 0, available_services
    except Exception as e:
        add_log(f"Error checking streaming availability: {str(e)}", 'warning')
        import traceback
        add_log(f"Traceback: {traceback.format_exc()}", 'error')
        return False, []

def search_and_verify_plex(imdb_id, title, year, plex_token):
    """Search Plex and verify IMDB ID matches"""
    try:
        headers = {
            'X-Plex-Token': plex_token,
            'Accept': 'application/json'
        }
        
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
            
            response = requests.get(search_url, headers=headers, params=params, timeout=10)
            
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
                    
                    metadata_url = f"https://discover.provider.plex.tv/library/metadata/{rating_key}"
                    meta_response = requests.get(metadata_url, headers=headers, timeout=10)
                    
                    if meta_response.status_code == 200:
                        meta_data = meta_response.json()
                        full_metadata = meta_data.get('MediaContainer', {}).get('Metadata', [])
                        
                        if full_metadata:
                            guids = full_metadata[0].get('Guid', [])
                            found_title = full_metadata[0].get('title', '')
                            found_year = full_metadata[0].get('year', '')
                            
                            for guid in guids:
                                guid_id = guid.get('id', '')
                                if imdb_id in guid_id:
                                    add_log(f"✓ MATCH: '{found_title}' ({found_year})", 'success')
                                    return rating_key, found_title
        
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
        
        headers = {
            'X-Plex-Token': plex_token,
            'Accept': 'application/json'
        }
        
        watchlist_url = f"https://discover.provider.plex.tv/actions/addToWatchlist"
        params = {'ratingKey': rating_key}
        
        response = requests.put(watchlist_url, headers=headers, params=params, timeout=10)
        
        if response.status_code in [200, 204]:
            add_log(f"✓ Added '{verified_title}'", 'success')
            return True
        
        return False
        
    except Exception as e:
        add_log(f"Error adding to Plex: {str(e)}", 'error')
        return False

def sync_watchlist():
    config = load_config()
    
    if not all([config.get('imdbListUrl'), config.get('plexToken'), config.get('tmdbApiKey')]):
        add_log("Configuration incomplete", 'error')
        return
    
    add_log("=" * 50, 'info')
    add_log("Starting sync", 'info')
    add_log("=" * 50, 'info')
    
    items = get_imdb_watchlist(config['imdbListUrl'])
    
    if not items:
        add_log("No items found in watchlist", 'warning')
        return
    
    add_log(f"Found {len(items)} items total", 'info')
    
    processed = 0
    added = 0
    skipped = 0
    results = []
    
    for item in items:
        processed += 1
        
        result = {
            'imdb_id': item['imdb_id'],
            'original_title': item['title'],
            'title': None,
            'year': None,
            'status': 'processing',
            'streaming_services': [],
            'error': None
        }
        
        tmdb_id, media_type, title, year = get_tmdb_data(item['imdb_id'], config['tmdbApiKey'])
        
        if not tmdb_id:
            result['status'] = 'failed'
            result['error'] = 'Not in TMDB'
            results.append(result)
            continue
        
        result['title'] = title
        result['year'] = year
        
        add_log(f"[{processed}/{len(items)}] {title} ({year})", 'info')
        
        is_available, providers = check_streaming_availability(
            tmdb_id,
            media_type,
            config['tmdbApiKey'],
            config['streamingServices']
        )
        
        if is_available:
            skipped += 1
            result['status'] = 'skipped'
            result['streaming_services'] = providers
            add_log(f"Skipped - on {', '.join(providers)}", 'warning')
            results.append(result)
            continue
        
        if add_to_plex_watchlist(item['imdb_id'], title, year, config['plexToken']):
            added += 1
            result['status'] = 'added'
        else:
            result['status'] = 'failed'
            result['error'] = 'Not in Plex or no IMDB match'
        
        results.append(result)
        time.sleep(1.0)
    
    save_sync_results(results)
    
    add_log("=" * 50, 'info')
    add_log(f"Complete: {processed} processed, {added} added, {skipped} skipped", 'success')
    add_log("=" * 50, 'info')

def schedule_sync():
    schedule.every(6).hours.do(sync_watchlist)
    
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

@app.route('/api/logs', methods=['GET'])
def get_logs():
    if os.path.exists(LOGS_FILE):
        with open(LOGS_FILE, 'r') as f:
            return jsonify(json.load(f))
    return jsonify([])

@app.route('/api/results', methods=['GET'])
def get_results():
    return jsonify(load_sync_results())

@app.route('/api/sync', methods=['POST'])
def trigger_sync():
    threading.Thread(target=sync_watchlist, daemon=True).start()
    return jsonify({'success': True, 'message': 'Sync started'})

@app.route('/api/status', methods=['GET'])
def get_status():
    results = load_sync_results()
    
    if not results:
        return jsonify({
            'lastSync': None,
            'status': 'idle',
            'processed': 0,
            'added': 0,
            'skipped': 0
        })
    
    added = len([r for r in results if r['status'] == 'added'])
    skipped = len([r for r in results if r['status'] == 'skipped'])
    
    return jsonify({
        'lastSync': datetime.now().isoformat(),
        'status': 'completed',
        'processed': len(results),
        'added': added,
        'skipped': skipped
    })

if __name__ == '__main__':
    add_log("Application starting", 'info')
    threading.Thread(target=schedule_sync, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)
