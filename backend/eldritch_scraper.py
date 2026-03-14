"""
EldritchBot player stats scraper with queue-based bulk processing.

Priorities:
  1 = fight participant (just fought — update ASAP)
  5 = normal / initial bulk
  9 = stale refresh (>30 days old)

Statuses in scrape_queue:
  pending   — waiting to be processed
  done      — successfully scraped
  not_found — player has no EldritchBot record (404 or blank page)
  error     — fetch failed after max attempts
"""

import os
import re
import json
import sqlite3
import time
import threading
import urllib.request
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH   = os.path.join(BASE_DIR, 'eldritch.db')   # player_stats + scrape_queue
LOKA_PATH = os.path.join(BASE_DIR, 'loka.db')        # loka_players UUID registry

_DELAY_HIT      = 0.02  # seconds between successful scrapes (per-thread slot)
_DELAY_MISS     = 0.0   # seconds after a 404 / not-found
_MAX_ATTEMPTS   = 3     # give up after this many errors
_REFRESH_DAYS   = 30    # re-queue records older than this
_NUM_THREADS    = 8     # concurrent scrape threads
_last_req_ts    = 0.0
_rate_lock      = threading.Lock()
_worker_running = False

# ── DB ────────────────────────────────────────────────────────────────────────

def ensure_tables():
    db = sqlite3.connect(DB_PATH)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS player_stats (
            uuid            TEXT PRIMARY KEY,
            name            TEXT,
            alliance        TEXT,
            kills           INTEGER DEFAULT 0,
            deaths          INTEGER DEFAULT 0,
            assists         INTEGER DEFAULT 0,
            food            INTEGER DEFAULT 0,
            potions         INTEGER DEFAULT 0,
            pearls          INTEGER DEFAULT 0,
            ancient_ingots  INTEGER DEFAULT 0,
            conquest_wins   INTEGER DEFAULT 0,
            conquest_losses INTEGER DEFAULT 0,
            golems          INTEGER DEFAULT 0,
            lamps           INTEGER DEFAULT 0,
            first_bloods    INTEGER DEFAULT 0,
            close_calls     INTEGER DEFAULT 0,
            nemesis         TEXT,
            nemesis_deaths  INTEGER DEFAULT 0,
            best_kda_score  TEXT,
            best_kda_fight  TEXT,
            last_fight      TEXT,
            scraped_ts      INTEGER,
            error           TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_eldr_kills ON player_stats(kills DESC);
        CREATE INDEX IF NOT EXISTS idx_eldr_wins  ON player_stats(conquest_wins DESC);
        CREATE INDEX IF NOT EXISTS idx_eldr_name  ON player_stats(name COLLATE NOCASE);

        CREATE TABLE IF NOT EXISTS scrape_queue (
            uuid            TEXT PRIMARY KEY,
            priority        INTEGER DEFAULT 5,
            queued_ts       INTEGER NOT NULL,
            status          TEXT    DEFAULT 'pending',
            attempts        INTEGER DEFAULT 0,
            last_attempt_ts INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_sq_status   ON scrape_queue(status, priority, queued_ts);
        CREATE INDEX IF NOT EXISTS idx_sq_priority ON scrape_queue(priority, queued_ts)
            WHERE status = 'pending';
    ''')
    db.commit()
    db.close()

def ensure_loka_tables():
    db = sqlite3.connect(LOKA_PATH)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS loka_players (
            uuid       TEXT PRIMARY KEY,
            name       TEXT,
            fetched_ts INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_lp_name ON loka_players(name COLLATE NOCASE);
    ''')
    db.commit()
    db.close()

# ── helpers ───────────────────────────────────────────────────────────────────

def _int(s) -> int:
    try:
        return int(str(s).replace(',', '').strip())
    except Exception:
        return 0

def _td_pairs(soup, section_text: str) -> dict:
    h4 = next((h for h in soup.find_all('h4') if section_text in h.get_text()), None)
    if not h4:
        return {}
    block = h4.find_next_sibling()
    if not block:
        return {}
    tds = block.find_all('td')
    result = {}
    for i in range(0, len(tds) - 1, 2):
        key = tds[i].get_text(strip=True).rstrip(':').lower().replace(' ', '_')
        val = tds[i + 1].get_text(strip=True).replace(',', '')
        result[key] = val
    return result

def fmt_uuid(raw: str) -> str:
    """Ensure UUID has dashes: 8-4-4-4-12."""
    s = raw.lower().replace('-', '')
    if len(s) == 32:
        return f'{s[:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}'
    return raw

# ── Loka player list ──────────────────────────────────────────────────────────

LOKA_PAGE_SIZE = 20

def _fetch_loka_page(page: int, retries: int = 4) -> list:
    """Fetch one page of Loka players. Returns list of (uuid, name|None) tuples."""
    url = f'https://api.lokamc.com/players?size={LOKA_PAGE_SIZE}&page={page}'
    req = urllib.request.Request(url, headers={'User-Agent': 'LokaUtils/1.0'})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            return [
                (fmt_uuid(p['uuid']), p.get('name') or None)
                for p in data.get('_embedded', {}).get('players', [])
                if p.get('uuid')
            ]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 10 * (attempt + 1)
                print(f'[loka] 429 rate limited on page {page}, waiting {wait}s…', flush=True)
                time.sleep(wait)
            elif attempt == retries - 1:
                raise
            else:
                time.sleep(2 ** attempt)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)

