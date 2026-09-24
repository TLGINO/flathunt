# flathunt

Finds 1–4.5 room flats (≤ CHF 2,500 incl. utilities; the map defaults to 2–2.5 rooms) within 30 minutes by foot or public
transport of Elias-Canetti-Strasse 2, 8050 Zürich, logs them to SQLite, and shows them on a map.

Sources: Flatfox. Homegate and ImmoScout24 block automated requests (Cloudflare / DataDome)
and need API access or an IP allowlist from SMG before they can be added.

## Run locally

```
uv run flathunt.py            # fetch once, update flats.db and site/listings.json
uv run flathunt.py --serve    # map on http://localhost:8050, fetching every 15 min
```

`--port N` changes the port, `--poll N` the interval in minutes (`--poll 0` serves without fetching).

## Self-host on GitHub

`.github/workflows/poll.yml` runs the fetch every 15 minutes and publishes `site/` to GitHub Pages.
The database is kept on the `data` branch as a single commit that is overwritten on each run.

1. Push this repo to GitHub.
2. Settings → Pages → Source: **GitHub Actions**.
3. Actions → *Poll listings and publish map* → **Run workflow** for the first run.

Notes:
- GitHub Pages sites are public, even from a private repo (unless on GitHub Enterprise).
- Private repos get 2,000 free Actions minutes a month; a 15-minute schedule uses more than that.
  Public repos have no limit. On a private repo, change the cron to hourly (`0 * * * *`).
- GitHub disables scheduled workflows in repos with no activity for 60 days.

To query the data: `sqlite3 flats.db "select * from matches"`.
