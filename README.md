# Stablecoin Overseas Monitor

Public static daily report for overseas discussion around Circle, stablecoins, USDC, Tether/USDT, and Robinhood.

The project intentionally uses free public sources only. X/Twitter is not scraped and is not connected unless a free, official, compliant source becomes available. Optional APIs such as YouTube can be enabled with a free quota key.

## What It Builds

- `docs/index.html`: public dashboard for GitHub Pages.
- `docs/data/latest.json`: latest normalized data.
- `docs/archive/YYYY-MM-DD.json`: daily archive snapshots.

## Data Sources

Enabled by default:

- Google News RSS search
- Bluesky public search API
- Hacker News Algolia API
- Reddit public search RSS, when reachable
- Mastodon public search, when reachable
- Configured RSS feeds for crypto/news/blog sources

Skipped by default:

- X/Twitter direct monitoring, because official read access is paid.
- YouTube search, unless `YOUTUBE_API_KEY` is configured as a free-quota GitHub secret.
- Authenticated Bluesky search, unless free-account `BSKY_IDENTIFIER` and `BSKY_APP_PASSWORD` secrets are configured. The script still tries the public endpoint first.

## Local Run

```powershell
python scripts/generate_report.py --output docs
```

Open `docs/index.html` in a browser after generation.

## GitHub Pages Setup

1. Create a public GitHub repository.
2. Push this project to the repository.
3. In repository settings, set Pages source to **GitHub Actions**.
4. Run the `Update Stablecoin Monitor` workflow once from the Actions tab.

The workflow runs every day at `00:00 UTC`, which is `08:00` Beijing time.

## Optional YouTube Search

Create a repository secret named `YOUTUBE_API_KEY`. The script uses YouTube Data API free quota when that secret exists and otherwise skips YouTube.

## Optional Bluesky Search

If the public Bluesky search endpoint returns `403`, create a Bluesky app password and add two repository secrets:

- `BSKY_IDENTIFIER`
- `BSKY_APP_PASSWORD`

This uses a free account credential and does not require paid access.

## Adjusting Topics

Edit `config/sources.json` to add sources, change queries, or tune keyword aliases.
