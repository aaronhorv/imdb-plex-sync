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
STATS_FILE = '/config/sync_stats.json'

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
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        
        # Try both URLs with JSON extraction
        watchlist_url = f"https://www.imdb.com/user/{user_id}/watchlist"
        
        add_log(f"Fetching watchlist page for {user_id}...", 'info')
        response = session.get(watchlist_url, headers=headers, timeout=20)
        
        if response.status_code != 200:
            add_log(f"Failed to fetch watchlist: {response.status_code}", 'error')
            return []
        
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
    """Get IMDB list data directly using list ID - JSON extraction gets ALL items!"""
    try:
        session = requests.Session()
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        
        add_log(f"Using list ID: {list_id}", 'info')
        
        # JSON EXTRACTION - Gets ALL items in one request!
        add_log(f"Attempting JSON extraction from list page...", 'info')
        list_url = f"https://www.imdb.com/list/{list_id}/"
        
        try:
            response = session.get(list_url, headers=headers, timeout=20)
            
            if response.status_code == 200:
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

def remove_from_plex_watchlist(imdb_id, title, year, plex_token):
    """Remove item from Plex watchlist"""
    try:
        rating_key, verified_title = search_and_verify_plex(imdb_id, title, year, plex_token)
        
        if not rating_key:
            return False
        
        headers = {
            'X-Plex-Token': plex_token,
            'Accept': 'application/json'
        }
        
        watchlist_url = f"https://discover.provider.plex.tv/actions/removeFromWatchlist"
        params = {'ratingKey': rating_key}
        
        response = requests.delete(watchlist_url, headers=headers, params=params, timeout=10)
        
        if response.status_code in [200, 204]:
            add_log(f"✓ Removed '{verified_title}' from Plex watchlist", 'success')
            return True
        
        return False
        
    except Exception as e:
        add_log(f"Error removing from Plex: {str(e)}", 'error')
        return False

def get_plex_watchlist(plex_token):
    """Get all items currently in Plex watchlist"""
    try:
        headers = {
            'X-Plex-Token': plex_token,
            'Accept': 'application/json'
        }
        
        # Get watchlist from Plex
        watchlist_url = "https://discover.provider.plex.tv/library/sections/watchlist/all"
        response = requests.get(watchlist_url, headers=headers, timeout=10)
        
        if response.status_code != 200:
            add_log(f"Failed to fetch Plex watchlist: {response.status_code}", 'error')
            return []
        
        data = response.json()
        items = []
        
        # Extract IMDB IDs from watchlist items
        if 'MediaContainer' in data and 'Metadata' in data['MediaContainer']:
            for item in data['MediaContainer']['Metadata']:
                # Try to find IMDB ID in the guids
                imdb_id = None
                if 'Guid' in item:
                    for guid in item['Guid']:
                        guid_id = guid.get('id', '')
                        if 'imdb://' in guid_id:
                            imdb_id = guid_id.replace('imdb://', '')
                            break
                
                if imdb_id:
                    items.append({
                        'imdb_id': imdb_id,
                        'title': item.get('title'),
                        'year': item.get('year'),
                        'rating_key': item.get('ratingKey')
                    })
        
        return items
        
    except Exception as e:
        add_log(f"Error fetching Plex watchlist: {str(e)}", 'error')
        return []

def sync_watchlist():
    config = load_config()
    
    if not all([config.get('imdbListUrl'), config.get('plexToken'), config.get('tmdbApiKey')]):
        add_log("Configuration incomplete", 'error')
        return
    
    add_log("=" * 50, 'info')
    add_log("Starting sync", 'info')
    add_log("=" * 50, 'info')
    
    # Step 1: Get IMDB watchlist
    items = get_imdb_watchlist(config['imdbListUrl'])
    
    if not items:
        add_log("No items found in watchlist", 'warning')
        return
    
    add_log(f"Found {len(items)} items in IMDB watchlist", 'info')
    
    # Step 2: Get current Plex watchlist for cleanup
    add_log("Fetching Plex watchlist for cleanup...", 'info')
    plex_items = get_plex_watchlist(config['plexToken'])
    add_log(f"Found {len(plex_items)} items in Plex watchlist", 'info')
    
    # Step 3: Check Plex watchlist items for streaming availability and remove if found
    removed = 0
    for plex_item in plex_items:
        # Get TMDB data for the Plex item
        tmdb_id, media_type, title, year = get_tmdb_data(plex_item['imdb_id'], config['tmdbApiKey'])
        
        if not tmdb_id:
            continue
        
        # Check if it's now available on streaming
        is_available, providers = check_streaming_availability(
            tmdb_id,
            media_type,
            config['tmdbApiKey'],
            config['streamingServices']
        )
        
        if is_available:
            add_log(f"🗑️  '{plex_item['title']}' now on {', '.join(providers)} - removing from Plex", 'warning')
            if remove_from_plex_watchlist(plex_item['imdb_id'], plex_item['title'], plex_item['year'], config['plexToken']):
                removed += 1
    
    if removed > 0:
        add_log(f"Removed {removed} items from Plex watchlist (now on streaming)", 'info')
    
    # Step 4: Process IMDB watchlist items
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
    save_sync_stats({
        'removed': removed,
        'last_sync': datetime.now().isoformat()
    })
    
    add_log("=" * 50, 'info')
    add_log(f"Complete: {processed} processed, {added} added, {skipped} skipped, {removed} removed", 'success')
    add_log("=" * 50, 'info')
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
    
    return jsonify({
        'lastSync': stats.get('last_sync', datetime.now().isoformat()),
        'status': 'completed',
        'processed': len(results),
        'added': added,
        'skipped': skipped,
        'removed': stats.get('removed', 0)
    })

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for Docker"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat()
    }), 200

if __name__ == '__main__':
    add_log("Application starting", 'info')
    threading.Thread(target=schedule_sync, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)
