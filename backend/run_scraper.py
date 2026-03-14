"""
run_scraper.py — CLI runner for the EldritchBot scrape queue.

Wraps eldritch_scraper with live progress output by polling the DB
alongside the worker threads.

Usage:
    python run_scraper.py [--threads N]
"""

import sys
import time
import sqlite3
import threading
import argparse

sys.path.insert(0, '.')
import eldritch_scraper as es

def _progress_loop(stop: threading.Event, interval: float = 2.0):
    last_done = 0
    while not stop.is_set():
        stop.wait(interval)
        db = sqlite3.connect(es.DB_PATH)
        db.row_factory = sqlite3.Row
        row = db.execute('''
            SELECT
                SUM(status = "pending")   AS pending,
                SUM(status = "done")      AS done,
                SUM(status = "not_found") AS not_found,
                SUM(status = "error")     AS errors,
                SUM(status = "running")   AS running
            FROM scrape_queue
        ''').fetchone()
        last_row = db.execute('''
            SELECT ps.name, ps.kills, ps.deaths, ps.assists
            FROM player_stats ps
            JOIN scrape_queue sq ON sq.uuid = ps.uuid
            WHERE sq.status = "done" AND ps.name != ""
            ORDER BY sq.last_attempt_ts DESC
            LIMIT 1
        ''').fetchone()
        db.close()

        done    = row['done'] or 0
        pending = row['pending'] or 0
        nf      = row['not_found'] or 0
        errors  = row['errors'] or 0
        new     = done - last_done
        last_done = done

        eta = f'{round(pending * es._DELAY_HIT / 60)}min' if pending else 'done'
        recent = ''
        if last_row and last_row['name']:
            recent = f'  last: {last_row["name"]} (K:{last_row["kills"]} D:{last_row["deaths"]} A:{last_row["assists"]})'

        print(
            f'[scraper] done={done:,}  pending={pending:,}  not_found={nf:,}  '
            f'errors={errors}  eta={eta}{recent}',
            flush=True
        )

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--threads', type=int, default=6)
    args = parser.parse_args()

    stats = es.get_queue_stats()
    print(f'[scraper] starting — {stats["pending"]:,} pending  |  {stats["eb_records"]:,} already scraped  |  {args.threads} threads')

    stop = threading.Event()
    prog = threading.Thread(target=_progress_loop, args=(stop,), daemon=True)
    prog.start()

    try:
        es.run_scrape_worker(exit_when_empty=True, threads=args.threads)
    finally:
        stop.set()

    final = es.get_queue_stats()
    print(f'\n[scraper] complete — {final["eb_records"]:,} scraped  |  {final["not_found"]:,} not found  |  {final["errors"]} errors')

if __name__ == '__main__':
    main()