def load_loka_players(fetch_workers: int = 10) -> int:
    """
    Fetch every player UUID+name from api.lokamc.com/players using parallel requests.
    Returns count of players loaded. Safe to re-run (upserts names).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Get total page count first (with retries)
    total_pages = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(
                f'https://api.lokamc.com/players?size={LOKA_PAGE_SIZE}&page=0',
                headers={'User-Agent': 'LokaUtils/1.0'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                total_pages = json.loads(resp.read()).get('page', {}).get('totalPages', 1)
            break
        except Exception as e:
            print(f'[eldritch] could not fetch total pages (attempt {attempt+1}): {e}', flush=True)
            time.sleep(3 * (attempt + 1))
    if total_pages is None:
        return 0

    print(f'[eldritch] loading {total_pages:,} pages ({fetch_workers} threads)…', flush=True)

    db    = sqlite3.connect(LOKA_PATH)
    now   = int(time.time())
    total = 0
    lock  = threading.Lock()

    def insert_batch(players):
        nonlocal total
        with lock:
            for uuid, name in players:
                db.execute('''
                    INSERT INTO loka_players (uuid, name, fetched_ts) VALUES (?, ?, ?)
                    ON CONFLICT(uuid) DO UPDATE SET
                        name = COALESCE(excluded.name, loka_players.name),
                        fetched_ts = excluded.fetched_ts
                ''', (uuid, name, now))
            db.commit()
            total += len(players)
            if total % 5000 == 0:
                print(f'[eldritch] {total:,} / ~{total_pages * 20:,} players loaded…', flush=True)

    with ThreadPoolExecutor(max_workers=fetch_workers) as ex:
        futures = {ex.submit(_fetch_loka_page, p): p for p in range(total_pages)}
        for fut in as_completed(futures):
            try:
                insert_batch(fut.result())
            except Exception as e:
                print(f'[eldritch] page {futures[fut]} error: {e}')

    db.close()
    print(f'[eldritch] Loka player list done: {total:,} players', flush=True)
    return total

def queue_all_loka_players(priority: int = 5):
    """
    Add every UUID in loka_players to the scrape queue (skips already-done).
    """
    loka = sqlite3.connect(LOKA_PATH)
    db   = sqlite3.connect(DB_PATH)
    now  = int(time.time())
    uuids = [r[0] for r in loka.execute('SELECT uuid FROM loka_players').fetchall()]
    loka.close()
    cutoff = now - _REFRESH_DAYS * 86400
    added = 0
    for uuid in uuids:
        db.execute('''
            INSERT OR IGNORE INTO scrape_queue (uuid, priority, queued_ts, status)
            VALUES (?, ?, ?, 'pending')
            ON CONFLICT(uuid) DO UPDATE SET
                status = 'pending', priority = ?, queued_ts = ?, attempts = 0
            WHERE status = 'done' AND last_attempt_ts < ?
        ''', (uuid, priority, now, priority, now, cutoff))
    added = db.execute('SELECT changes()').fetchone()[0]
    db.commit()
    db.close()
    print(f'[eldritch] queued {added} players for scraping')
    return added

def queue_players(uuids: list, priority: int = 5):
    """Add a list of UUIDs to the scrape queue at the given priority."""
    if not uuids:
        return
    db  = sqlite3.connect(DB_PATH)
    now = int(time.time())
    for uuid in uuids:
        uuid = fmt_uuid(uuid)
        # If already pending with lower priority, upgrade it
        db.execute('''
            INSERT INTO scrape_queue (uuid, priority, queued_ts, status)
            VALUES (?, ?, ?, 'pending')
            ON CONFLICT(uuid) DO UPDATE SET
                priority  = MIN(excluded.priority, scrape_queue.priority),
                queued_ts = excluded.queued_ts,
                status    = CASE WHEN scrape_queue.status IN ('done','not_found')
                                 THEN 'pending' ELSE scrape_queue.status END
            WHERE scrape_queue.status != 'pending'
               OR excluded.priority < scrape_queue.priority
        ''', (uuid, priority, now))
    db.commit()
    db.close()

def queue_by_names(names: list, priority: int = 1):
    """
    Resolve player names → UUIDs via the local loka_players table,
    then queue them. Used after a fight is parsed.
    """
    if not names:
        return
    db = sqlite3.connect(LOKA_PATH)
    placeholders = ','.join('?' * len(names))
    rows = db.execute(
        f'SELECT uuid FROM loka_players WHERE name IN ({placeholders}) COLLATE NOCASE',
        names
    ).fetchall()
    db.close()
    uuids = [r[0] for r in rows]
    if uuids:
        queue_players(uuids, priority=priority)
        print(f'[eldritch] fight update: queued {len(uuids)}/{len(names)} players at priority {priority}')

def get_queue_stats() -> dict:
    db   = sqlite3.connect(DB_PATH)
    loka = sqlite3.connect(LOKA_PATH)
    rows = db.execute('''
        SELECT status, COUNT(*) as cnt FROM scrape_queue GROUP BY status
    ''').fetchall()
    total_loka = loka.execute('SELECT COUNT(*) FROM loka_players').fetchone()[0]
    total_eb   = db.execute('SELECT COUNT(*) FROM player_stats WHERE error IS NULL AND name != ""').fetchone()[0]
    loka.close()
    db.close()
    counts = {r[0]: r[1] for r in rows}
    pending = counts.get('pending', 0)
    eta_min = round(pending * _DELAY_HIT / 60, 1)
    return {
        'loka_players':   total_loka,
        'eb_records':     total_eb,
        'pending':        pending,
        'done':           counts.get('done', 0),
        'not_found':      counts.get('not_found', 0),
        'errors':         counts.get('error', 0),
        'eta_minutes':    eta_min,
    }

# ── scraper ───────────────────────────────────────────────────────────────────

def fetch_player(uuid: str, delay: float = _DELAY_HIT) -> dict:
    """Scrape stats for uuid from eldritchbot.com. Returns a stats dict."""
    global _last_req_ts
    uuid     = fmt_uuid(uuid)
    uuid_raw = uuid.replace('-', '')
    url      = f'https://eldritchbot.com/player/{uuid_raw}'

    with _rate_lock:
        gap = delay - (time.time() - _last_req_ts)
        if gap > 0:
            time.sleep(gap)
        _last_req_ts = time.time()

    resp = cffi_requests.get(url, impersonate='chrome120', timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    h1s = soup.find_all('h1')
    h3s = soup.find_all('h3')
    name     = h1s[0].get_text(strip=True) if h1s else ''
    alliance = h3s[0].get_text(strip=True) if h3s else ''

    # If name is empty, EB doesn't have this player
    if not name:
        return {'uuid': uuid, 'error': 'not_found', 'scraped_ts': int(time.time())}

    best_kda_score = h1s[1].get_text(strip=True) if len(h1s) > 1 else ''

    conquest_wins_h4 = 0
    last_fight = ''
    best_kda_fight = ''
    for h4 in soup.find_all('h4'):
        txt = h4.get_text(strip=True)
        if txt.startswith('Wins:'):
            conquest_wins_h4 = _int(txt[5:])
        elif txt.startswith('Last Fight:'):
            last_fight = txt[11:].strip()
        elif 'Best KDA' in txt:
            sib = h4.find_next_sibling()
            if sib:
                best_kda_fight = sib.get_text(strip=True)

    nemesis = ''
    nemesis_deaths = 0
    for h2 in soup.find_all('h2'):
        nemesis = h2.get_text(strip=True)
        sib = h2.find_next_sibling()
        while sib:
            if sib.name == 'h3':
                m = re.search(r'(\d+)', sib.get_text())
                if m:
                    nemesis_deaths = int(m.group(1))
                break
            sib = sib.find_next_sibling()
        break

    combat      = _td_pairs(soup, 'Combat')
    consumables = _td_pairs(soup, 'Consumables')
    conquest    = _td_pairs(soup, 'Conquest')

    return {
        'uuid':            uuid,
        'name':            name,
        'alliance':        alliance,
        'kills':           _int(combat.get('kills',   0)),
        'deaths':          _int(combat.get('deaths',  0)),
        'assists':         _int(combat.get('assists', 0)),
        'food':            _int(consumables.get('food',           0)),
        'potions':         _int(consumables.get('potions',        0)),
        'pearls':          _int(consumables.get('pearls',         0)),
        'ancient_ingots':  _int(consumables.get('ancient_ingots', 0)),
        'conquest_wins':   _int(conquest.get('wins',   0)) or conquest_wins_h4,
        'conquest_losses': _int(conquest.get('losses', 0)),
        'golems':          _int(conquest.get('golems', 0)),
        'lamps':           _int(conquest.get('lamps',  0)),
        'first_bloods':    _int(conquest.get('first_bloods', 0)),
        'close_calls':     _int(conquest.get('close_calls',  0)),
        'nemesis':         nemesis,
        'nemesis_deaths':  nemesis_deaths,
        'best_kda_score':  best_kda_score,
        'best_kda_fight':  best_kda_fight,
        'last_fight':      last_fight,
        'scraped_ts':      int(time.time()),
        'error':           None,
    }

# ── DB helpers ────────────────────────────────────────────────────────────────

def save_player(data: dict):
    db = sqlite3.connect(DB_PATH)
    db.execute('''
        INSERT OR REPLACE INTO player_stats
        (uuid, name, alliance, kills, deaths, assists, food, potions, pearls,
         ancient_ingots, conquest_wins, conquest_losses, golems, lamps,
         first_bloods, close_calls, nemesis, nemesis_deaths, best_kda_score,
         best_kda_fight, last_fight, scraped_ts, error)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        data['uuid'], data.get('name',''), data.get('alliance',''),
        data.get('kills',0), data.get('deaths',0), data.get('assists',0),
        data.get('food',0), data.get('potions',0), data.get('pearls',0),
        data.get('ancient_ingots',0), data.get('conquest_wins',0),
        data.get('conquest_losses',0), data.get('golems',0), data.get('lamps',0),
        data.get('first_bloods',0), data.get('close_calls',0),
        data.get('nemesis',''), data.get('nemesis_deaths',0),
        data.get('best_kda_score',''), data.get('best_kda_fight',''),
        data.get('last_fight',''), data.get('scraped_ts', int(time.time())),
        data.get('error'),
    ))
    db.commit()
    db.close()
    # Also write name into loka.db so search autocomplete picks it up immediately
    name = data.get('name', '')
    if name:
        loka = sqlite3.connect(LOKA_PATH)
        loka.execute('UPDATE loka_players SET name = ? WHERE uuid = ? AND name IS NULL',
                     (name, data['uuid']))
        loka.commit()
        loka.close()

def get_player(uuid: str) -> dict | None:
    uuid = fmt_uuid(uuid)
    db   = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row  = db.execute('SELECT * FROM player_stats WHERE uuid=?', (uuid,)).fetchone()
    db.close()
    return dict(row) if row else None

def get_leaderboard(sort='kills', limit=50) -> list:
    valid = {'kills','deaths','assists','conquest_wins','conquest_losses',
             'golems','lamps','first_bloods','potions','food','close_calls'}
    db  = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    if sort == 'kda':
        rows = db.execute(
            '''SELECT *, CAST(kills AS REAL) / CASE WHEN deaths = 0 THEN 1 ELSE deaths END AS kd_ratio
               FROM player_stats WHERE error IS NULL AND name != "" AND kills >= 50
               ORDER BY kd_ratio DESC LIMIT ?''',
            (limit,)
        ).fetchall()
        db.close()
        return [dict(r) for r in rows]
    if sort == 'kda_worst':
        rows = db.execute(
            '''SELECT *, CAST(kills AS REAL) / CASE WHEN deaths = 0 THEN 1 ELSE deaths END AS kd_ratio
               FROM player_stats WHERE error IS NULL AND name != "" AND kills >= 50
               ORDER BY kd_ratio ASC LIMIT ?''',
            (limit,)
        ).fetchall()
        db.close()
        return [dict(r) for r in rows]
    col = sort if sort in valid else 'kills'
    rows = db.execute(
        f'SELECT * FROM player_stats WHERE error IS NULL AND name != "" ORDER BY {col} DESC LIMIT ?',
        (limit,)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]

def search_players(q: str) -> list:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    # Check player_stats first, also check loka_players for name lookup
    rows = db.execute(
        'SELECT * FROM player_stats WHERE name LIKE ? AND error IS NULL ORDER BY kills DESC LIMIT 20',
        (f'%{q}%',)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]

def name_to_uuid(name: str) -> str | None:
    """Resolve player name → UUID. Checks local loka_players first, then Mojang API."""
    # Fast path: local DB
    db  = sqlite3.connect(LOKA_PATH)
    row = db.execute(
        'SELECT uuid FROM loka_players WHERE name = ? COLLATE NOCASE LIMIT 1', (name,)
    ).fetchone()
    db.close()
    if row:
        return row[0]
    # Fallback: Mojang API
    try:
        url = f'https://api.mojang.com/users/profiles/minecraft/{name}'
        req = urllib.request.Request(url, headers={'User-Agent': 'LokaUtils/1.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read()).get('id', '')
            return fmt_uuid(raw) if len(raw) == 32 else None
    except Exception:
        return None

# ── Queue worker ──────────────────────────────────────────────────────────────

def _next_job(db) -> tuple | None:
    """Claim the next pending job from the queue. Returns (uuid,) or None."""
    row = db.execute('''
        SELECT uuid FROM scrape_queue
        WHERE status = 'pending' AND attempts < ?
        ORDER BY priority ASC, queued_ts ASC
        LIMIT 1
    ''', (_MAX_ATTEMPTS,)).fetchone()
    if not row:
        return None
    uuid = row[0]
    db.execute('''
        UPDATE scrape_queue
        SET status = 'running', last_attempt_ts = ?, attempts = attempts + 1
        WHERE uuid = ?
    ''', (int(time.time()), uuid))
    db.commit()
    return uuid

def _mark_job(db, uuid: str, status: str):
    db.execute('''
        UPDATE scrape_queue SET status = ?, last_attempt_ts = ? WHERE uuid = ?
    ''', (status, int(time.time()), uuid))
    db.commit()

def _worker_thread(exit_when_empty: bool, stop_event: threading.Event):
    """Single worker thread — claims jobs from the queue and scrapes them."""
    while not stop_event.is_set():
        db   = sqlite3.connect(DB_PATH)
        uuid = _next_job(db)

        if uuid is None:
            db.close()
            if exit_when_empty:
                return
            stop_event.wait(30)
            continue

        try:
            data = fetch_player(uuid, delay=_DELAY_MISS)
            if data.get('error') == 'not_found':
                save_player(data)
                _mark_job(db, uuid, 'not_found')
            else:
                # Hit — hold for a moment to avoid hammering after a real page
                with _rate_lock:
                    time.sleep(max(0, _DELAY_HIT - _DELAY_MISS))
                save_player(data)
                _mark_job(db, uuid, 'done')
        except Exception as e:
            attempts = db.execute(
                'SELECT attempts FROM scrape_queue WHERE uuid=?', (uuid,)
            ).fetchone()
            attempts = attempts[0] if attempts else 1
            status = 'error' if attempts >= _MAX_ATTEMPTS else 'pending'
            _mark_job(db, uuid, status)
            if not exit_when_empty:
                print(f'[eldritch-worker] {uuid}: {e}')

        db.close()

def run_scrape_worker(exit_when_empty: bool = False, threads: int = _NUM_THREADS):
    """
    Process the scrape queue using multiple threads.

    exit_when_empty=True  → return when queue is empty (local build mode).
    exit_when_empty=False → sleep and re-queue stale records forever (server mode).
    """
    global _worker_running
    _worker_running = True
    if not exit_when_empty:
        print(f'[eldritch-worker] started ({threads} threads)')

    stop_event = threading.Event()

    if threads == 1:
        _worker_thread(exit_when_empty, stop_event)
        return

    # Multi-threaded: spin up N threads and wait for all to finish
    pool = [
        threading.Thread(target=_worker_thread, args=(exit_when_empty, stop_event), daemon=True)
        for _ in range(threads)
    ]
    for t in pool:
        t.start()

    if exit_when_empty:
        for t in pool:
            t.join()
    else:
        # Server mode: also run stale re-queue loop in background
        while True:
            time.sleep(300)
            _requeue_stale()

def _requeue_stale():
    """Re-queue done records older than _REFRESH_DAYS at priority 9."""
    cutoff = int(time.time()) - _REFRESH_DAYS * 86400
    db  = sqlite3.connect(DB_PATH)
    now = int(time.time())
    db.execute('''
        UPDATE scrape_queue
        SET status = 'pending', priority = 9, queued_ts = ?, attempts = 0
        WHERE status = 'done' AND last_attempt_ts < ?
    ''', (now, cutoff))
    n = db.execute('SELECT changes()').fetchone()[0]
    if n:
        print(f'[eldritch-worker] re-queued {n} stale records')
    db.commit()
    db.close()

def start_worker():
    """Start the background scrape worker thread (call once at startup)."""
    t = threading.Thread(target=run_scrape_worker, daemon=True)
    t.start()
    return t

ensure_tables()
