#!/usr/bin/env python3
"""Game Pass Ultimate break-even analyzer.

Given a list of games in `games.txt`, fetches historical-low prices from
IsThereAnyDeal and "main + sides" hours from HowLongToBeat, then prints a
Markdown report showing whether it's cheaper to subscribe or buy.
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import pathlib
import shutil
import sys
import time

import requests
from howlongtobeatpy import HowLongToBeat


ITAD_BASE = "https://api.isthereanydeal.com"
FIRST_PARTY_SHOPS = {"steam", "microsoft store", "gog", "epic game store"}
CACHE_PATH = pathlib.Path("cache.json")
ENV_PATH = pathlib.Path(".env")
CACHE_TTL = dt.timedelta(days=7)
REQUEST_TIMEOUT = 15
SLEEP_BETWEEN_LOOKUPS = 0.3

# (currency_code, monthly_price). Only US is verified per CLAUDE.md
# (effective 2026-04-21). Other regions are best-effort defaults — the
# script warns at runtime so the user remembers to verify or override
# with --gpu-price.
GPU_PRICES = {
    "CA": ("CAD", 26.99),
    "US": ("USD", 22.99),
    "DE": ("EUR", 17.99),
    "GB": ("GBP", 14.99),
    "AU": ("AUD", 28.95),
}
VERIFIED_REGIONS = {"US"}


def load_env(path):
    """Parse a .env file and populate os.environ for any keys not already set."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default="games.txt", help="Path to games list")
    p.add_argument("--region", default="CA",
                   help="ISO country code (e.g. CA, US, GB). Default: CA")
    p.add_argument("--currency", default=None,
                   help="3-letter currency code; defaults from --region table")
    p.add_argument("--gpu-price", type=float, default=None,
                   help="Monthly Game Pass Ultimate price; defaults from --region table")
    p.add_argument("--hours-per-week", type=float, default=8.0,
                   help="Assumed play rate (default 8)")
    p.add_argument("--workers", type=int, default=8,
                   help="Concurrent HLTB lookups (default 8)")
    p.add_argument("--write-report", action="store_true",
                   help="Also write the report to report.md")
    p.add_argument("--refresh", action="store_true",
                   help="Bypass the cache for prices and HLTB hours")
    return p.parse_args()


LABEL_WIDTH = 18
BAR_WIDTH = 20
TRAIL_WIDTH = 40


def status(msg):
    """Single free-form line to stderr (used for warnings)."""
    print(msg, file=sys.stderr, flush=True)


def status_line(label, value):
    """Aligned label/value status to stderr."""
    print(f"{label:<{LABEL_WIDTH}} {value}", file=sys.stderr, flush=True)


