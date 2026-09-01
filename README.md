# Watchlist Sync → Plex

Sync your watchlist to your **Plex Watchlist** automatically — which then drives **Radarr** and **Sonarr** as the single source of truth for what to download. Supports **IMDB**, **TMDB**, and **Trakt** as input sources, with streaming service filtering so you only add things you can't already watch elsewhere.

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://www.buymeacoffee.com/aaronhorv)

---

## Features

- **Multiple input sources** — IMDB watchlist/list, TMDB list, TMDB watchlist, or Trakt watchlist
- **Streaming service filter** — skip items already available on Netflix, Disney+, Prime, HBO, Apple TV+, Paramount+, Hulu, Peacock
- **OAuth flows built in** — authenticate with TMDB and Trakt directly from the web UI
- **Auto sync** — runs every 6 hours; manual trigger available
- **Web dashboard** — configure, monitor, and view results from a browser

---

## How it works

```
Watchlist source            This app              Arr apps
(IMDB / TMDB / Trakt)  ──────────────────▶  Plex Watchlist  ──▶  Radarr (movies)
                          syncs every 6h                      └──▶  Sonarr (shows)
```

1. You maintain a watchlist on IMDB, TMDB, or Trakt
2. This app syncs it into your **Plex Watchlist** every 6 hours (or on demand)
3. **Radarr** and **Sonarr** poll the Plex Watchlist as an import list and automatically download matching items

The Plex Watchlist is the single source of truth — anything added here gets picked up automatically. The streaming service filter prevents adding titles you can already watch on Netflix, Disney+, etc.

### Radarr & Sonarr setup

In both apps: **Settings → Import Lists → Add → Plex Watchlist**. Enter your Plex token and save.

---

## Quick Start

### Docker Compose (recommended)

```yaml
services:
  watchlist-plex-sync:
    build: .
    container_name: watchlist-plex-sync
    ports:
      - "5000:5000"
    volumes:
      - ./config:/config
    restart: unless-stopped
    environment:
      - TZ=Europe/Berlin
```

Or clone and build locally:

```bash
git clone https://github.com/aaronhorv/watchlist-plex-sync.git
cd watchlist-plex-sync
docker compose up -d --build
```

Open **http://localhost:5000** (or your server's address).

---

## Configuration

All configuration is done through the web UI. Open the **Configuration** tab, which has two sub-tabs:

### Input List

Choose your source from the dropdown:

| Source | What you need |
|--------|--------------|
| **IMDB** | Your IMDB watchlist or list URL; authenticated lists can use an IMDb Cookie header |
| **TMDB List** | A TMDB list ID |
| **TMDB Watchlist** | TMDB Session ID + Account ID (auth flow built in) |
| **Trakt** | Trakt watchlist URL + Client ID + Client Secret (OAuth built in) |

#### TMDB Authentication (for TMDB Watchlist)
1. Enter your TMDB API Key in the **Plex & TMDB** tab first
2. Click **Step 1: Get Request Token**
3. Click the approval link → approve at themoviedb.org
4. Click **Step 3: Create Session ID** — it's saved automatically
5. Click **Fetch Account ID** to populate your account ID

#### Trakt Authentication (for private watchlists)
1. Create a free app at [trakt.tv/oauth/applications](https://trakt.tv/oauth/applications) — get Client ID and Client Secret
2. Enter both in the Trakt fields
3. Click **Step 1: Authorize with Trakt** — a code appears
4. Visit [trakt.tv/activate](https://trakt.tv/activate), enter the code
5. The page updates automatically when authorized

### Plex & TMDB

| Field | Where to get it |
|-------|----------------|
| **Plex Token** | [plex.tv/claim](https://plex.tv/claim) or Settings → Account in Plex |
| **TMDB API Key** | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) |
| **Streaming Services** | Select your active subscriptions and region |

---

## Updating

```bash
git pull
docker compose down && docker compose up -d --build
```

Config is preserved in `./config/`.

---

## Troubleshooting

**IMDB: fewer items than expected**
- Make sure your watchlist is set to public
- For authenticated lists, paste a full IMDb request `Cookie` header in Configuration. It seeds a persistent Chromium profile under `./config/imdb-browser-profile`; the profile is reused on later syncs and survives container rebuilds.
- If IMDb shows Human Verification, solve it in a normal browser and refresh the configured Cookie header once. The app does not bypass IMDb challenges.
- Check Docker logs: `docker logs watchlist-plex-sync`

**TMDB 401 errors**
- Verify your TMDB API Key is saved correctly
- For watchlist access, complete the session auth flow in the UI

**Trakt 401 errors**
- Private watchlists require OAuth — use the built-in auth flow
- Ensure both Client ID and Client Secret are entered before authorizing

**Container won't start**
```bash
docker logs watchlist-plex-sync
docker compose down && docker compose up -d --build
```

---

## License

MIT

---

*Built with ❤️ — if this saves you time, consider buying me a coffee!*

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/aaronhorv)
