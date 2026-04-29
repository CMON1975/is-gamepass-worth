# Game Pass Ultimate Break-Even Analyzer

## Goal
Given a manually-curated list of Xbox Game Pass titles I'm currently playing, compute:
- Total cost to buy them all at historical lows (via IsThereAnyDeal)
- Equivalent months of Xbox Game Pass Ultimate that money would cover
- Estimated total hours to beat (via HowLongToBeat)
- Implied "deadline" before subscribing becomes more expensive than buying

## Simplest path
- **Language**: Python 3 (system python in WSL 2 Ubuntu is fine; no venv unless asked)
- **Dependencies**: `requests`, `howlongtobeatpy` (install with `pip install --user requests howlongtobeatpy`)
- **Single script**: `gpu_breakeven.py`
- **Input**: `games.txt`, one title per line; blank lines and `#` comments ignored
- **Output**: a Markdown table printed to stdout, optionally written to `report.md` with `--write-report`

## Constants
- Game Pass Ultimate US price: **$22.99/month** (effective April 21, 2026)
- Default region: `US`, currency `USD`
- Default play rate: `8 hours/week` (override with `--hours-per-week`)

## ITAD API
- Docs: https://docs.isthereanydeal.com/  (verify exact endpoint paths/params here before coding — the docs are the source of truth)
- Base URL: `https://api.isthereanydeal.com`
- Auth: API key in `.env` as `ITAD_API_KEY`. Read it with stdlib (parse `.env` manually or `os.environ.get` after `set -a; source .env`). Do not add `python-dotenv` as a dependency.
- Get a key at: https://isthereanydeal.com/apps/

### Endpoints we need
1. **Title → UUID lookup**: the games lookup-by-title endpoint. Use the `found` flag to detect misses.
2. **Prices / historical low**: the games overview or prices endpoint, POSTing a list of UUIDs. We want `historyLow.all.amount` (or equivalent) per game.
3. **Shop list**: `/service/shops/v1` — fetch once and cache. Use it to build a filter restricted to first-party storefronts (Steam, Microsoft Store, GOG, Epic). This avoids keyshop/bundle prices distorting the "buyout" estimate.

## HowLongToBeat
- No official API. Use the community `howlongtobeatpy` package.
- `from howlongtobeatpy import HowLongToBeat`
- `HowLongToBeat().search(title)` returns a list; take the highest `similarity` match.
- Use `main_extra` ("Main + Sides") in hours as the realistic completion estimate. Fall back to `main_story` if `main_extra` is missing. If the search returns nothing, record `None` and continue.

## Workflow
1. Load `games.txt`; strip blanks/comments.
2. Load `cache.json` if present (maps title → `{itad_uuid, hist_low, hltb_hours, fetched_at}`).
3. For each title not in cache (or older than 7 days):
   - Resolve ITAD UUID.
   - Fetch historical low (filtered to first-party shops).
   - Fetch HLTB hours.
   - Update cache.
4. Save `cache.json`.
5. Render a Markdown table: `Title | Hist. Low | Hours | $/hr`.
6. Render summary:
   - Total buyout cost
   - Equivalent months of Ultimate (`cost / 22.99`)
   - Total hours
   - Projected completion date at `--hours-per-week`
   - Verdict: "subscribe" if completion date < break-even date, else "buy"
7. Print a warning list of any titles that failed to resolve so I can fix spelling in `games.txt`.

## Code style
- Single file, top-to-bottom readable. Plain functions; no classes unless they materially help.
- `argparse` flags: `--input`, `--region`, `--hours-per-week`, `--write-report`, `--refresh` (bypass cache).
- Fail soft: a missing title prints a warning and continues. The script never aborts mid-run.
- Don't hammer ITAD — the cache plus a small `time.sleep` between uncached lookups is enough. ITAD has no hard rate limit but does heuristic abuse checks.
- Network errors: retry once with backoff, then warn and skip.

## Out of scope (do not add unless asked)
- GUI / web UI
- Database
- Async / concurrency
- Multi-region or multi-currency comparison
- Auto-fetching the Game Pass library from Xbox

## Example `games.txt`
```
# Currently playing on Game Pass Ultimate
Avowed
Indiana Jones and the Great Circle
Hi-Fi Rush
Pentiment
```

## Notes for future me
- If HLTB's wrapper breaks (it scrapes the site, so it does occasionally), pin a known-good version or swap to a different community wrapper. Don't try to write our own scraper.
- ITAD UUIDs are stable, so once cached they don't need refreshing — only the prices do.
- "Historical low" can be from a years-ago sale that may not realistically recur. A useful future enhancement is also pulling the most-recent-12-months low, but only if asked.
