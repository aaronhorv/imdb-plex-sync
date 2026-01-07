# IMDB Watchlist to Plex Sync - Complete Solution

🎉 **Successfully extracts all 248 items from IMDB watchlist!**

## Overview

This Docker container automatically syncs your IMDB watchlist to Plex, skipping items available on your streaming services.

### Key Features

- ✅ **Extracts all 248+ items** from IMDB watchlist (not just 25!)
- ✅ **Fast extraction** (<2 seconds instead of 10-15)
- ✅ **Preserves international characters** (German, Hungarian, etc.)
- ✅ **Single-page extraction** (no pagination needed)
- ✅ **Automatic scheduling** (syncs every 6 hours)
- ✅ **Streaming service checking** (skips items on Netflix, Disney+, etc.)
- ✅ **Web dashboard** for monitoring and configuration

## The Breakthrough: JSON Extraction

Previous version only extracted 25 items due to IMDB's lazy loading. The new version uses **JSON extraction** to get all items in one request.

### How It Works

IMDB embeds all watchlist data in JSON format within the HTML:
```json
"titleText": {"text": "Movie Title"}
```

The extraction uses two simple regex patterns:
1. Extract titles: `"titleText"[^}]*"text":"([^"]*)"`
2. Extract IDs: `/title/(tt\d+)/`
3. Match them together

Result: All 248 items in <2 seconds! 🚀

## Quick Start

### Option 1: Docker Compose (Recommended)

```bash
# Clone or copy files
cd imdb-plex-sync

# Start container
docker-compose up -d

# View logs
docker-compose logs -f
```

### Option 2: Docker Run

```bash
# Build
docker build -t imdb-plex-sync .

# Run
docker run -d \
  --name imdb-plex-sync \
  -p 5000:5000 \
  -v ./config:/config \
  --restart unless-stopped \
  imdb-plex-sync
```

## Configuration

1. **Open web interface**: `http://localhost:5000`

2. **Enter your details**:
   - IMDB List URL (either format works):
     - `https://www.imdb.com/user/ur125858502/watchlist`
     - `https://www.imdb.com/list/ls086668596/`
   - Plex Token (from [plex.tv/claim](https://plex.tv/claim))
   - TMDB API Key (from [themoviedb.org](https://www.themoviedb.org/settings/api))
   - Streaming Services (select your subscriptions)

3. **Test**: Click "Test Sync Now" to verify it finds all 248 items

## Files Included

### Core Files
- `app.py` - Main application with JSON extraction
- `Dockerfile` - Container configuration
- `docker-compose.yml` - Compose configuration
- `requirements.txt` - Python dependencies

### Test Files
- `test_json_extraction.py` - Verify extraction works
- `test_watchlist.py` - Compare all extraction methods
- `save_html.py` - Save HTML for debugging

### Documentation
- `DEPLOYMENT_GUIDE.md` - Detailed deployment instructions
- `JSON_EXTRACTION_SUMMARY.md` - Technical implementation details
- `README.md` - This file

## Testing

### Test JSON Extraction

```bash
# Run test script
python3 test_json_extraction.py
```

Expected output:
```
✅ SUCCESS! Found 248 items (expected ~248)
```

### Manual Verification with curl

```bash
# Count titles in the page
curl -L -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36" \
  "https://www.imdb.com/list/ls086668596/" | \
  grep -o '"titleText"' | wc -l

# Output: 248
```

## Performance

| Metric | Old Method | New Method |
|--------|-----------|------------|
| Items Found | 25 | 248 |
| HTTP Requests | 10+ | 1 |
| Time | 10-15 sec | <2 sec |
| Success Rate | Low | High |

## Technical Details

### Dependencies
- Python 3.11
- Flask (web interface)
- Requests (HTTP client)
- BeautifulSoup4 (HTML parsing)
- Schedule (automated syncing)
- curl (debugging tool)

### Extraction Method

**Primary**: JSON extraction with regex
```python
# Extract all titles from embedded JSON
title_pattern = r'"titleText"[^}]*"text":"([^"]*)"'
titles = re.findall(title_pattern, html)

# Extract all IMDB IDs
id_pattern = r'/title/(tt\d+)/'
ids = re.findall(id_pattern, html)

# Match them together
items = [(title, id) for title, id in zip(titles, ids)]
```

**Fallback**: Traditional BeautifulSoup scraping (if JSON fails)

### Supported Features
- ✅ International titles (all languages)
- ✅ Special characters (umlauts, accents, etc.)
- ✅ Both watchlist and list URLs
- ✅ Public IMDB lists
- ✅ Automatic deduplication

## Troubleshooting

### Less than 248 items found?

1. **Check if watchlist is public**:
   - Visit: `https://www.imdb.com/list/ls086668596/`
   - Should be accessible without login

2. **Check Docker logs**:
   ```bash
   docker logs imdb-plex-sync
   ```
   Look for: "JSON extraction successful: 248 items"

3. **Verify manually**:
   ```bash
   docker exec imdb-plex-sync curl -L -A "Mozilla/5.0" \
     "https://www.imdb.com/list/ls086668596/" | \
     grep -o '"titleText"' | wc -l
   ```

### Extraction fails completely?

The app has multiple fallback methods:
1. JSON extraction (gets all 248)
2. Traditional scraping (gets 25)
3. CSV export (if available)
4. Pagination (last resort)

Check logs to see which method is being used.

### Container won't start?

```bash
# Check logs
docker logs imdb-plex-sync

# Rebuild
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

## API Endpoints

- `GET /` - Web dashboard
- `GET /health` - Health check
- `GET /api/config` - Get configuration
- `POST /api/config` - Update configuration
- `GET /api/logs` - Get application logs
- `GET /api/results` - Get sync results
- `POST /api/sync` - Trigger manual sync
- `GET /api/status` - Get sync status

## Updating

```bash
# Stop container
docker-compose down

# Update files (app.py, Dockerfile, etc.)

# Rebuild and restart
docker-compose build
docker-compose up -d
```

Your config files are preserved in the `./config` directory.

## Success Indicators

✅ **Working correctly when**:
- Logs show: "JSON extraction successful: 248 items"
- Dashboard shows all 248 movies
- Sync completes in <5 seconds
- Special characters display correctly

## Why It Works

IMDB loads all watchlist data into the page for React to render, even though only 25 items are initially visible. The JSON extraction method:
1. Fetches the page once
2. Extracts embedded JSON data
3. Gets all 248 items immediately
4. No pagination needed!

This is **much faster and more reliable** than pagination or web scraping.

## Contributing

If you encounter issues:
1. Check logs for errors
2. Verify your watchlist is public
3. Test with `test_json_extraction.py`
4. Open an issue with logs

## License

MIT License - feel free to use and modify!

## Credits

- Uses IMDB for watchlist data
- Uses TMDB for movie metadata
- Uses Plex for watchlist management
- Inspired by the need to sync all 248 movies, not just 25!

---

**Built with ❤️ to solve the "only 25 items" problem**
