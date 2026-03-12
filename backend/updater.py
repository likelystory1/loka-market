"""
Incremental market data updater for Loka Market.

The Loka API is oldest-first: page 0 = oldest trades, last page = newest.
New trades are appended at the end. This updater fetches only the tail
pages needed to catch up, using INSERT OR IGNORE so it's always safe to run.

Usage:
    python updater.py              # run once
    python updater.py --loop 300   # run every 300 seconds
"""

import os
import sys
import time
import sqlite3
import argparse
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "market_history.db")
API_BASE = "https://api.lokamc.com"

PAGE_SIZE           = 20     # trades per page (fixed by API)
OVERLAP_PAGES       = 5      # extra pages before estimated start (safety buffer)
DELAY_BETWEEN_PAGES = 0.15   # seconds between requests
DELAY_ON_RATE_LIMIT = 5      # seconds to wait on 429
MAX_RETRIES         = 5


# ── db helpers ────────────────────────────────────────────────────────────────

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def db_trade_count() -> int:
    conn = db_connect()
    n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    conn.close()
    return n


# ── api helpers ───────────────────────────────────────────────────────────────

def api_get_meta() -> tuple[int, int]:
    """Returns (total_pages, total_elements)."""
    r = requests.get(f"{API_BASE}/completed_market_orders?page=0", timeout=10)
    r.raise_for_status()
    p = r.json()["page"]
    return p["totalPages"], p["totalElements"]

def api_fetch_page(page: int) -> list[dict]:
    """Fetch one page with retry. Returns list of order dicts."""
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(
                f"{API_BASE}/completed_market_orders?page={page}",
                timeout=10
            )
            if r.status_code == 429:
                wait = DELAY_ON_RATE_LIMIT * (attempt + 1)
                print(f"  [rate limit] waiting {wait}s…", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()["_embedded"]["completed_market_orders"]
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
            else:
                print(f"  [error] page {page}: {e}", flush=True)
    return []


# ── core update logic ─────────────────────────────────────────────────────────

def run_update() -> int:
    """
    Fetch new trades from the end of the API (newest pages) and insert them.
    Returns number of new trades inserted.
    """
    total_pages, total_elements = api_get_meta()
    db_count    = db_trade_count()
    new_estimate = total_elements - db_count

    if new_estimate <= 0:
        print(f"Already up to date ({db_count:,} trades in DB).")
        return 0

    # The API is oldest-first. Estimate where our data ends:
    # If DB has N trades and page size is 20, our data covers roughly pages 0..(N/20).
    # New trades live on pages (N/20)..(totalPages-1).
    # We go back OVERLAP_PAGES for safety.
    new_pages  = -(-new_estimate // PAGE_SIZE)   # ceiling division
    start_page = max(0, total_pages - new_pages - OVERLAP_PAGES)
    end_page   = total_pages - 1  # inclusive

    print(f"API:  {total_elements:,} trades across {total_pages:,} pages")
    print(f"DB:   {db_count:,} trades")
    print(f"New:  ~{new_estimate:,} trades — fetching pages {start_page}–{end_page}")

    conn     = db_connect()
    inserted = 0

    for page in range(start_page, end_page + 1):
        orders = api_fetch_page(page)
        if not orders:
            continue

        rows = [
            (
                o["id"],
                o["type"],
                o["price"],
                o.get("buyerId"),                              # standard MC UUID — resolvable
                o.get("oldOwnerId") or o.get("ownerId"),       # use whichever field is set
                None,                                          # buyer_name  (resolved on-demand)
                None,                                          # seller_name (resolved on-demand)
                None,                                          # ts — API provides no trade timestamp
            )
            for o in orders
        ]

        cur = conn.executemany(
            "INSERT OR IGNORE INTO trades "
            "(id, item, price, buyer_uuid, seller_uuid, buyer_name, seller_name, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        n = cur.rowcount
        inserted += n

        if n:
            print(f"  page {page}: +{n} new", flush=True)

        time.sleep(DELAY_BETWEEN_PAGES)

    conn.close()

    final_count = db_trade_count()
    if inserted:
        print(f"\nInserted {inserted:,} new trades. DB now has {final_count:,} total.")
    else:
        print(f"No new trades found. DB has {final_count:,} total.")

    return inserted


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Loka Market incremental updater")
    parser.add_argument(
        "--loop", type=int, default=0, metavar="SECONDS",
        help="Run continuously, sleeping SECONDS between updates (0 = run once)"
    )
    args = parser.parse_args()

    if args.loop:
        print(f"Running in loop mode (every {args.loop}s). Ctrl+C to stop.\n")
        while True:
            try:
                print(f"[{time.strftime('%H:%M:%S')}] Checking for new trades…")
                run_update()
                print(f"Sleeping {args.loop}s…\n")
                time.sleep(args.loop)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
    else:
        run_update()


if __name__ == "__main__":
    main()
