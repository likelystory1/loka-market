"""
eb_fight_scraper.py — EldritchBot fight scraper.

Enumerates fight IDs via Socket.IO pagination, fetches each /fight?id= page,
extracts the embedded fightData JSON, and stores results in eb_fights.db.

Usage:
    python eb_fight_scraper.py            # incremental: new fights from homepage only
    python eb_fight_scraper.py --full     # full historical scrape via Socket.IO
"""

import os, re, json, sqlite3, time, threading, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'eb_fights.db')

HOME_URL       = 'https://eldritchbot.com'
FIGHT_URL      = 'https://eldritchbot.com/fight?id={}'
_DELAY         = 0.4   # seconds between fight page fetches

WORLD_MAP = {
    'rivina': 'Rivina', 'kalros': 'Kalros', 'garama': 'Garama',
    'ascalon': 'Ascalon', 'balak': 'Balak',
}
MONTHS_ABBR = {
    'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
    'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12,
}

# ── DB ────────────────────────────────────────────────────────────────────────

def ensure_tables():
    db = sqlite3.connect(DB_PATH)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS eb_fights (
            id              TEXT PRIMARY KEY,
            title           TEXT,
            location        TEXT,
            world           TEXT,
            attacker_town   TEXT,
            defender_town   TEXT,
            attackers_won   INTEGER,
            fight_date      TEXT,
            fight_time      TEXT,
            timestamp       INTEGER,
            duration_mins   REAL,
            total_players   INTEGER,
            attacker_pkills INTEGER DEFAULT 0,
            attacker_gkills INTEGER DEFAULT 0,
            defender_pkills INTEGER DEFAULT 0,
            defender_gkills INTEGER DEFAULT 0,
            fight_data      TEXT,
            scraped_ts      INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_ebf_ts    ON eb_fights(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_ebf_loc   ON eb_fights(location);
        CREATE INDEX IF NOT EXISTS idx_ebf_atk   ON eb_fights(attacker_town);
        CREATE INDEX IF NOT EXISTS idx_ebf_def   ON eb_fights(defender_town);

        CREATE TABLE IF NOT EXISTS eb_fight_players (
            fight_id       TEXT    NOT NULL,
            side           TEXT    NOT NULL,
            name           TEXT    NOT NULL,
            uuid           TEXT,
            town           TEXT,
            pkills         INTEGER DEFAULT 0,
            gkills         INTEGER DEFAULT 0,
            deaths         INTEGER DEFAULT 0,
            assists        REAL    DEFAULT 0,
            lamps          INTEGER DEFAULT 0,
            food           INTEGER DEFAULT 0,
            potions        INTEGER DEFAULT 0,
            pearls         INTEGER DEFAULT 0,
            ancient_ingots INTEGER DEFAULT 0,
            close_calls    INTEGER DEFAULT 0,
            PRIMARY KEY (fight_id, name)
        );
        CREATE INDEX IF NOT EXISTS idx_efp_fight ON eb_fight_players(fight_id);
        CREATE INDEX IF NOT EXISTS idx_efp_name  ON eb_fight_players(name);
        CREATE INDEX IF NOT EXISTS idx_efp_uuid  ON eb_fight_players(uuid);
    ''')
    db.commit()
    db.close()

def fight_exists(fight_id: str) -> bool:
    db = sqlite3.connect(DB_PATH)
    row = db.execute('SELECT 1 FROM eb_fights WHERE id=?', (fight_id,)).fetchone()
    db.close()
    return row is not None

def save_fight(fight: dict):
    db = sqlite3.connect(DB_PATH)
    db.execute('''
        INSERT OR REPLACE INTO eb_fights
        (id, title, location, world, attacker_town, defender_town, attackers_won,
         fight_date, fight_time, timestamp, duration_mins, total_players,
         attacker_pkills, attacker_gkills, defender_pkills, defender_gkills,
         fight_data, scraped_ts)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        fight['id'], fight['title'], fight['location'], fight['world'],
        fight['attacker_town'], fight['defender_town'], fight['attackers_won'],
        fight['fight_date'], fight['fight_time'], fight['timestamp'],
        fight['duration_mins'], fight['total_players'],
        fight['attacker_pkills'], fight['attacker_gkills'],
        fight['defender_pkills'], fight['defender_gkills'],
        json.dumps(fight['fight_data'], separators=(',', ':')),
        int(time.time()),
    ))

    fd = fight['fight_data']
    for side_key, side_label in [('attackers', 'attacker'), ('defenders', 'defender')]:
        for p in fd.get(side_key, {}).get('players', []):
            db.execute('''
                INSERT OR REPLACE INTO eb_fight_players
                (fight_id, side, name, uuid, town, pkills, gkills, deaths,
                 assists, lamps, food, potions, pearls, ancient_ingots, close_calls)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                fight['id'], side_label,
                p.get('name', ''), p.get('uuid', ''), p.get('town', ''),
                p.get('pkills', 0), p.get('gkills', 0), p.get('deaths', 0),
                p.get('assists', 0), p.get('lamps', 0),
                p.get('food', 0), p.get('potions', 0),
                p.get('pearls', 0), p.get('ancientIngots', 0),
                p.get('closeCalls', 0),
            ))

    db.commit()
    db.close()

def get_fight(fight_id: str) -> dict | None:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute('SELECT * FROM eb_fights WHERE id=?', (fight_id,)).fetchone()
    db.close()
    if not row:
        return None
    r = dict(row)
    r['fight_data'] = json.loads(r['fight_data']) if r['fight_data'] else {}
    return r

def list_fights(limit=50, offset=0, town=None, world=None) -> dict:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    where_clauses = []
    params = []
    if town:
        where_clauses.append('(attacker_town LIKE ? OR defender_town LIKE ?)')
        params += [f'%{town}%', f'%{town}%']
    if world:
        where_clauses.append('world = ?')
        params.append(world)
    where = ('WHERE ' + ' AND '.join(where_clauses)) if where_clauses else ''
    total = db.execute(f'SELECT COUNT(*) FROM eb_fights {where}', params).fetchone()[0]
    rows = db.execute(
        f'SELECT id,title,location,world,attacker_town,defender_town,attackers_won,'
        f'fight_date,fight_time,timestamp,duration_mins,total_players,'
        f'attacker_pkills,attacker_gkills,defender_pkills,defender_gkills '
        f'FROM eb_fights {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?',
        params + [limit, offset]
    ).fetchall()
    db.close()
    return {'fights': [dict(r) for r in rows], 'total': total}

def get_stats() -> dict:
    db = sqlite3.connect(DB_PATH)
    total   = db.execute('SELECT COUNT(*) FROM eb_fights').fetchone()[0]
    players = db.execute('SELECT COUNT(DISTINCT name) FROM eb_fight_players').fetchone()[0]
    db.close()
    return {'total_fights': total, 'unique_players': players}

# ── Fight ID enumeration ──────────────────────────────────────────────────────

def get_homepage_ids() -> list[str]:
    """Get fight IDs from the pre-rendered homepage HTML (page 0, 8 fights)."""
    try:
        resp = cffi_requests.get(HOME_URL, impersonate='chrome120', timeout=20)
        soup = BeautifulSoup(resp.text, 'html.parser')
        return [r['data-href'] for r in soup.find_all('tr', class_='clickable-row')
                if r.get('data-href')]
    except Exception as e:
        print(f'[eb-fights] homepage error: {e}')
        return []

def get_all_ids_via_fightlist(min_players: int = 5,
                              stop_on_existing: bool = False) -> list[str]:
    """
    Paginate https://eldritchbot.com/fightlist?page=N (8 fights per page).
    Skips fights vs "Wilds" or with fewer than min_players participants.
    Stops when a page returns no fight IDs.
    """
    all_ids: list[str] = []
    page = 1

    # Row pattern inside the table:
    #   fight?id=XXXX  and  the row text has attacker/defender/participants
    RE_ROW = re.compile(
        r'fight\?id=([A-Za-z0-9_\-\$\@\!\#\%\^]+)[^<]*</a>\s*</td>\s*</tr>',
        re.DOTALL,
    )
    # Simpler: parse each <tr> for id + participants
    RE_FIGHTID = re.compile(r'fight\?id=([A-Za-z0-9_\-\$\@\!\#\%\^]+)')

    # Create one session; refresh CF cookies every 200 pages
    session = cffi_requests.Session(impersonate='chrome120')
    session.get(HOME_URL, timeout=20)
    last_refresh = page

    while True:
        # Refresh CF cookies every 200 pages to avoid expiry
        if page - last_refresh >= 200:
            try:
                session = cffi_requests.Session(impersonate='chrome120')
                session.get(HOME_URL, timeout=20)
                last_refresh = page
            except Exception:
                pass

        try:
            r = session.get(f'{HOME_URL}/fightlist', params={'page': page}, timeout=30)
        except Exception as e:
            print(f'[eb-fights] fightlist page {page} error: {e} — retrying')
            time.sleep(3)
            # Refresh session on error
            try:
                session = cffi_requests.Session(impersonate='chrome120')
                session.get(HOME_URL, timeout=20)
                last_refresh = page
            except Exception:
                pass
            continue

        soup = BeautifulSoup(r.text, 'html.parser')
        rows = soup.find_all('tr')

        # Skip header row; collect data rows
        page_ids: list[str] = []
        server_rows = 0  # rows with actual fight data from server
        for row in rows:
            cells = [td.get_text(strip=True) for td in row.find_all('td')]
            if len(cells) < 4:
                continue  # header or malformed
            server_rows += 1
            # cells: [date, attacker, defender, participants, view_button]
            attacker    = cells[1] if len(cells) > 1 else ''
            defender    = cells[2] if len(cells) > 2 else ''
            participants_str = cells[3] if len(cells) > 3 else '0'
            try:
                participants = int(re.sub(r'\D', '', participants_str) or '0')
            except ValueError:
                participants = 0

            # Filters: skip Wilds fights and low-player fights
            if 'wilds' in attacker.lower() or 'wilds' in defender.lower():
                continue
            if participants < min_players:
                continue

            # Extract fight ID from the View link inside this row
            id_match = RE_FIGHTID.search(str(row))
            if id_match:
                page_ids.append(id_match.group(1))

        # Stop only when the server returns no rows (past last page)
        if server_rows == 0:
            print(f'[eb-fights] fightlist page {page} server-empty — done ({len(all_ids):,} IDs total)')
            break

        # Check stop_on_existing
        if stop_on_existing:
            new_on_page = [fid for fid in page_ids if not fight_exists(fid)]
            if not new_on_page and page > 1:
                print(f'[eb-fights] page {page} all existing — stopping')
                break

        all_ids.extend(page_ids)
        if page % 100 == 0:
            print(f'[eb-fights] fightlist page {page}/~3976 ({len(all_ids):,} qualifying IDs so far)…')

        page += 1
        time.sleep(0.3)

    return all_ids

# ── Fight page parser ─────────────────────────────────────────────────────────

RE_SUBTITLE  = re.compile(
    r'^(.+?)\s*[-–]\s*(\d+:\d+)\s*[-–]\s*(.+?\d{4})\s*[-–]\s*(\d+)\s*players?',
    re.IGNORECASE
)
RE_ATK_PKILLS = re.compile(r'var attackerPKills\s*=\s*(\d+)')
RE_ATK_GKILLS = re.compile(r'var attackerGKills\s*=\s*(\d+)')
RE_DEF_PKILLS = re.compile(r'var defenderPKills\s*=\s*(\d+)')
RE_DEF_GKILLS = re.compile(r'var defenderGKills\s*=\s*(\d+)')

def _extract_fight_data_json(html: str) -> dict | None:
    """Extract the fightData JSON object from the page script, handling nested braces."""
    marker = 'var fightData = '
    start  = html.find(marker)
    if start == -1:
        return None
    start += len(marker)
    depth = 0
    for i, ch in enumerate(html[start:], start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:i + 1])
                except Exception:
                    return None
    return None

def _parse_ts(date_str: str, time_str: str) -> int:
    try:
        import datetime
        parts = date_str.split()
        month = MONTHS_ABBR.get(parts[0], 1)
        day, year = int(parts[1]), int(parts[2])
        h, m = map(int, time_str.split(':'))
        return int(datetime.datetime(year, month, day, h, m).timestamp())
    except Exception:
        return 0

def scrape_fight(fight_id: str) -> dict | None:
    """Fetch /fight?id=<id> and return a parsed fight dict, or None on failure."""
    try:
        resp = cffi_requests.get(FIGHT_URL.format(fight_id), impersonate='chrome120', timeout=20)
        if resp.status_code != 200:
            return None
    except Exception as e:
        print(f'[eb-fights] fetch {fight_id}: {e}')
        return None

    html = resp.text
    soup = BeautifulSoup(html, 'html.parser')

    fight_data = _extract_fight_data_json(html)
    if not fight_data:
        return None

    # Script-level kill counts
    def _si(pat): m = pat.search(html); return int(m.group(1)) if m else 0
    apk, agk = _si(RE_ATK_PKILLS), _si(RE_ATK_GKILLS)
    dpk, dgk = _si(RE_DEF_PKILLS), _si(RE_DEF_GKILLS)

    # Title
    h1s   = soup.find_all('h1')
    title = h1s[0].get_text(strip=True) if h1s else ''

    # Subtitle: "Rivina - 17:10 - Mar 14 2026 - 15 players"
    world = fight_time = fight_date = ''
    total_players = len(fight_data.get('playerNames', []))
    for tag in soup.find_all(['h5', 'h4', 'p']):
        txt = tag.get_text(strip=True)
        ms  = RE_SUBTITLE.search(txt)
        if ms:
            world         = WORLD_MAP.get(ms.group(1).strip().lower(), ms.group(1).strip())
            fight_time    = ms.group(2).strip()
            fight_date    = ms.group(3).strip()
            total_players = int(ms.group(4))
            break

    # Town names: h1[1] = attacker, h1[2] = defender (h1[0] is the fight title)
    attacker_town = h1s[1].get_text(strip=True) if len(h1s) > 1 else ''
    defender_town = h1s[2].get_text(strip=True) if len(h1s) > 2 else ''

    # Fallback: pull from fightData player towns
    if not attacker_town:
        ps = fight_data.get('attackers', {}).get('players', [])
        attacker_town = ps[0].get('town', '') if ps else ''
    if not defender_town:
        ps = fight_data.get('defenders', {}).get('players', [])
        defender_town = ps[0].get('town', '') if ps else ''

    return {
        'id':             fight_id,
        'title':          title,
        'location':       fight_data.get('location', ''),
        'world':          world,
        'attacker_town':  attacker_town,
        'defender_town':  defender_town,
        'attackers_won':  1 if fight_data.get('attackersWon') else 0,
        'fight_date':     fight_date,
        'fight_time':     fight_time,
        'timestamp':      _parse_ts(fight_date, fight_time),
        'duration_mins':  fight_data.get('length', 0),
        'total_players':  total_players,
        'attacker_pkills': apk,
        'attacker_gkills': agk,
        'defender_pkills': dpk,
        'defender_gkills': dgk,
        'fight_data':     fight_data,
    }

# ── Run modes ─────────────────────────────────────────────────────────────────

def run_incremental() -> int:
    """Check homepage for new fights and scrape any not already in DB."""
    ensure_tables()
    ids = get_homepage_ids()
    new = 0
    for fid in ids:
        if fight_exists(fid):
            continue
        fight = scrape_fight(fid)
        if fight:
            save_fight(fight)
            new += 1
            print(f'[eb-fights] +{fid}: {fight["attacker_town"]} vs {fight["defender_town"]} ({fight["fight_date"]})')
            time.sleep(_DELAY)
    return new

RE_FIGHTID = re.compile(r'fight\?id=([A-Za-z0-9_\-\$\@\!\#\%\^]+)')

def _parse_fightlist_html(html: str) -> tuple[list[str], int]:
    """Parse fightlist HTML, return (qualifying_ids, server_row_count)."""
    soup = BeautifulSoup(html, 'html.parser')
    rows = soup.find_all('tr')
    ids: list[str] = []
    server_rows = 0
    for row in rows:
        cells = [td.get_text(strip=True) for td in row.find_all('td')]
        if len(cells) < 4:
            continue
        server_rows += 1
        attacker     = cells[1] if len(cells) > 1 else ''
        defender     = cells[2] if len(cells) > 2 else ''
        players_str  = cells[3] if len(cells) > 3 else '0'
        try:
            players = int(re.sub(r'\D', '', players_str) or '0')
        except ValueError:
            players = 0
        if 'wilds' in attacker.lower() or 'wilds' in defender.lower():
            continue
        if players < 5:
            continue
        m = RE_FIGHTID.search(str(row))
        if m:
            ids.append(m.group(1))
    return ids, server_rows


def _fightlist_chunk_worker(pages: list[int], result_map: dict, stop_event: threading.Event):
    """Worker: fetch a range of fightlist pages sequentially with one session."""
    session = cffi_requests.Session(impersonate='chrome120')
    session.get(HOME_URL, timeout=20)
    last_refresh = 0

    for i, page in enumerate(pages):
        if stop_event.is_set():
            break
        if i - last_refresh >= 150:
            try:
                session = cffi_requests.Session(impersonate='chrome120')
                session.get(HOME_URL, timeout=20)
                last_refresh = i
            except Exception:
                pass
        for attempt in range(3):
            try:
                r = session.get(f'{HOME_URL}/fightlist', params={'page': page}, timeout=30)
                ids, srv = _parse_fightlist_html(r.text)
                result_map[page] = (ids, srv)
                if srv == 0:
                    stop_event.set()
                break
            except Exception as e:
                if attempt == 2:
                    result_map[page] = ([], -1)
                else:
                    time.sleep(1)


_save_lock = threading.Lock()
_json_dir: str | None = None  # set before threads start

def _scrape_and_save(fid: str) -> tuple[str, dict | None]:
    """Fetch, parse, and save one fight. Returns ('new'|'skip'|'error', fight|None)."""
    if fight_exists(fid):
        return ('skip', None)
    fight = scrape_fight(fid)
    if fight:
        with _save_lock:
            save_fight(fight)
        # Write individual JSON file if export dir configured
        if _json_dir:
            out = {
                'id':            fight['id'],
                'attacker_town': fight['attacker_town'],
                'defender_town': fight['defender_town'],
                'attackers_won': bool(fight['attackers_won']),
                'world':         fight['world'],
                'fight_date':    fight['fight_date'],
                'fight_time':    fight['fight_time'],
                'total_players': fight['total_players'],
                'duration_mins': fight['duration_mins'],
                'attacker_pkills': fight['attacker_pkills'],
                'attacker_gkills': fight['attacker_gkills'],
                'defender_pkills': fight['defender_pkills'],
                'defender_gkills': fight['defender_gkills'],
                'fight_data':    fight['fight_data'],
            }
            path = os.path.join(_json_dir, f"{fid}.json")
            with open(path, 'w') as f:
                json.dump(out, f, separators=(',', ':'))
        return ('new', fight)
    return ('error', None)


def run_full(stop_on_existing: bool = False, workers: int = 12, max_pages: int = 4000,
            json_dir: str | None = None):
    """Full threaded scrape: enumerate via fightlist pages in parallel, then scrape fights."""
    ensure_tables()
    print(f'[eb-fights] enumerating fightlist pages 1-{max_pages} with {workers} worker threads…')

    # ── Phase 1: enumerate all qualifying fight IDs ────────────────────────────
    # Split pages into chunks, one chunk per worker thread
    all_pages = list(range(1, max_pages + 1))
    chunk_size = (len(all_pages) + workers - 1) // workers
    chunks = [all_pages[i:i + chunk_size] for i in range(0, len(all_pages), chunk_size)]

    result_map: dict[int, tuple[list[str], int]] = {}
    stop_event = threading.Event()

    threads = []
    for chunk in chunks:
        t = threading.Thread(target=_fightlist_chunk_worker,
                             args=(chunk, result_map, stop_event))
        t.daemon = True
        t.start()
        threads.append(t)
        time.sleep(0.2)  # slight stagger to avoid simultaneous CF handshakes

    # Monitor progress
    while any(t.is_alive() for t in threads):
        time.sleep(10)
        done = len(result_map)
        qualifying = sum(len(v[0]) for v in result_map.values())
        print(f'[eb-fights] enumerated {done}/{max_pages} pages, {qualifying:,} qualifying IDs…')

    for t in threads:
        t.join()

    # Collect IDs in page order
    all_ids: list[str] = []
    last_valid_page = 0
    for pg in sorted(result_map.keys()):
        ids, srv = result_map[pg]
        if srv > 0:
            all_ids.extend(ids)
            last_valid_page = pg

    print(f'[eb-fights] {len(all_ids):,} qualifying fight IDs across {last_valid_page} pages')

    # ── Phase 2: scrape each fight page ───────────────────────────────────────
    ids_to_fetch = [fid for fid in all_ids if not fight_exists(fid)]
    print(f'[eb-fights] {len(ids_to_fetch):,} new fights to scrape ({len(all_ids)-len(ids_to_fetch):,} already in DB)')

    global _json_dir
    if json_dir:
        os.makedirs(json_dir, exist_ok=True)
        _json_dir = json_dir
        print(f'[eb-fights] writing JSON files to {json_dir}')

    new = skipped = errors = 0
    total = len(ids_to_fetch)

    with ThreadPoolExecutor(max_workers=min(workers, 10)) as ex:  # cap fight fetchers at 10
        futures = {ex.submit(_scrape_and_save, fid): fid for fid in ids_to_fetch}
        for i, fut in enumerate(as_completed(futures), 1):
            result, fight = fut.result()
            if result == 'new' and fight:
                new += 1
                won = 'ATK WIN' if fight['attackers_won'] else 'DEF WIN'
                print(f'[{i}/{total}] +{fight["id"]} | {fight["attacker_town"]} vs {fight["defender_town"]} | {fight["world"]} {fight["fight_date"]} | {fight["total_players"]}p | {won}')
            elif result == 'skip':
                skipped += 1
            else:
                errors += 1
                print(f'[{i}/{total}] ERR {futures[fut]}')
            if i % 500 == 0:
                print(f'[eb-fights] progress: {new} saved  {skipped} skipped  {errors} errors')

    print(f'[eb-fights] complete — {new} new  {skipped} skipped  {errors} errors')

# ── Startup (import-time) ─────────────────────────────────────────────────────
ensure_tables()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--full', action='store_true',
                        help='Full historical scrape via fightlist pagination')
    parser.add_argument('--stop-on-existing', action='store_true',
                        help='Stop full scrape when hitting already-stored fights')
    parser.add_argument('--json', metavar='DIR', default=None,
                        help='Also write each fight as FIGHTID.json to DIR')
    parser.add_argument('--max-pages', type=int, default=4000,
                        help='Max fightlist pages to enumerate (default 4000)')
    args = parser.parse_args()

    JSON_DIR_ARG = os.path.abspath(args.json) if args.json else None

    if args.full:
        run_full(stop_on_existing=args.stop_on_existing,
                 max_pages=args.max_pages,
                 json_dir=JSON_DIR_ARG)
    else:
        n = run_incremental()
        print(f'[eb-fights] incremental done: {n} new fights')
