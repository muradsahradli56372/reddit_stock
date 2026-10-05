#!/usr/bin/env python3
"""Check that ApeWisdom and StockTwits are reachable from THIS machine and that our parsers
understand their current responses. Makes only a handful of requests; stores nothing.

    python scripts/check_sources.py

Exit code 0 = both OK, 1 = at least one problem (details printed).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.attention import APEWISDOM_URL, parse_apewisdom_page  # noqa: E402
from app.collectors.stocktwits import BASE, parse_message  # noqa: E402
from app.config import settings  # noqa: E402
from app.http_client import HttpError, JsonClient  # noqa: E402

ok = True
client = JsonClient(max_retries=2)


def section(title):
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


section("ApeWisdom (Reddit mention counts)")
for flt in settings.apewisdom_filters:
    t0 = time.time()
    try:
        data = client.get_json(APEWISDOM_URL.format(filter=flt, page=1))
        rows = parse_apewisdom_page(data, flt)
        print(f"  {flt:<16} OK  {len(rows):>3} tickers on page 1 of {data.get('pages')}  "
              f"({time.time() - t0:.1f}s)  top: " + ", ".join(f"{r.ticker}={r.mentions}" for r in rows[:5]))
        if not rows:
            ok = False
            print("     ! no rows parsed - response format may have changed. Raw keys:", list(data)[:10])
    except HttpError as e:
        ok = False
        print(f"  {flt:<16} FAIL HTTP {e.status} (a 404 means this filter name doesn't exist on ApeWisdom;"
              " fix APEWISDOM_FILTERS)")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  {flt:<16} FAIL {e!r}")

section("StockTwits (message text)")
try:
    data = client.get_json(f"{BASE}/trending/symbols.json")
    print("  trending OK:", ", ".join(s.get("symbol", "?") for s in data.get("symbols", [])[:10]))
except Exception as e:  # noqa: BLE001
    print(f"  trending FAIL {e!r} (optional - the watchlist is used instead)")
for sym in ["NVDA", "AAPL"]:
    try:
        data = client.get_json(f"{BASE}/streams/symbol/{sym}.json")
        msgs = data.get("messages") or []
        parsed = [p for p in (parse_message(m) for m in msgs) if p]
        labelled = sum(1 for p in parsed if p.author_sentiment)
        print(f"  {sym}: OK  {len(msgs)} messages, {len(parsed)} parsed, {labelled} author-labelled")
        if parsed:
            p = parsed[0]
            print(f"     newest: {p.created_utc:%Y-%m-%d %H:%M} UTC  {p.author}  {p.body[:70]!r}")
        else:
            ok = False
            print("     ! nothing parsed - response format may have changed. Raw keys:", list(data)[:10])
    except HttpError as e:
        ok = False
        hint = " (403 usually means StockTwits' bot protection blocked this network)" if e.status == 403 else ""
        print(f"  {sym}: FAIL HTTP {e.status}{hint}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  {sym}: FAIL {e!r}")
    time.sleep(1)

print("\nRESULT:", "OK - set TEXT_SOURCE=stocktwits and ATTENTION_SOURCE=apewisdom" if ok else
      "PROBLEMS - see above (paste this output to get help)")
sys.exit(0 if ok else 1)
