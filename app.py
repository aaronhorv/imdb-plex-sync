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
        'region': 'US',
        'streamingServices': []
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

def get_imdb_export_data(user_id):
    """Get IMDB watchlist data using web scraping"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': f'https://www.imdb.com/user/{user_id}/watchlist'
        }
        
        session = requests.Session()
        watchlist_url = f"https://www.imdb.com/user/{user_id}/watchlist"
        
        add_log(f"Fetching watchlist page: {watchlist_url}", 'info')
        response = session.get(watchlist_url, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        list_id = None
        list_elements = soup.find_all(attrs={'data-list-id': True})
        if list_elements:
            list_id = list_elements[0].get('data-list-id')
            add_log(f"Found list ID in data attribute: {list_id}", 'info')
        
        if not list_id:
            export_link = soup.find('a', href=re.compile(r'/list/export'))
            if export_link:
                href = export_link.get('href', '')
                match = re.search(r'list_id=(ls\d+)', href)
                if match:
                    list_id = match.group(1)
                    add_log(f"Found list ID in export link: {list_id}", 'info')
        
        if not list_id:
            scripts = soup.find_all('script')
            for script in scripts:
                if script.string:
                    match = re.search(r'"list":\s*{\s*"id":\s*"(ls\d+)"', script.string)
                    if match:
                        list_id = match.group(1)
                        add_log(f"Found list ID in script: {list_id}", 'info')
                        break
        
        if list_id:
            export_url = f"https://www.imdb.com/list/{list_id}/export"
            add_log(f"Attempting CSV export from: {export_url}", 'info')
            
            response = session.get(export_url, headers=headers, timeout=15)
            if response.status_code == 200:
                return parse_csv_export(response.text)
        
        add_log("CSV export not available, falling back to page scraping", 'warning')
        return scrape_watchlist_page(soup, watchlist_url)
        
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
                add_log(f"Found from CSV: {title} ({imdb_id})", 'info')
    
    return items

def scrape_watchlist_page(soup, url):
    """Scrape watchlist page for IMDB IDs"""
    items = []
    seen_ids = set()
    
    title_links = soup.find_all('a', href=re.compile(r'/title/tt\d+'))
    
    for link in title_links:
        href = link.get('href', '')
        imdb_match = re.search(r'/title/(tt\d+)', href)
        
        if imdb_match:
            imdb_id = imdb_match.group(1)
            
            if imdb_id in seen_ids:
                continue
            seen_ids.add(imdb_id)
            
            title = link.get_text(strip=True)
            if not title or len(title) < 2:
                title = f"IMDB:{imdb_id}"
            
            items.append({
                'title': title,
                'imdb_id': imdb_id,
                'link': f"https://www.imdb.com/title/{imdb_id}/"
            })
            add_log(f"Found from page: {title} ({imdb_id})", 'info')
    
    return items

def get_imdb_watchlist(list_url):
    """Main function to get IMDB watchlist items"""
    try:
        user_id = extract_user_id(list_url)
        
        if user_id:
            add_log(f"Detected personal watchlist for user: {user_id}", 'info')
            return get_imdb_export_data(user_id)
        else:
            add_log(f"Detected custom list", 'info')
            return scrape_custom_list(list_url)
        
    except Exception as e:
        add_log(f"Error fetching IMDB watchlist: {str(e)}", 'error')
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

def check_streaming_availability(tmdb_id, media_type, api_key, region, provider_ids):
    try:
        url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/watch/providers"
        params = {'api_key': api_key}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if 'results' not in data or region not in data['results']:
            return False, []
        
        region_data = data['results'][region]
        available_providers = []
        
        if 'flatrate' in region_data:
            for provider in region_data['flatrate']:
                if provider['provider_id'] in provider_ids:
                    available_providers.append(provider['provider_name'])
        
        return len(available_providers) > 0, available_providers
    except Exception as e:
        add_log(f"Error checking streaming availability: {str(e)}", 'warning')
        return False, []

def search_plex_by_title(title, year, plex_token):
    """Search Plex discover for a movie/show by title"""
    try:
        headers = {
            'X-Plex-Token': plex_token,
            'Accept': 'application/json'
        }
        
        search_url = "https://discover.provider.plex.tv/library/search"
        
        search_query = f"{title} {year}" if year else title
        params = {
            'query': search_query,
            'limit': 10,
            'searchTypes': 'movies,tv',
            'includeMetadata': 1,
            'searchProviders': 'discover,plexAVOD'  # <-- ADD THIS LINE
        }
        
        response = requests.get(search_url, headers=headers, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            # Parse the nested SearchResults structure
            search_results = data.get('MediaContainer', {}).get('SearchResults', [])
            for result_group in search_results:
                if 'SearchResult' in result_group:
                    for item in result_group['SearchResult']:
                        metadata = item.get('Metadata', {})
                        if metadata:
                            return metadata.get('ratingKey')
        
        # Try without year if first search failed
        if year:
            params['query'] = title
            response = requests.get(search_url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                search_results = data.get('MediaContainer', {}).get('SearchResults', [])
                for result_group in search_results:
                    if 'SearchResult' in result_group:
                        for item in result_group['SearchResult']:
                            metadata = item.get('Metadata', {})
                            if metadata:
                                return metadata.get('ratingKey')
        
        return None
    except Exception as e:
        add_log(f"Error searching Plex: {str(e)}", 'warning')
        return None

def verify_plex_item_imdb(rating_key, imdb_id, plex_token):
    """Verify that a Plex item matches the IMDB ID"""
    try:
        headers = {
            'X-Plex-Token': plex_token,
            'Accept': 'application/json'
        }
        
        url = f"https://discover.provider.plex.tv/library/metadata/{rating_key}"
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            metadata = data.get('MediaContainer', {}).get('Metadata', [])
            if metadata:
                guids = metadata[0].get('Guid', [])
                for guid in guids:
                    if guid.get('id', '').endswith(imdb_id):
                        return True
        
        return False
    except Exception as e:
        add_log(f"Error verifying Plex item: {str(e)}", 'warning')
        return False

def add_to_plex_watchlist(imdb_id, title, year, plex_token):
    """Add item to Plex watchlist using the correct API"""
    try:
        rating_key = search_plex_by_title(title, year, plex_token)
        
        if not rating_key:
            add_log(f"Could not find '{title}' in Plex discover", 'warning')
            return False
        
        if not verify_plex_item_imdb(rating_key, imdb_id, plex_token):
            add_log(f"Found item but IMDB ID doesn't match for '{title}'", 'warning')
            return False
        
        headers = {
            'X-Plex-Token': plex_token,
            'Accept': 'application/json'
        }
        
        watchlist_url = f"https://discover.provider.plex.tv/actions/addToWatchlist"
        params = {'ratingKey': rating_key}
        
        response = requests.put(watchlist_url, headers=headers, params=params, timeout=10)
        
        if response.status_code in [200, 204]:
            return True
        
        add_log(f"Plex API returned status {response.status_code} for '{title}'", 'warning')
        return False
        
    except Exception as e:
        add_log(f"Error adding to Plex watchlist: {str(e)}", 'error')
        import traceback
        add_log(f"Traceback: {traceback.format_exc()}", 'error')
        return False

def sync_watchlist():
    config = load_config()
    
    if not all([config.get('imdbListUrl'), config.get('plexToken'), config.get('tmdbApiKey')]):
        add_log("Configuration incomplete. Please configure all settings.", 'error')
        return
    
    add_log("=" * 50, 'info')
    add_log("Starting sync process", 'info')
    add_log(f"IMDB List URL: {config['imdbListUrl']}", 'info')
    add_log(f"Region: {config['region']}", 'info')
    add_log(f"Streaming services: {len(config['streamingServices'])} configured", 'info')
    add_log("=" * 50, 'info')
    
    items = get_imdb_watchlist(config['imdbListUrl'])
    
    if not items:
        add_log("No items found in IMDB watchlist. Check if list is public and has items.", 'warning')
        return
    
    add_log(f"Found {len(items)} items in IMDB watchlist", 'info')
    
    processed = 0
    added = 0
    skipped = 0
    results = []
    
    for item in items:
        processed += 1
        add_log(f"Processing {processed}/{len(items)}: {item['title']}", 'info')
        
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
            result['error'] = 'Not found in TMDB'
            add_log(f"Could not find TMDB data for {item['imdb_id']}", 'warning')
            results.append(result)
            continue
        
        result['title'] = title
        result['year'] = year
        
        is_available, providers = check_streaming_availability(
            tmdb_id,
            media_type,
            config['tmdbApiKey'],
            config['region'],
            config['streamingServices']
        )
        
        if is_available:
            skipped += 1
            result['status'] = 'skipped'
            result['streaming_services'] = providers
            add_log(f"Skipped '{title}' - available on {', '.join(providers)}", 'warning')
            results.append(result)
            continue
        
        if add_to_plex_watchlist(item['imdb_id'], title, year, config['plexToken']):
            added += 1
            result['status'] = 'added'
            add_log(f"Added '{title}' to Plex watchlist", 'success')
        else:
            result['status'] = 'failed'
            result['error'] = 'Could not add to Plex'
            add_log(f"Failed to add '{title}' to Plex watchlist", 'error')
        
        results.append(result)
        time.sleep(0.5)
    
    save_sync_results(results)
    
    add_log("=" * 50, 'info')
    add_log(f"Sync complete: {processed} processed, {added} added, {skipped} skipped", 'success')
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
    add_log("Application starting...", 'info')
    threading.Thread(target=schedule_sync, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)