def render_bar(done, total, width=BAR_WIDTH):
    if total <= 0:
        return "[" + "░" * width + "]"
    filled = int(round(width * done / total))
    filled = max(0, min(width, filled))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _truncate(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


class Progress:
    """Updating progress line on stderr; falls back to per-line if stderr isn't a TTY."""

    def __init__(self, label, total):
        self.label = label
        self.total = total
        self.done = 0
        self.is_tty = sys.stderr.isatty()
        self.start = time.monotonic()

    def tick(self, current):
        self.done += 1
        if self.is_tty:
            bar = render_bar(self.done, self.total)
            trail = _truncate(current, TRAIL_WIDTH)
            line = f"{self.label:<{LABEL_WIDTH}} {bar} {self.done}/{self.total}  {trail}"
            print(f"\r\x1b[2K{line}", end="", file=sys.stderr, flush=True)
        else:
            print(f"  [{self.done}/{self.total}] {current}",
                  file=sys.stderr, flush=True)

    def finish(self, suffix=""):
        elapsed = time.monotonic() - self.start
        msg = f"{self.done}/{self.total} done · {elapsed:.1f}s"
        if suffix:
            msg += f" · {suffix}"
        if self.is_tty:
            print(f"\r\x1b[2K{self.label:<{LABEL_WIDTH}} {msg}",
                  file=sys.stderr, flush=True)
        else:
            print(f"{self.label:<{LABEL_WIDTH}} {msg}",
                  file=sys.stderr, flush=True)


def resolve_region(args):
    """Return (currency, gpu_price, used_default_for_unverified_region)."""
    preset = GPU_PRICES.get(args.region.upper())
    currency = args.currency or (preset[0] if preset else None)
    price = args.gpu_price if args.gpu_price is not None else (preset[1] if preset else None)
    if currency is None or price is None:
        sys.exit(
            f"Region '{args.region}' has no preset price. "
            f"Pass --gpu-price and --currency explicitly."
        )
    used_unverified_default = (
        preset is not None
        and args.region.upper() not in VERIFIED_REGIONS
        and args.gpu_price is None
    )
    return currency.upper(), price, used_unverified_default


def read_titles(path):
    titles = []
    for raw in pathlib.Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        titles.append(line)
    return titles


def load_cache():
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        print("warning: cache.json unreadable, starting fresh", file=sys.stderr)
        return {}


def save_cache(cache):
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def cache_fresh(iso_ts):
    if not iso_ts:
        return False
    try:
        ts = dt.datetime.fromisoformat(iso_ts)
    except ValueError:
        return False
    return dt.datetime.now(dt.timezone.utc) - ts < CACHE_TTL


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def retry_once(fn, label):
    """Call fn(); on exception, sleep 1s and try again. Return None on second failure."""
    try:
        return fn()
    except Exception as exc:
        print(f"warning: {label} failed ({exc}); retrying once", file=sys.stderr)
        time.sleep(1.0)
        try:
            return fn()
        except Exception as exc2:
            print(f"warning: {label} failed again ({exc2}); skipping", file=sys.stderr)
            return None


def itad_lookup_uuids(titles, api_key):
    """POST /lookup/id/title/v1 — returns dict mapping each title to UUID or None."""
    if not titles:
        return {}

    def call():
        r = requests.post(
            f"{ITAD_BASE}/lookup/id/title/v1",
            params={"key": api_key},
            json=titles,
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    result = retry_once(call, "ITAD title lookup")
    if not isinstance(result, dict):
        return {t: None for t in titles}
    return {t: result.get(t) for t in titles}


def itad_fetch_first_party_shop_ids(api_key, region):
    """GET /service/shops/v1 — return list of integer shop IDs whose name matches FIRST_PARTY_SHOPS."""
    def call():
        r = requests.get(
            f"{ITAD_BASE}/service/shops/v1",
            params={"key": api_key, "country": region},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    shops = retry_once(call, "ITAD shops list")
    if not isinstance(shops, list):
        return []
    ids = []
    for s in shops:
        name = (s.get("title") or s.get("name") or "").strip().lower()
        sid = s.get("id")
        if name in FIRST_PARTY_SHOPS and isinstance(sid, int):
            ids.append(sid)
    return ids


def _extract_effective_price(entry):
    """Waterfall over a /games/prices/v3 entry.

    Order of preference:
      1. historyLow.all  (best historical low across the filtered shops)
      2. lowest deals[].price  (best current deal price)
      3. lowest deals[].regular  (MSRP / sticker)

    Returns (amount, currency, source) where source is one of
    "hist_low" | "current" | "msrp", or None if nothing usable.
    """
    low = (entry.get("historyLow") or {}).get("all") or {}
    if isinstance(low.get("amount"), (int, float)):
        return (float(low["amount"]), low.get("currency"), "hist_low")

    deals = entry.get("deals") or []

    def _collect(field):
        out = []
        for d in deals:
            p = (d.get(field) or {}).get("amount")
            c = (d.get(field) or {}).get("currency")
            if isinstance(p, (int, float)):
                out.append((float(p), c))
        return out

    current = _collect("price")
    if current:
        amount, currency = min(current, key=lambda x: x[0])
        return (amount, currency, "current")

    regular = _collect("regular")
    if regular:
        amount, currency = min(regular, key=lambda x: x[0])
        return (amount, currency, "msrp")

    return None


def itad_fetch_prices(uuids, api_key, region, shop_ids):
    """POST /games/prices/v3 — return dict mapping UUID to price dict or None.

    Each value is {"amount", "currency", "source"} from the waterfall above.
    """
    if not uuids:
        return {}

    def call():
        params = {"key": api_key, "country": region}
        if shop_ids:
            params["shops"] = ",".join(str(s) for s in shop_ids)
        r = requests.post(
            f"{ITAD_BASE}/games/prices/v3",
            params=params,
            json=list(uuids),
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    data = retry_once(call, "ITAD prices fetch")
    out = {}
    if not isinstance(data, list):
        return {u: None for u in uuids}
    for entry in data:
        uuid = entry.get("id")
        result = _extract_effective_price(entry)
        if result is None:
            out[uuid] = None
        else:
            amount, currency, source = result
            out[uuid] = {"amount": amount, "currency": currency, "source": source}
    for u in uuids:
        out.setdefault(u, None)
    return out


def hltb_lookup(title):
    """Return {'hours': float, 'name': str} for the best match, or None on miss."""
    try:
        results = HowLongToBeat().search(title)
    except Exception as exc:
        print(f"warning: HLTB search failed for '{title}' ({exc})", file=sys.stderr)
        return None
    if not results:
        return None
    best = max(results, key=lambda r: getattr(r, "similarity", 0))
    hours = getattr(best, "main_extra", 0) or 0
    if not hours:
        hours = getattr(best, "main_story", 0) or 0
    if not hours:
        return None
    name = (getattr(best, "game_name", None) or title).strip()
    return {"hours": float(hours), "name": name or title}


def render_markdown(rows, currency, total_cost, total_hours,
                    gpu_price, hours_per_week, unresolved,
                    hltb_missing=None, substitutions=None):
    months = total_cost / gpu_price if gpu_price else 0
    completion_weeks = total_hours / hours_per_week if hours_per_week else 0
    completion_date = dt.date.today() + dt.timedelta(weeks=completion_weeks)
    breakeven_date = dt.date.today() + dt.timedelta(days=int(months * 30.44))
    verdict = "subscribe" if completion_date <= breakeven_date else "buy"

    lines = [
        f"# Game Pass Ultimate Break-Even Report",
        f"",
        f"_Generated {dt.date.today().isoformat()} — region price ${gpu_price:.2f} {currency}/mo, "
        f"{hours_per_week:g} hrs/week_",
        f"",
        f"| Title | Hist. Low ({currency}) | Hours | $/hr |",
        f"| --- | ---: | ---: | ---: |",
    ]
    has_fallback = False
    for r in rows:
        if r["price"] is None:
            price_s = "—"
        else:
            mark = "*" if r.get("price_source") and r["price_source"] != "hist_low" else ""
            price_s = f"{r['price']:.2f}{mark}"
            if mark:
                has_fallback = True
        hours_s = f"{r['hours']:.1f}" if r["hours"] is not None else "—"
        if r["price"] is not None and r["hours"]:
            per_hr = f"{r['price'] / r['hours']:.2f}"
        else:
            per_hr = "—"
        lines.append(f"| {r['title']} | {price_s} | {hours_s} | {per_hr} |")

    if has_fallback:
        lines += [
            "",
            "_* current sale price or MSRP — no historical low available for this title in first-party shops._",
        ]

    lines += [
        "",
        "## Summary",
        f"- Total buyout: **{total_cost:.2f} {currency}**",
        f"- Equivalent Game Pass Ultimate months: **{months:.1f}**",
        f"- Total hours to beat: **{total_hours:.1f}**",
        f"- Projected completion at {hours_per_week:g} hrs/week: **{completion_date.isoformat()}**",
        f"- Break-even date (subscription cost == buyout cost): **{breakeven_date.isoformat()}**",
        f"- Verdict: **{verdict}**",
    ]
    if unresolved:
        lines += ["", "## Unresolved titles (ITAD)",
                  "_No price match — fix spelling in games.txt and re-run._", ""]
        lines += [f"- {t}" for t in unresolved]
    if hltb_missing:
        lines += ["", "## Hours not found (HLTB)",
                  "_No HowLongToBeat match — fix spelling in games.txt and re-run._", ""]
        lines += [f"- {t}" for t in hltb_missing]
    if substitutions:
        lines += ["", "## Title substitutions",
                  "_HLTB returned a different title than you typed. "
                  "Verify these are the right games — if not, fix `games.txt` "
                  "and re-run with `--refresh`._", ""]
        lines += [f"- {orig} → {canonical}" for orig, canonical in substitutions]
    return "\n".join(lines) + "\n"


def _verdict_dates(total_cost, total_hours, gpu_price, hours_per_week):
    months = total_cost / gpu_price if gpu_price else 0
    completion_weeks = total_hours / hours_per_week if hours_per_week else 0
    completion_date = dt.date.today() + dt.timedelta(weeks=completion_weeks)
    breakeven_date = dt.date.today() + dt.timedelta(days=int(months * 30.44))
    verdict = "SUBSCRIBE" if completion_date <= breakeven_date else "BUY"
    return months, completion_date, breakeven_date, verdict


def render_terminal(rows, currency, total_cost, total_hours,
                    gpu_price, hours_per_week, unresolved,
                    hltb_missing=None, substitutions=None, width=None):
    """Render a human-friendly aligned-column report for stdout."""
    hltb_missing = hltb_missing or []
    substitutions = substitutions or []
    if width is None:
        width = shutil.get_terminal_size((100, 20)).columns
    width = max(60, min(width, 200))

    months, completion_date, breakeven_date, verdict = _verdict_dates(
        total_cost, total_hours, gpu_price, hours_per_week
    )

    # Column widths: numerics fixed, title fills remaining.
    PRICE_W, HOURS_W, PERHR_W = 9, 8, 8
    GAP = 2
    INDENT = 2
    title_w = width - INDENT - (PRICE_W + HOURS_W + PERHR_W + GAP * 3)
    title_w = max(20, title_w)
    indent = " " * INDENT
    rule = (
        indent + "─" * title_w + " " * GAP + "─" * PRICE_W + " " * GAP
        + "─" * HOURS_W + " " * GAP + "─" * PERHR_W
    )

    lines = []
    lines.append("")
    lines.append(indent + f"Game Pass Ultimate · Break-Even Analyzer")
    lines.append(indent + (
        f"{currency} · ${gpu_price:.2f}/mo · "
        f"{hours_per_week:g} hrs/week · {dt.date.today().isoformat()}"
    ))
    lines.append("")

    header_fmt = (f"{indent}{{:<{title_w}}}{' ' * GAP}{{:>{PRICE_W}}}"
                  f"{' ' * GAP}{{:>{HOURS_W}}}{' ' * GAP}{{:>{PERHR_W}}}")
    lines.append(header_fmt.format("Title", "Price", "Hours", "$/hr"))
    lines.append(rule)

    has_fallback = False
    for r in rows:
        title = _truncate(r["title"], title_w)
        if r["price"] is None:
            price_s = "—"
        else:
            mark = "*" if r.get("price_source") and r["price_source"] != "hist_low" else ""
            price_s = f"{r['price']:.2f}{mark}"
            if mark:
                has_fallback = True
        hours_s = f"{r['hours']:.1f}" if r["hours"] is not None else "—"
        if r["price"] is not None and r["hours"]:
            per_hr = f"{r['price'] / r['hours']:.2f}"
        else:
            per_hr = "—"
        lines.append(header_fmt.format(title, price_s, hours_s, per_hr))

    overall_per_hr = (
        f"{total_cost / total_hours:.2f}" if total_hours > 0 and total_cost > 0
        else "—"
    )
    lines.append(rule)
    lines.append(header_fmt.format(
        "Total", f"{total_cost:.2f}", f"{total_hours:.1f}", overall_per_hr
    ))
    if has_fallback:
        lines.append(indent + "* current sale price or MSRP "
                              "(no historical low available)")
    lines.append("")

    # Summary block: aligned key/value pairs.
    summary_pairs = [
        ("Total buyout cost", f"{total_cost:.2f} {currency}"),
        ("Equivalent GP months",
         f"{months:.1f}  (at {gpu_price:.2f} {currency}/mo)"),
        ("Total hours to beat", f"{total_hours:.1f}"),
        ("Completion ETA",
         f"{completion_date.isoformat()}  (at {hours_per_week:g} hrs/week)"),
        ("Break-even date", breakeven_date.isoformat()),
        ("Verdict", verdict),
    ]
    label_w = max(len(k) for k, _ in summary_pairs)
    lines.append(indent + "Summary")
    lines.append(indent + "─" * (width - INDENT))
    for k, v in summary_pairs:
        lines.append(f"{indent}  {k:<{label_w}}  {v}")
    lines.append("")

    # Issues block: only render when there's something to show.
    issues = []
    if unresolved:
        issues.append((
            "Unresolved (ITAD)",
            "no price match — fix spelling in games.txt and re-run",
            list(unresolved),
        ))
    if hltb_missing:
        issues.append((
            "Hours not found (HLTB)",
            "no HLTB match — fix spelling in games.txt and re-run",
            list(hltb_missing),
        ))
    if substitutions:
        issues.append((
            "Title substitutions",
            "verify HLTB picked the right game — if not, fix games.txt and --refresh",
            [f"{orig}  →  {canon}" for orig, canon in substitutions],
        ))
    if issues:
        lines.append(indent + "Issues")
        lines.append(indent + "─" * (width - INDENT))
        for i, (label, hint, items) in enumerate(issues):
            if i > 0:
                lines.append("")
            lines.append(f"{indent}  {label}  ({len(items)})")
            lines.append(f"{indent}  {hint}")
            for item in items:
                lines.append(f"{indent}    • {_truncate(item, width - INDENT - 6)}")
        lines.append("")

    return "\n".join(lines) + "\n"


def main():
    load_env(ENV_PATH)
    args = parse_args()

    api_key = os.environ.get("ITAD_API_KEY", "").strip()
    if not api_key:
        sys.exit("ITAD_API_KEY missing. Copy .env.example to .env and paste your key.")

    region = args.region.upper()
    currency, gpu_price, unverified = resolve_region(args)
    if unverified:
        print(
            f"warning: using a placeholder Game Pass Ultimate price for region "
            f"{region} ({gpu_price:.2f} {currency}). Verify on xbox.com or pass "
            f"--gpu-price.",
            file=sys.stderr,
        )

    titles = read_titles(args.input)
    if not titles:
        sys.exit(f"No titles found in {args.input}.")
    status_line("games.txt", f"{len(titles)} titles")

    cache = {} if args.refresh else load_cache()

    # Step 1: fill in missing/stale UUIDs (one batched POST).
    needs_uuid = [t for t in titles if not cache.get(t, {}).get("itad_uuid")]
    if needs_uuid:
        uuid_map = itad_lookup_uuids(needs_uuid, api_key)
        for t, u in uuid_map.items():
            cache.setdefault(t, {})["itad_uuid"] = u
        matched = sum(1 for u in uuid_map.values() if u)
        status_line("ITAD lookup", f"{matched}/{len(uuid_map)} matched")

    # Step 2: fill in missing/stale prices for this region (one batched POST).
    # Cache schema migration: pre-waterfall entries had `hist_low` but no `amount`;
    # treat them as stale so the new shape is populated on next fetch.
    needs_price = []
    for t in titles:
        entry = cache.get(t, {})
        uuid = entry.get("itad_uuid")
        if not uuid:
            continue
        region_prices = entry.setdefault("prices", {}).get(region, {})
        is_v2 = "amount" in region_prices
        if args.refresh or not cache_fresh(region_prices.get("fetched_at")) or not is_v2:
            needs_price.append((t, uuid))

    if needs_price:
        shop_ids = itad_fetch_first_party_shop_ids(api_key, region)
        uuid_to_title = {u: t for t, u in needs_price}
        price_map = itad_fetch_prices(
            [u for _, u in needs_price], api_key, region, shop_ids
        )
        priced = 0
        fallback = 0
        for uuid, val in price_map.items():
            t = uuid_to_title.get(uuid)
            if not t:
                continue
            if val is not None:
                priced += 1
                if val["source"] != "hist_low":
                    fallback += 1
                cache[t]["prices"][region] = {
                    "amount": val["amount"],
                    "currency": val["currency"],
                    "source": val["source"],
                    "fetched_at": now_iso(),
                }
            else:
                cache[t]["prices"][region] = {
                    "amount": None,
                    "currency": None,
                    "source": None,
                    "fetched_at": now_iso(),
                }
        suffix = f" · {fallback} fallback" if fallback else ""
        status_line("ITAD prices",
                    f"first-party shops · {priced}/{len(needs_price)} priced{suffix}")

    # Step 3: fetch HLTB hours+canonical name in parallel for any stale/missing entries.
    needs_hltb = [
        t for t in titles
        if args.refresh or not cache_fresh(cache.get(t, {}).get("hltb_fetched_at"))
    ]
    if needs_hltb:
        bar = Progress("HLTB hours", len(needs_hltb))
        misses = 0
        with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            future_to_title = {pool.submit(hltb_lookup, t): t for t in needs_hltb}
            for fut in cf.as_completed(future_to_title):
                t = future_to_title[fut]
                result = fut.result()
                entry = cache.setdefault(t, {})
                entry["hltb_fetched_at"] = now_iso()
                if result is None:
                    entry["hltb_hours"] = None
                    entry["hltb_title"] = None
                    misses += 1
                    bar.tick(f"{t} — HLTB miss")
                else:
                    entry["hltb_hours"] = result["hours"]
                    entry["hltb_title"] = result["name"]
                    bar.tick(f"{result['name']} — {result['hours']:.1f}h")
        bar.finish(f"{misses} miss" if misses == 1 else f"{misses} misses")

    save_cache(cache)

    # Step 4: assemble rows + warnings.
    rows = []
    unresolved = []
    hltb_missing = []
    substitutions = []
    total_cost = 0.0
    total_hours = 0.0
    for t in titles:
        entry = cache.get(t, {})
        uuid = entry.get("itad_uuid")
        price_entry = entry.get("prices", {}).get(region, {})
        price = price_entry.get("amount")
        ccy = price_entry.get("currency")
        source = price_entry.get("source")
        hours = entry.get("hltb_hours")
        canonical = entry.get("hltb_title")
        if not uuid:
            unresolved.append(t)
        if hours is None:
            hltb_missing.append(t)
        if canonical and canonical.casefold() != t.casefold():
            substitutions.append((t, canonical))
        if price is not None:
            total_cost += price
            if ccy and ccy != currency:
                print(
                    f"warning: '{t}' priced in {ccy}, summary uses {currency}",
                    file=sys.stderr,
                )
        if hours is not None:
            total_hours += hours
        display_title = canonical or t
        rows.append({
            "title": display_title,
            "price": price,
            "hours": hours,
            "price_source": source,
        })

    pretty = render_terminal(
        rows, currency, total_cost, total_hours, gpu_price,
        args.hours_per_week, unresolved,
        hltb_missing=hltb_missing, substitutions=substitutions,
    )
    print(pretty, end="")
    if args.write_report:
        markdown = render_markdown(
            rows, currency, total_cost, total_hours, gpu_price,
            args.hours_per_week, unresolved,
            hltb_missing=hltb_missing, substitutions=substitutions,
        )
        pathlib.Path("report.md").write_text(markdown)


if __name__ == "__main__":
    main()
