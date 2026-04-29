# is-gamepass-worth

Should you keep paying for Xbox Game Pass Ultimate, or just buy your backlog?

Given a list of games you're currently playing, this tool fetches historical-low prices from [IsThereAnyDeal](https://isthereanydeal.com) and "Main + Sides" hours from [HowLongToBeat](https://howlongtobeat.com), then renders a verdict: **subscribe** if you'll finish before the subscription cost overtakes the buyout cost, **buy** otherwise.

## Example

```
  Game Pass Ultimate · Break-Even Analyzer
  CAD · $26.99/mo · 8 hrs/week · 2026-04-28

  Title                                       Price     Hours    $/hr
  ─────────────────────────────────────────  ────────  ──────  ──────
  Avowed                                       44.99     47.6    0.95
  Clair Obscur: Expedition 33                  42.32     45.7    0.93
  Fallout: New Vegas                            1.34     60.3    0.02
  ...
  ─────────────────────────────────────────  ────────  ──────  ──────
  Total                                       645.85    888.7    0.73

  Summary
  ──────────────────────────────────────────────────────────────────
    Total buyout cost     645.85 CAD
    Equivalent GP months  23.9  (at 26.99 CAD/mo)
    Total hours to beat   888.7
    Completion ETA        2028-06-13  (at 8 hrs/week)
    Break-even date       2028-04-28
    Verdict               BUY
```

## Setup

1. Install dependencies:
   ```
   pip install --user -r requirements.txt
   ```
2. Get an ITAD API key at https://isthereanydeal.com/apps/
3. Copy the env template and paste your key:
   ```
   cp .env.example .env
   $EDITOR .env
   ```
4. Edit `games.txt` — one title per line. Blank lines and `#` comments are ignored.

## Run

```
python3 gpu_breakeven.py
```

Output streams to your terminal. Add `--write-report` to also save a Markdown copy at `report.md`.

## Options

| Flag                | Default       | Notes |
| ------------------- | ------------- | ----- |
| `--region`          | `CA`          | ISO 3166-1 alpha-2 code. Built-in price presets for CA/US/DE/GB/AU. Any other region works if you also pass `--gpu-price` and `--currency`. |
| `--gpu-price`       | per region    | Monthly Game Pass Ultimate price (numeric). |
| `--currency`        | per region    | 3-letter currency code, e.g. `JPY`. |
| `--hours-per-week`  | `8`           | Assumed play rate. |
| `--workers`         | `8`           | Concurrent HLTB lookups. Lower if HLTB rate-limits you. |
| `--refresh`         | off           | Bypass `cache.json` (forces fresh ITAD + HLTB fetches). |
| `--write-report`    | off           | Also write `report.md` (Markdown). |

Only `US` is treated as a verified default; other regions print a warning so you remember to verify the local Game Pass price on xbox.com or override with `--gpu-price`.

## How it works

- **Price waterfall.** ITAD's historical low → lowest current first-party deal → MSRP → unavailable. Filtered to first-party shops (Steam, Microsoft Store, GOG, Epic) so keyshop bundles don't skew the buyout estimate. Prices marked with `*` came from the fallback.
- **HLTB hours.** The highest-similarity match's "Main + Sides," falling back to "Main Story." Fetched concurrently via `ThreadPoolExecutor`.
- **Canonical titling.** HLTB's official spelling replaces yours in the report. Every non-cosmetic substitution is listed in the Issues block so you can audit it — HLTB occasionally picks the wrong game when titles share a substring (e.g. `avowed` → `Unavowed`).
- **Cache.** `cache.json` with a 7-day TTL on prices and HLTB hours. UUIDs never expire; only prices/hours refresh. `--refresh` bypasses everything.
- **Verdict.** Completion date (hours / weekly rate) vs. break-even date (buyout / monthly cost). Whichever date arrives first is the cheaper path.

## Caveats

- **Non-US Game Pass prices are best-effort defaults.** Verify the current local price for your region or override with `--gpu-price` once.
- **HLTB matching is fuzzy.** It can confidently substitute a different game with a similar name. Always check the "Title substitutions" section of the report.
- **ITAD title lookup is exact-ish.** Roman numerals vs. Arabic, missing punctuation, or stripped commas can lead to a phantom UUID with no price data. If a game shows `—` for price, double-check spelling against the official store name and re-run with `--refresh`.

## Tests

```
python3 -m unittest tests.test_gpu_breakeven
```

Pure helpers (price waterfall, region resolution, render functions, cache freshness) are unit-tested. Network paths (ITAD, HLTB) are exercised by smoke runs only.

## Out of scope

GUI, async/multi-region runs, auto-fetching the Game Pass library from Xbox, database storage. See `CLAUDE.md` for the full design constraints.
