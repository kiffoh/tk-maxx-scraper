# TK Maxx × END. brands

Every day this finds TK Maxx UK products from brands that END. also stocks. It saves them to `tkmaxx.db` (SQLite), rebuilds `docs/index.html` (served by GitHub Pages) and emails a digest when new products appear.

Standard-library Python 3.11+ only. Product data comes from TK Maxx's Bloomreach search API, so there's no browser and no Akamai block.

## Setup
1. `cp .env.example .env` and fill it in. The Gmail app password comes from Google account → Security → 2-Step Verification → App passwords.
2. Edit `favourites.txt`.
3. `python3 scraper.py --dry-run`, then open `docs/index.html`. The first real run seeds the DB without emailing.
4. GitHub: `git init`, push to a repo, then Settings → Pages → Deploy from branch → `main` / `/docs`.
5. Schedule it daily at 05:00:
   ```
   cp com.kiffohara.tkmaxx-scraper.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.kiffohara.tkmaxx-scraper.plist
   launchctl start com.kiffohara.tkmaxx-scraper   # run once now (skips outside 04:00–07:30)
   ```
   Optional, to wake the Mac first: `sudo pmset repeat wake MTWRFSU 04:55:00`

## Tests
`python3 test_scraper.py`

## Useful queries
`sqlite3 tkmaxx.db "select brand, name, sizes, url from products where last_seen = date('now') limit 20"`

## Later
FastAPI app that reads the same `tkmaxx.db`, with `render_site` moved into a Jinja template.
