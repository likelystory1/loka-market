import os
import re
import time
import json
from pathlib import Path
import statistics
import sqlite3
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, send_from_directory, request, abort
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ── paths ─────────────────────────────────────────────────────────────────────
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DB_PATH         = os.path.join(BASE_DIR, "market_history.db")
TERRITORIES_DB  = os.path.join(BASE_DIR, "territories.db")
FRONTEND        = os.path.join(BASE_DIR, "..", "frontend")

# ── app ───────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=None)

# Restrict CORS to GET/HEAD/OPTIONS only — no write methods from any origin
CORS(app, resources={r"/api/*": {
    "origins": os.environ.get("ALLOWED_ORIGIN", "*"),
    "methods": ["GET", "HEAD", "OPTIONS"],
}})

# Rate limiting — 60 requests/minute per IP on all API endpoints
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["60 per minute"],
    storage_uri="memory://",
)

# ── cache (10-min TTL) ────────────────────────────────────────────────────────
_CACHE:    dict = {}
_CACHE_TS: dict = {}
CACHE_TTL = 600

def _cache_get(key):
    if key in _CACHE and time.time() - _CACHE_TS[key] < CACHE_TTL:
        return _CACHE[key]
    return None

def _cache_set(key, val):
    _CACHE[key]    = val
    _CACHE_TS[key] = time.time()

# ── db ────────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_indexes():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_item       ON trades(item)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts         ON trades(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_buyer_uuid ON trades(buyer_uuid)")
    conn.commit()
    conn.close()

def ensure_battle_tables():
    db = sqlite3.connect(TERRITORIES_DB)
    c = db.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS territory_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_ts INTEGER NOT NULL,
            territory_num INTEGER NOT NULL,
            world TEXT NOT NULL,
            area_name TEXT,
            mutator TEXT,
            town_id TEXT,
            last_battle INTEGER,
            inhibitor_town TEXT,
            raw_json TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_snap_unique ON territory_snapshots(poll_ts, territory_num, world);
        CREATE INDEX IF NOT EXISTS idx_snap_poll_ts ON territory_snapshots(poll_ts);

        CREATE TABLE IF NOT EXISTS battle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_ts INTEGER NOT NULL,
            territory_num INTEGER NOT NULL,
            world TEXT NOT NULL,
            area_name TEXT,
            mutator TEXT,
            old_town_id TEXT,
            new_town_id TEXT,
            old_alliance_id TEXT,
            new_alliance_id TEXT,
            old_alliance_name TEXT,
            new_alliance_name TEXT,
            old_town_name TEXT,
            new_town_name TEXT,
            strength_delta INTEGER,
            territory_won_by TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_battle_ts ON battle_events(detected_ts);
        CREATE INDEX IF NOT EXISTS idx_battle_mutator ON battle_events(mutator);

        CREATE TABLE IF NOT EXISTS alliance_strength_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_ts INTEGER NOT NULL,
            alliance_id TEXT NOT NULL,
            alliance_name TEXT,
            strength INTEGER,
            bb_strength INTEGER
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_allstr_poll_aid ON alliance_strength_snapshots(poll_ts, alliance_id);
    ''')
    db.commit()
    db.close()

# ── block write methods on all API routes ─────────────────────────────────────
@app.before_request
def enforce_read_only():
    if request.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        abort(405)

# ── security headers ──────────────────────────────────────────────────────────
@app.after_request
def add_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"]        = "SAMEORIGIN"
    resp.headers["X-XSS-Protection"]       = "1; mode=block"
    resp.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' https://mc-heads.net https://raw.githubusercontent.com data:; "
        "connect-src 'self'; "
        "frame-src https://lokamc.com; "
        "frame-ancestors 'none'"
    )
    return resp

# ── UUID → username resolution ────────────────────────────────────────────────
_UUID_CACHE: dict    = {}   # uuid/objectid -> resolved username (or '' if failed)
_UUID_LOCK           = threading.Lock()
_STD_UUID_RE         = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
_NO_DASH_UUID_RE     = re.compile(r'^[0-9a-f]{32}$', re.I)
_MONGO_OID_RE        = re.compile(r'^[0-9a-f]{24}$', re.I)

def _normalise_uuid(uid: str) -> str:
    """Return UUID in 8-4-4-4-12 dashed format, or the original string if not parseable."""
    clean = uid.replace('-', '')
    if len(clean) == 32 and _NO_DASH_UUID_RE.match(clean):
        return f"{clean[0:8]}-{clean[8:12]}-{clean[12:16]}-{clean[16:20]}-{clean[20:32]}"
    return uid

def _is_minecraft_uuid(uid: str) -> bool:
    """True for both dashed and undashed 32-hex UUIDs."""
    return bool(uid and (_STD_UUID_RE.match(uid) or _NO_DASH_UUID_RE.match(uid.replace('-', ''))))

def _fetch_loka_objectid(oid: str) -> tuple[str, str, str]:
    """Resolve a Loka MongoDB ObjectID via api.lokamc.com.
    Returns (oid, username, minecraft_uuid)."""
    try:
        req = urllib.request.Request(
            f"https://api.lokamc.com/players/{oid}",
            headers={"User-Agent": "LokaMarket/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
            return oid, data.get("name", ""), data.get("uuid", "")
    except Exception:
        return oid, "", ""

# oid -> (name, mc_uuid)
_OID_CACHE: dict[str, tuple[str, str]] = {}

def resolve_loka_objectids(oids: list[str]) -> dict[str, tuple[str, str]]:
    """Resolve Loka MongoDB ObjectIDs to (name, mc_uuid). Caches results."""
    result: dict[str, tuple[str, str]] = {}
    to_fetch: list[str] = []

    for oid in oids:
        with _UUID_LOCK:
            if oid in _OID_CACHE:
                result[oid] = _OID_CACHE[oid]
                continue
        to_fetch.append(oid)

    if to_fetch:
        workers = min(10, len(to_fetch))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_fetch_loka_objectid, oid): oid for oid in to_fetch}
            for fut in as_completed(futures):
                oid, name, mc_uuid = fut.result()
                entry = (name, mc_uuid)
                with _UUID_LOCK:
                    _OID_CACHE[oid] = entry
                result[oid] = entry

    return result

def _lookup_loka_db(uuids: list[str]) -> dict[str, str]:
    """Bulk-resolve UUIDs from loka.db (current Loka player names). Returns uuid→name."""
    try:
        from eldritch_scraper import LOKA_PATH
        import sqlite3 as _sq
        # Normalise to dashed format for lookup (loka.db stores without dashes; handle both)
        normalised = {_normalise_uuid(u): u for u in uuids}
        stripped   = {u.replace('-', ''): u for u in uuids}
        placeholders = ','.join('?' * len(uuids))
        db   = _sq.connect(LOKA_PATH)
        # loka_players.uuid may be stored without dashes
        rows = db.execute(
            f'SELECT uuid, name FROM loka_players WHERE uuid IN ({placeholders}) AND name != ""',
            list(stripped.keys())
        ).fetchall()
        db.close()
        out = {}
        for uuid_raw, name in rows:
            orig = stripped.get(uuid_raw) or normalised.get(uuid_raw)
            if orig:
                out[orig] = name
        return out
    except Exception:
        return {}

def _fetch_one_uuid(uid: str) -> tuple[str, str]:
    """Returns (uuid, username) via Mojang sessionserver. Username is '' on failure."""
    clean = uid.replace('-', '')
    url   = f"https://sessionserver.mojang.com/session/minecraft/profile/{clean}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LokaMarket/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return uid, data.get("name", "")
    except Exception:
        return uid, ""

def _db_write_names(pairs: list[tuple[str, str]]):
    """Persist resolved names to market DB so they survive restarts."""
    try:
        conn = sqlite3.connect(DB_PATH)
        for uid, name in pairs:
            conn.execute(
                "UPDATE trades SET buyer_name=?  WHERE buyer_uuid=?  AND buyer_name IS NULL",
                (name, uid))
            conn.execute(
                "UPDATE trades SET seller_name=? WHERE seller_uuid=? AND seller_name IS NULL",
                (name, uid))
        conn.commit()
        conn.close()
    except Exception:
        pass

def resolve_uuids(uuids: list[str]) -> dict[str, str]:
    """
    Resolve UUIDs to current display names.
    Priority: in-memory cache → loka.db (86k players, always current) → Mojang API.
    Handles both dashed and undashed UUID formats.
    """
    result: dict[str, str] = {}
    to_resolve: list[str]  = []

    for uid in uuids:
        if not uid:
            result[uid] = "Unknown"
            continue
        if not _is_minecraft_uuid(uid):
            result[uid] = uid[:8] + "…"
            continue
        with _UUID_LOCK:
            if uid in _UUID_CACHE:
                result[uid] = _UUID_CACHE[uid] or (uid[:8] + "…")
                continue
        to_resolve.append(uid)

    if not to_resolve:
        return result

    # 1) Bulk-resolve from loka.db (fast, covers all 86k Loka players with current names)
    loka_hits = _lookup_loka_db(to_resolve)
    still_missing = []
    for uid in to_resolve:
        if uid in loka_hits:
            name = loka_hits[uid]
            with _UUID_LOCK:
                _UUID_CACHE[uid] = name
            result[uid] = name
        else:
            still_missing.append(uid)

    # 2) Fall back to Mojang API for any not found in loka.db
    if still_missing:
        workers = min(10, len(still_missing))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_fetch_one_uuid, u): u for u in still_missing}
            for fut in as_completed(futures, timeout=8):
                try:
                    uid, name = fut.result()
                    with _UUID_LOCK:
                        _UUID_CACHE[uid] = name
                    result[uid] = name if name else (uid[:8] + "…")
                except Exception:
                    uid = futures[fut]
                    result[uid] = uid[:8] + "…"

        # Persist newly resolved names to market DB in background
        pairs = [(u, _UUID_CACHE.get(u, "")) for u in still_missing if _UUID_CACHE.get(u)]
        if pairs:
            threading.Thread(target=_db_write_names, args=(pairs,), daemon=True).start()

    return result

# ── golden zone (IQR-based) ───────────────────────────────────────────────────
def _compute_stats(prices: list) -> dict | None:
    if not prices:
        return None
    n   = len(prices)
    atl = round(min(prices), 2)
    ath = round(max(prices), 2)

    if n < 4:
        fmv = round(statistics.median(prices), 2)
        return {"fmv": fmv, "zone_low": fmv, "zone_high": fmv,
                "fence_lo": fmv, "fence_hi": fmv,
                "atl": atl, "ath": ath, "outlier_count": 0, "sample": n}

    sp  = sorted(prices)
    q1  = sp[n  // 4]
    q3  = sp[(3 * n) // 4]
    iqr = q3 - q1

    fence_lo = q1 - 1.5 * iqr
    fence_hi = q3 + 1.5 * iqr

    clean    = [p for p in sp if fence_lo <= p <= fence_hi]
    outliers = [p for p in sp if p < fence_lo or p > fence_hi]
    if not clean:
        clean = sp

    return {
        "fmv":           round(statistics.median(clean), 2),
        "zone_low":      round(q1, 2),
        "zone_high":     round(q3, 2),
        "fence_lo":      round(max(0, fence_lo), 2),
        "fence_hi":      round(fence_hi, 2),
        "atl":           atl,
        "ath":           ath,
        "outlier_count": len(outliers),
        "sample":        len(clean),
    }

# ── item name normalisation ────────────────────────────────────────────────────
# Any item whose name ends with SHULKER_BOX (e.g. WHITE_SHULKER_BOX) is merged
# into the canonical SHULKER_BOX entry. Add more rules here as needed.
def _normalize_item(name: str) -> str:
    if name.endswith('SHULKER_BOX'):
        return 'SHULKER_BOX'
    return name

def _item_variants(c, canonical: str):
    """Return every raw item name in the DB that normalises to `canonical`."""
    if canonical == 'SHULKER_BOX':
        rows = c.execute(
            "SELECT DISTINCT item FROM trades WHERE item LIKE '%SHULKER_BOX'"
        ).fetchall()
        return [r[0] for r in rows]
    return [canonical]

def _fetch_prices(c, raw_names, limit=200):
    ph = ','.join('?' * len(raw_names))
    return [r[0] for r in c.execute(
        f"SELECT price FROM trades WHERE item IN ({ph}) ORDER BY ts DESC, rowid DESC LIMIT {limit}",
        raw_names
    ).fetchall()]

# ── build items list ──────────────────────────────────────────────────────────
def _build_items():
    conn = get_db()
    c    = conn.cursor()
    # Group raw DB items by their canonical (normalised) name
    raw_rows = c.execute(
        "SELECT item, COUNT(*) AS vol, MAX(ts) AS last_ts FROM trades GROUP BY item ORDER BY vol DESC"
    ).fetchall()

    from collections import defaultdict
    groups = defaultdict(lambda: {"vol": 0, "last_ts": 0, "raw_names": []})
    for row in raw_rows:
        canon = _normalize_item(row["item"])
        groups[canon]["vol"]      += row["vol"]
        groups[canon]["last_ts"]   = max(groups[canon]["last_ts"], row["last_ts"] or 0)
        groups[canon]["raw_names"].append(row["item"])

    results = []
    for canon, g in groups.items():
        prices = _fetch_prices(c, g["raw_names"])
        stats  = _compute_stats(prices)
        if stats is None:
            continue
        results.append({"item": canon, "volume": g["vol"],
                        "last_price": prices[0] if prices else None,
                        "last_ts": g["last_ts"], **stats})

    results.sort(key=lambda x: x["volume"], reverse=True)
    conn.close()
    return results

def _warmup():
    time.sleep(0.5)
    try:
        data = _build_items()
        _cache_set("items", data)
        print(f"[warmup] {len(data)} items cached")
    except Exception as e:
        print(f"[warmup] error: {e}")

# ── frontend ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(FRONTEND, "index.html")

@app.route("/item")
def item_page():
    return send_from_directory(FRONTEND, "item.html")

@app.route("/alliances")
def alliances_page():
    return send_from_directory(FRONTEND, "alliances.html")

@app.route("/towns")
def towns_page():
    return send_from_directory(FRONTEND, "towns.html")

@app.route("/market")
def market_page_route():
    return send_from_directory(FRONTEND, "market.html")

@app.route("/fights")
def fights_page():
    return send_from_directory(FRONTEND, "fights.html")

@app.route("/founders")
def founders_page():
    return send_from_directory(FRONTEND, "founders.html")

@app.route("/territories")
def territories_page():
    return send_from_directory(FRONTEND, "territories.html")

@app.route("/players")
def players_page():
    return send_from_directory(FRONTEND, "players.html")

FIGHTLOGS_DIR = os.path.join(BASE_DIR, 'fightlogs')
PARSED_DIR    = os.path.join(FIGHTLOGS_DIR, 'parsed')

os.makedirs(PARSED_DIR, exist_ok=True)

# ── active fight log poller ───────────────────────────────────────────────────
_active_fight_logs: set = set()   # stem names currently live from the API
_active_fight_lock       = threading.Lock()

def _poll_active_fights() -> int:
    """Download + parse any active conquest fight logs. Returns count of active fights."""
    try:
        req = urllib.request.Request(
            'https://api.lokamc.com/territories?size=600',
            headers={'User-Agent': 'LokaUtils/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        all_territories = data.get('_embedded', {}).get('territories', [])
        # Filter to only those with an active battleZone and a log filename
        territories = [t for t in all_territories if t.get('battleZone') and t['battleZone'].get('log')]
    except Exception as e:
        print(f'[fights-poll] fetch failed: {e}')
        return 0

    seen = set()
    for t in territories:
        bz = t.get('battleZone') or t.get('tg', {}).get('battleZone')
        if not bz:
            continue
        log_name = bz.get('log', '')
        if not log_name:
            continue

        txt_path    = os.path.join(FIGHTLOGS_DIR, log_name + '.txt')
        parsed_path = os.path.join(PARSED_DIR, log_name + '.json')

        seen.add(log_name)
        active = len(seen)
        try:
            url = f'https://api.lokamc.com/conquestlogs/{log_name}.txt'
            req = urllib.request.Request(url, headers={'User-Agent': 'LokaUtils/1.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read()
            with open(txt_path, 'wb') as f:
                f.write(content)
        except Exception as e:
            print(f'[fights-poll] download {log_name}: {e}')
            continue

        try:
            from fights_parser import parse_fight_log
            result = parse_fight_log(txt_path)
            tmp = parsed_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(result, f, separators=(',', ':'))
            os.replace(tmp, parsed_path)
            total = len(result.get('attackers', [])) + len(result.get('defenders', []))
            print(f'[fights-poll] parsed {log_name} ({total} players)')
            # Queue fight participants for priority EB re-scrape
            try:
                names = [p['name'] for p in result.get('attackers', []) + result.get('defenders', [])]
                queue_by_names(names, priority=1)
            except Exception:
                pass
        except Exception as e:
            print(f'[fights-poll] parse {log_name}: {e}')

    with _active_fight_lock:
        _active_fight_logs.clear()
        _active_fight_logs.update(seen)

    return len(seen)

def _fight_poll_loop():
    from eb_fight_scraper import run_incremental as _eb_incremental
    eb_check_interval = 300   # check EB for new fights every 5 min
    last_eb_check     = 0.0
    while True:
        active = _poll_active_fights()
        now = time.time()
        if now - last_eb_check >= eb_check_interval:
            try:
                _eb_incremental()
            except Exception as e:
                print(f'[eb-fights] incremental error: {e}')
            last_eb_check = time.time()
        time.sleep(10 if active else 60)

_fights_cache      = None
_fights_cache_ts   = 0.0
_fights_rebuilding = False
_FIGHTS_CACHE_TTL  = 3600  # seconds
_FIGHTS_INDEX_PATH = os.path.join(FIGHTLOGS_DIR, 'fights_index.json')

def _load_fights_index():
    """Return the in-memory index, loading from disk if needed. Never blocks on file scan."""
    global _fights_cache, _fights_cache_ts, _fights_rebuilding
    now = time.time()
    if _fights_cache is not None and now - _fights_cache_ts < _FIGHTS_CACHE_TTL:
        return _fights_cache
    # Try loading from pre-built index file on disk (fast path)
    if os.path.exists(_FIGHTS_INDEX_PATH):
        try:
            with open(_FIGHTS_INDEX_PATH, encoding='utf-8') as f:
                data = json.load(f)
            _fights_cache    = data
            _fights_cache_ts = now
            # Kick off a background rebuild so index stays fresh
            if not _fights_rebuilding:
                threading.Thread(target=_rebuild_fights_index, daemon=True).start()
            return _fights_cache
        except Exception as e:
            print(f'[fights] index load error: {e}')
    # No index file yet — rebuild now (only on very first boot)
    return _rebuild_fights_index()

def _rebuild_fights_index():
    """Scan all parsed JSONs, build index, save to disk, update in-memory cache."""
    global _fights_cache, _fights_cache_ts, _fights_rebuilding
    if _fights_rebuilding:
        return _fights_cache or []
    _fights_rebuilding = True
    print('[fights] rebuilding index…')
    results = []
    if not os.path.isdir(PARSED_DIR):
        _fights_rebuilding = False
        return results

    # Load date/time/timestamp from eb_fights.db keyed by fight ID
    eb_db_path = os.path.join(BASE_DIR, 'eb_fights.db')
    eb_dates = {}
    if os.path.exists(eb_db_path):
        try:
            import sqlite3 as _sq
            _db = _sq.connect(eb_db_path)
            eb_dates = {r[0]: (r[1], r[2], r[3], r[4], r[5], r[6]) for r in
                        _db.execute('SELECT id, fight_date, fight_time, timestamp, world, attackers_won, attacker_town FROM eb_fights').fetchall()}
            _db.close()
        except Exception as e:
            print(f'[fights] eb_dates load error: {e}')

    for path in Path(PARSED_DIR).glob('*.json'):
        try:
            with open(path, encoding='utf-8') as f:
                d = json.load(f)
            total = len(d.get('attackers', [])) + len(d.get('defenders', []))
            with _active_fight_lock:
                is_live = path.stem in _active_fight_logs
            eb = eb_dates.get(path.stem, ())
            att_town = d.get('attacker_town', '')
            def_town = d.get('defender_town', '')
            # Resolve winner to a town name
            parsed_winner = d.get('winner', '')
            if parsed_winner in ('attackers', 'defenders'):
                parsed_winner = att_town if parsed_winner == 'attackers' else def_town
            if eb and not parsed_winner:
                eb_att_won, eb_att_town = eb[4], eb[5]
                if eb_att_won is not None:
                    parsed_winner = (eb_att_town or att_town) if eb_att_won else def_town
            results.append({
                'filename':       path.stem,
                'location':       d.get('location', ''),
                'winner':         parsed_winner,
                'duration':       d.get('duration', ''),
                'attacker_town':  att_town,
                'defender_town':  def_town,
                'world':          (eb[3] if eb else None) or d.get('world', ''),
                'date_display':   eb[0] if eb else d.get('date_display', ''),
                'time_display':   eb[1] if eb else d.get('time_display', ''),
                'attacker_count': len(d.get('attackers', [])),
                'defender_count': len(d.get('defenders', [])),
                'attacker_kills': d.get('attacker_kills', 0),
                'defender_kills': d.get('defender_kills', 0),
                'total_players':  total,
                'is_live':        is_live,
                '_ts':            eb[2] if eb else path.stat().st_mtime,
            })
        except Exception as e:
            print(f'[fights] error reading {path.name}: {e}')
    results.sort(key=lambda r: (not r['is_live'], -(r['_ts'] or 0)))
    for r in results:
        del r['_ts']
    # Save to disk so next startup is instant
    try:
        tmp = _FIGHTS_INDEX_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(results, f, separators=(',', ':'))
        os.replace(tmp, _FIGHTS_INDEX_PATH)
        print(f'[fights] index saved — {len(results):,} fights')
    except Exception as e:
        print(f'[fights] index save error: {e}')
    _fights_cache      = results
    _fights_cache_ts   = time.time()
    _fights_rebuilding = False
    return results

@app.route("/api/fights-index")
def api_fights_index():
    """Serve the pre-built fights index file directly."""
    if not os.path.exists(_FIGHTS_INDEX_PATH):
        return jsonify([])
    return app.response_class(
        open(_FIGHTS_INDEX_PATH, 'rb').read(),
        content_type='application/json'
    )

@app.route("/api/fights")
def api_fights():
    """List all pre-parsed fight JSON files with pagination."""
    limit  = min(int(request.args.get('limit',  50)), 500)
    offset = int(request.args.get('offset', 0))
    town   = request.args.get('town', '').strip().lower()
    all_fights = _load_fights_index()
    if town:
        all_fights = [f for f in all_fights
                      if town in f['attacker_town'].lower() or town in f['defender_town'].lower()]
    page = all_fights[offset:offset + limit]
    return jsonify({'fights': page, 'total': len(all_fights)})

@app.route("/api/fights/<path:filename>")
def api_fight_detail(filename):
    """Serve a pre-parsed fight JSON, enriched with date from eb_fights.db."""
    if '/' in filename or '..' in filename:
        abort(400)
    stem = filename.removesuffix('.txt').removesuffix('.json')
    jp = os.path.join(PARSED_DIR, stem + '.json')
    if not os.path.exists(jp):
        abort(404)
    with open(jp, encoding='utf-8') as f:
        d = json.load(f)
    # Enrich with date/location/winner from eb_fights.db
    eb_db_path = os.path.join(BASE_DIR, 'eb_fights.db')
    if os.path.exists(eb_db_path):
        try:
            import sqlite3 as _sq
            _db = _sq.connect(eb_db_path)
            row = _db.execute(
                'SELECT fight_date, fight_time, location, world, attackers_won, attacker_town FROM eb_fights WHERE id=?', (stem,)
            ).fetchone()
            _db.close()
            if row:
                if row[0] and not d.get('date_display'): d['date_display'] = row[0]
                if row[1] and not d.get('time_display'): d['time_display'] = row[1]
                if row[2] and not d.get('location'):     d['location'] = row[2]
                if row[3]: d['world'] = row[3]  # DB world always takes priority
                # Resolve winner to a town name
                winner = d.get('winner', '')
                if winner in ('attackers', 'defenders'):
                    winner = d.get('attacker_town', '') if winner == 'attackers' else d.get('defender_town', '')
                if not winner and row[4] is not None:
                    winner = (row[5] or d.get('attacker_town', '')) if row[4] else d.get('defender_town', '')
                d['winner'] = winner
        except Exception:
            pass
    # Sort players by kills desc
    for side in ('attackers', 'defenders'):
        if d.get(side):
            d[side].sort(key=lambda p: p.get('kills', 0), reverse=True)
    return jsonify(d)


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(FRONTEND, filename)

# ── api: stats ────────────────────────────────────────────────────────────────
@app.route("/api/stats")
def market_stats():
    cached = _cache_get("stats")
    if cached:
        return jsonify(cached)
    conn = get_db()
    row  = conn.execute("""
        SELECT COUNT(*)             AS total_trades,
               COUNT(DISTINCT item) AS unique_items,
               COUNT(DISTINCT buyer_uuid) + COUNT(DISTINCT seller_uuid) AS active_traders
        FROM trades
    """).fetchone()
    conn.close()
    data = {"total_trades": row["total_trades"],
            "unique_items": row["unique_items"],
            "active_traders": row["active_traders"]}
    _cache_set("stats", data)
    return jsonify(data)

# ── api: items list ───────────────────────────────────────────────────────────
@app.route("/api/items")
def items():
    cached = _cache_get("items")
    if cached:
        return jsonify(cached)
    data = _build_items()
    _cache_set("items", data)
    return jsonify(data)

# ── api: item detail ──────────────────────────────────────────────────────────
@app.route("/api/item/<path:item_name>")
def item_detail(item_name):
    conn = get_db()
    c    = conn.cursor()
    # Resolve canonical name and all raw DB variants
    canon    = _normalize_item(item_name)
    variants = _item_variants(c, canon)
    ph       = ','.join('?' * len(variants))
    trades = c.execute(f"""
        SELECT price, buyer_uuid, seller_uuid, ts
        FROM trades WHERE item IN ({ph})
        ORDER BY ts DESC, rowid DESC LIMIT 100
    """, variants).fetchall()
    total  = c.execute(f"SELECT COUNT(*) FROM trades WHERE item IN ({ph})", variants).fetchone()[0]
    prows  = c.execute(
        f"SELECT price FROM trades WHERE item IN ({ph}) ORDER BY ts DESC, rowid DESC LIMIT 200",
        variants
    ).fetchall()
    conn.close()

    prices = [p[0] for p in prows]
    stats  = _compute_stats(prices)
    if stats is None:
        abort(404)

    # Resolve UUIDs for all trades in parallel
    all_uuids = []
    for t in trades:
        all_uuids.extend([t["buyer_uuid"], t["seller_uuid"]])
    names = resolve_uuids(all_uuids)

    return jsonify({
        "item": item_name, "volume": total,
        "last_price": prices[0] if prices else None,
        **stats,
        "trades": [
            {"price":       t["price"],
             "buyer":       names.get(t["buyer_uuid"],  t["buyer_uuid"][:8] + "…" if t["buyer_uuid"] else "—"),
             "seller":      names.get(t["seller_uuid"], t["seller_uuid"][:8] + "…" if t["seller_uuid"] else "—"),
             "buyer_uuid":  t["buyer_uuid"],
             "seller_uuid": t["seller_uuid"],
             "ts":          t["ts"]}
            for t in trades
        ],
    })

# ── api: recent trades (global, all items) ────────────────────────────────────
@app.route("/api/recent")
def recent_trades():
    conn = get_db()
    rows = conn.execute("""
        SELECT item, price, buyer_uuid, seller_uuid, ts
        FROM trades
        ORDER BY rowid DESC
        LIMIT 100
    """).fetchall()
    conn.close()

    # Resolve all UUIDs in parallel
    all_uuids = []
    for r in rows:
        all_uuids.extend([r["buyer_uuid"], r["seller_uuid"]])
    names = resolve_uuids(all_uuids)

    # Attach zone info from cached items list
    items_data = _cache_get("items") or []
    item_stats = {i["item"]: i for i in items_data}

    result = []
    for r in rows:
        s = item_stats.get(r["item"], {})
        result.append({
            "item":       r["item"],
            "price":      r["price"],
            "buyer":      names.get(r["buyer_uuid"],  r["buyer_uuid"][:8] + "…"  if r["buyer_uuid"]  else "—"),
            "seller":     names.get(r["seller_uuid"], r["seller_uuid"][:8] + "…" if r["seller_uuid"] else "—"),
            "buyer_uuid": r["buyer_uuid"],
            "seller_uuid":r["seller_uuid"],
            "ts":         r["ts"],
            "zone_low":   s.get("zone_low"),
            "zone_high":  s.get("zone_high"),
            "fence_lo":   s.get("fence_lo"),
            "fence_hi":   s.get("fence_hi"),
            "fmv":        s.get("fmv"),
        })
    return jsonify(result)

# ── api: search ───────────────────────────────────────────────────────────────
@app.route("/api/search")
def search():
    q = request.args.get("q", "").strip()
    if not q or len(q) > 64:
        return jsonify([])
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT item FROM trades WHERE item LIKE ? ORDER BY item LIMIT 20",
        (f"%{q}%",)
    ).fetchall()
    conn.close()
    return jsonify([r["item"] for r in rows])

# ── api: top spenders leaderboard ─────────────────────────────────────────────
@app.route("/api/players")
def top_players():
    cached = _cache_get("players")
    if cached:
        return jsonify(cached)

    conn = get_db()
    c    = conn.cursor()

    # Top 50 buyers by total shards spent
    buyers = c.execute("""
        SELECT buyer_uuid,
               SUM(price)  AS total_spent,
               COUNT(*)    AS trade_count
        FROM   trades
        WHERE  buyer_uuid IS NOT NULL
        GROUP  BY buyer_uuid
        ORDER  BY total_spent DESC
        LIMIT  50
    """).fetchall()

    if not buyers:
        conn.close()
        return jsonify([])

    uuids = [b["buyer_uuid"] for b in buyers]

    # Favorite item per buyer — one batch query, ordered so first row per
    # buyer is their most-purchased item
    ph   = ",".join("?" * len(uuids))
    favs = c.execute(f"""
        SELECT buyer_uuid, item, COUNT(*) AS cnt
        FROM   trades
        WHERE  buyer_uuid IN ({ph})
        GROUP  BY buyer_uuid, item
        ORDER  BY buyer_uuid, cnt DESC
    """, uuids).fetchall()

    conn.close()

    # Keep only the top item per buyer (first occurrence after ORDER BY)
    fav_item: dict[str, str] = {}
    for row in favs:
        if row["buyer_uuid"] not in fav_item:
            fav_item[row["buyer_uuid"]] = row["item"]

    # Resolve all UUIDs to usernames in parallel
    names = resolve_uuids(uuids)

    results = [
        {
            "rank":        i + 1,
            "uuid":        uid,
            "name":        names.get(uid, uid[:8] + "…"),
            "total_spent": round(b["total_spent"], 2),
            "trade_count": b["trade_count"],
            "fav_item":    fav_item.get(uid),
        }
        for i, (b, uid) in enumerate(zip(buyers, uuids))
    ]

    _cache_set("players", results)
    return jsonify(results)

# ── api: top sellers leaderboard ──────────────────────────────────────────────
@app.route("/api/sellers")
def top_sellers():
    cached = _cache_get("sellers")
    if cached:
        return jsonify(cached)

    conn = get_db()
    c    = conn.cursor()

    sellers = c.execute("""
        SELECT seller_uuid,
               SUM(price)  AS total_earned,
               COUNT(*)    AS trade_count
        FROM   trades
        WHERE  seller_uuid IS NOT NULL
        GROUP  BY seller_uuid
        ORDER  BY total_earned DESC
        LIMIT  50
    """).fetchall()

    if not sellers:
        conn.close()
        return jsonify([])

    uuids = [s["seller_uuid"] for s in sellers]

    ph   = ",".join("?" * len(uuids))
    favs = c.execute(f"""
        SELECT seller_uuid, item, COUNT(*) AS cnt
        FROM   trades
        WHERE  seller_uuid IN ({ph})
        GROUP  BY seller_uuid, item
        ORDER  BY seller_uuid, cnt DESC
    """, uuids).fetchall()

    conn.close()

    fav_item: dict[str, str] = {}
    for row in favs:
        if row["seller_uuid"] not in fav_item:
            fav_item[row["seller_uuid"]] = row["item"]

    # seller_uuid is a Loka MongoDB ObjectID (24-char hex), not a Minecraft UUID
    oid_ids = [u for u in uuids if _MONGO_OID_RE.match(u)]
    mc_ids  = [u for u in uuids if not _MONGO_OID_RE.match(u)]

    oid_resolved: dict[str, tuple[str, str]] = resolve_loka_objectids(oid_ids) if oid_ids else {}

    # After resolving OIDs → MC UUIDs, look up current names via loka.db/Mojang
    oid_mc_uuids = [mc_uuid for _, mc_uuid in oid_resolved.values() if mc_uuid]
    mc_names: dict[str, str] = resolve_uuids(mc_ids + oid_mc_uuids) if (mc_ids or oid_mc_uuids) else {}

    def _seller_name(uid: str) -> str:
        if uid in oid_resolved:
            _, mc_uuid = oid_resolved[uid]
            if mc_uuid:
                name = mc_names.get(mc_uuid, '')
                return name if name else uid[:8] + "…"
            loka_name, _ = oid_resolved[uid]
            return loka_name if loka_name else uid[:8] + "…"
        return mc_names.get(uid, uid[:8] + "…")

    def _seller_mc_uuid(uid: str) -> str:
        if uid in oid_resolved:
            _, mc_uuid = oid_resolved[uid]
            return mc_uuid if mc_uuid else uid
        return uid

    results = [
        {
            "rank":         i + 1,
            "uuid":         _seller_mc_uuid(uid),
            "name":         _seller_name(uid),
            "total_earned": round(s["total_earned"], 2),
            "trade_count":  s["trade_count"],
            "fav_item":     fav_item.get(uid),
        }
        for i, (s, uid) in enumerate(zip(sellers, uuids))
    ]

    _cache_set("sellers", results)
    return jsonify(results)

# ── world / area name display maps ────────────────────────────────────────────
WORLD_DISPLAY = {
    'north':  'Kalros',
    'south':  'Garama',
    'west':   'Ascalon',
    'lilboi': 'Rivina',
    'bigboi': 'Balak',
}

def _display_world(raw):
    return WORLD_DISPLAY.get((raw or '').lower(), (raw or '').capitalize())

def _display_area(raw, world=''):
    """Human-readable area name."""
    if not raw:
        return ''
    import re
    s = raw.strip()
    # Rivina tiles: lbisle<N> or lb<anything>
    m = re.match(r'^lb(?:isle)?(\d+)$', s, re.I)
    if m:
        return f'Rivina Tile {m.group(1)}'
    # Balak tiles: bbisle<N> or bb<anything>
    m = re.match(r'^bb(?:isle)?(\d+)$', s, re.I)
    if m:
        return f'Balak Tile {m.group(1)}'
    # Strip lb/bb prefix on other names (e.g. lbcoast -> Coast)
    s = re.sub(r'^lb', '', s, flags=re.I)
    s = re.sub(r'^bb', '', s, flags=re.I)
    # Replace underscores, title-case
    return s.replace('_', ' ').title()

# ── api: battles ──────────────────────────────────────────────────────────────
def _build_town_lookup():
    """Return town_id -> {town_name, alliance_name} using towns + alliances cache."""
    town_name_map   = {}   # town_id -> town_name
    alliance_map    = {}   # town_id -> alliance_name
    try:
        with _loka_lock:
            # Build town_id -> name from towns cache
            if os.path.exists(LOKA_CACHE_TOWNS):
                with open(LOKA_CACHE_TOWNS) as f:
                    tdata = json.load(f)
                for t in tdata.get('_embedded', {}).get('towns', []):
                    tid = str(t.get('id', ''))
                    if tid:
                        town_name_map[tid] = t.get('name', '')
            # Build town_id -> alliance from alliances townIds
            if os.path.exists(LOKA_CACHE_ALLIANCES):
                with open(LOKA_CACHE_ALLIANCES) as f:
                    adata = json.load(f)
                for a in adata.get('_embedded', {}).get('alliances', []):
                    aname = a.get('name', '')
                    for tid in (a.get('townIds') or []):
                        alliance_map[str(tid)] = aname
    except Exception:
        pass
    # Merge
    all_ids = set(town_name_map) | set(alliance_map)
    return {tid: {'town_name': town_name_map.get(tid, ''), 'alliance_name': alliance_map.get(tid, '')} for tid in all_ids}

@app.route("/api/battles")
def api_battles():
    try:
        db = sqlite3.connect(TERRITORIES_DB)
        db.row_factory = sqlite3.Row
        c = db.cursor()

        def fetch_battles(query, params=()):
            try:
                return [dict(r) for r in c.execute(query, params).fetchall()]
            except Exception:
                return []

        now = int(time.time())
        day_ago = now - 86400

        # Diff-based battle events (ownership changes detected between polls)
        recent     = fetch_battles('SELECT * FROM battle_events WHERE detected_ts > ? ORDER BY detected_ts DESC LIMIT 100', (day_ago,))
        top_alliance = fetch_battles('SELECT * FROM battle_events ORDER BY ABS(COALESCE(strength_delta,0)) DESC LIMIT 50')
        rivina     = fetch_battles("SELECT * FROM battle_events WHERE mutator='rivina' OR world='rivina' ORDER BY detected_ts DESC LIMIT 50")
        no_transfer = fetch_battles('SELECT * FROM battle_events WHERE strength_delta=0 OR strength_delta IS NULL ORDER BY detected_ts DESC LIMIT 50')

        # Territory activity from latest snapshot — territories with a recent
        # last_battle timestamp, regardless of ownership change.
        # last_battle is stored in milliseconds in the Loka API.
        latest_poll = c.execute('SELECT MAX(poll_ts) FROM territory_snapshots').fetchone()[0]
        recent_activity = []
        if latest_poll:
            week_ago_ms = (now - 7 * 86400) * 1000
            snap_rows = c.execute('''
                SELECT territory_num, world, area_name, mutator, town_id, last_battle
                FROM territory_snapshots
                WHERE poll_ts = ? AND last_battle > ?
                ORDER BY last_battle DESC LIMIT 100
            ''', (latest_poll, week_ago_ms)).fetchall()

            town_lookup = _build_town_lookup()
            for row in snap_rows:
                town_id   = row['town_id'] or ''
                info      = town_lookup.get(town_id, {})
                raw_world = row['world'] or ''
                raw_area  = row['area_name'] or ''
                recent_activity.append({
                    'territory_num':    row['territory_num'],
                    'world':            raw_world,
                    'world_display':    _display_world(raw_world),
                    'area_name':        _display_area(raw_area, raw_world),
                    'mutator':          row['mutator'],
                    'town_id':          town_id,
                    'town_name':        info.get('town_name', ''),
                    'alliance_name':    info.get('alliance_name', ''),
                    'last_battle':      row['last_battle'],
                    'last_battle_ts':   int(row['last_battle'] / 1000) if row['last_battle'] else None,
                    'territory_won_by': 'activity',
                })

        db.close()

        # Resolve town IDs → names for all battle event lists
        town_lookup = _build_town_lookup()
        def _resolve_battle(b):
            if not b.get('old_town_name') and b.get('old_town_id'):
                info = town_lookup.get(b['old_town_id'], {})
                b['old_town_name']     = info.get('town_name', '')
                b['old_alliance_name'] = info.get('alliance_name', '')
            if not b.get('new_town_name') and b.get('new_town_id'):
                info = town_lookup.get(b['new_town_id'], {})
                b['new_town_name']     = info.get('town_name', '')
                b['new_alliance_name'] = info.get('alliance_name', '')
            return b

        recent          = [_resolve_battle(b) for b in recent]
        top_alliance    = [_resolve_battle(b) for b in top_alliance]
        rivina          = [_resolve_battle(b) for b in rivina]
        no_transfer     = [_resolve_battle(b) for b in no_transfer]

        return jsonify({
            'recent_battles':      recent,
            'top_alliance_battles': top_alliance,
            'rivina_battles':      rivina,
            'no_transfer_battles': no_transfer,
            'recent_activity':     recent_activity,
        })
    except Exception as e:
        return jsonify({
            'recent_battles': [], 'top_alliance_battles': [],
            'rivina_battles': [], 'no_transfer_battles': [],
            'recent_activity': [], 'error': str(e)
        })

# ── api: site config ───────────────────────────────────────────────────────────
@app.route("/api/site_config")
def api_site_config():
    try:
        config_path = os.path.join(BASE_DIR, 'site_config.json')
        with open(config_path) as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({'announcement': {'enabled': False, 'text': '', 'type': 'info'}, 'hero_images': []})

# ── api: founders ──────────────────────────────────────────────────────────────
@app.route("/api/founders")
def api_founders():
    try:
        founders_path = os.path.join(BASE_DIR, 'founders.json')
        with open(founders_path) as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify([])

# ── api: eldritch bot player stats ────────────────────────────────────────────
from eldritch_scraper import (
    fetch_player, save_player, get_player, get_leaderboard,
    search_players, name_to_uuid, fmt_uuid, queue_players, queue_by_names,
    get_queue_stats, load_loka_players, queue_all_loka_players, start_worker,
)

ELDRITCH_STALE = 86400  # 24 hours — re-scrape if older

@app.route("/api/eldritch/leaderboard")
def api_eldritch_leaderboard():
    sort  = request.args.get('sort', 'kills')
    limit = min(int(request.args.get('limit', 50)), 200)
    return jsonify(get_leaderboard(sort, limit))

@app.route("/api/eldritch/search")
def api_eldritch_search():
    q = request.args.get('q', '').strip()
    if not q or len(q) > 32:
        return jsonify([])
    import sqlite3 as _sq
    from eldritch_scraper import LOKA_PATH
    db = _sq.connect(LOKA_PATH)
    db.row_factory = _sq.Row
    rows = db.execute(
        'SELECT uuid, name FROM loka_players WHERE name LIKE ? ORDER BY name COLLATE NOCASE LIMIT 12',
        (f'{q}%',)
    ).fetchall()
    db.close()
    return jsonify([{'uuid': r['uuid'], 'name': r['name']} for r in rows if r['name']])

@app.route("/api/eldritch/status")
def api_eldritch_status():
    return jsonify(get_queue_stats())

@app.route("/api/eldritch/player/<path:identifier>")
def api_eldritch_player(identifier):
    """Accepts a UUID (with/without dashes) or a player name."""
    raw = identifier.strip()
    is_uuid = bool(re.match(r'^[0-9a-f\-]{32,36}$', raw, re.I))
    if is_uuid:
        uuid = fmt_uuid(raw)
    else:
        uuid = name_to_uuid(raw)
        if not uuid:
            return jsonify({'error': 'Player not found'}), 404

    # Always fetch live from EldritchBot
    try:
        data = fetch_player(uuid)
        if data.get('error') == 'not_found':
            return jsonify({'error': 'Player has no EldritchBot record'}), 404
        # Persist to local DB if not already present, so they appear in the leaderboard
        if not get_player(uuid):
            threading.Thread(target=save_player, args=(data,), daemon=True).start()
    except Exception as e:
        # Fall back to cache if live fetch fails
        data = get_player(uuid)
        if not data:
            return jsonify({'error': str(e)}), 502

    # Resolve nemesis name → UUID so the frontend can link by UUID (survives name changes)
    if data.get('nemesis'):
        nem_uuid = name_to_uuid(data['nemesis'])
        if nem_uuid:
            data['nemesis_uuid'] = nem_uuid

    return jsonify(data)

def _eldritch_init():
    """
    Startup: re-queue stale player_stats records for daily refresh, then
    start the background worker so those jobs get processed continuously.
    """
    time.sleep(5)
    import sqlite3 as _sq
    from eldritch_scraper import DB_PATH as _EB_PATH, LOKA_PATH as _LOKA_PATH, _REFRESH_DAYS
    db   = _sq.connect(_EB_PATH)
    loka = _sq.connect(_LOKA_PATH)
    now    = int(time.time())
    cutoff = now - _REFRESH_DAYS * 86400

    # 1) Re-queue existing records that are stale (>30 days old)
    db.execute('''
        INSERT OR IGNORE INTO scrape_queue (uuid, priority, queued_ts, status)
        SELECT uuid, 9, ?, 'pending'
        FROM player_stats
        WHERE error IS NULL AND name != '' AND scraped_ts < ?
    ''', (now, cutoff))
    stale = db.execute('SELECT changes()').fetchone()[0]

    # 2) Queue any Loka players never attempted yet (not in scrape_queue at all)
    loka_uuids = [r[0] for r in loka.execute('SELECT uuid FROM loka_players').fetchall()]
    loka.close()
    new_q = 0
    for uuid in loka_uuids:
        db.execute('''
            INSERT OR IGNORE INTO scrape_queue (uuid, priority, queued_ts, status)
            VALUES (?, 9, ?, 'pending')
        ''', (uuid, now))
        new_q += db.execute('SELECT changes()').fetchone()[0]

    db.commit()
    db.close()

    if stale:
        print(f'[eldritch] re-queued {stale} stale records for refresh')
    if new_q:
        print(f'[eldritch] queued {new_q} new Loka players for first scrape')

    # 3) Start the persistent background worker
    start_worker()
    print('[eldritch] background scrape worker started')

# ── loka data cache (file-backed, refreshed every 5 min) ─────────────────────
LOKA_CACHE_DIR       = os.path.join(BASE_DIR, "cache")
LOKA_CACHE_ALLIANCES = os.path.join(LOKA_CACHE_DIR, "alliances.json")
LOKA_CACHE_TOWNS     = os.path.join(LOKA_CACHE_DIR, "towns.json")
LOKA_CACHE_INTERVAL  = 300   # seconds
_loka_lock           = threading.Lock()

os.makedirs(LOKA_CACHE_DIR, exist_ok=True)

def _loka_fetch_all(base_url, embedded_key):
    """Fetch every page from a Loka API endpoint and return the combined list."""
    results = []
    page    = 0
    while True:
        sep = "&" if "?" in base_url else "?"
        url = f"{base_url}{sep}size=100&page={page}"
        req = urllib.request.Request(url, headers={"User-Agent": "LokaUtils/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        items = data.get("_embedded", {}).get(embedded_key, [])
        results.extend(items)
        pg = data.get("page", {})
        if not items or page >= pg.get("totalPages", 1) - 1:
            break
        page += 1
    return results

def _write_cache_file(path, items, embedded_key):
    """Write items as a single-page Loka-style response (atomic via tmp file)."""
    payload = {
        "_embedded": {embedded_key: items},
        "page": {"size": len(items), "totalElements": len(items),
                 "totalPages": 1, "number": 0},
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)   # atomic on Linux

def _refresh_loka_cache():
    for label, url, key, path in [
        ("alliances", "https://api.lokamc.com/alliances",              "alliances", LOKA_CACHE_ALLIANCES),
        ("towns",     "https://api.lokamc.com/towns/search/findAll",   "towns",     LOKA_CACHE_TOWNS),
    ]:
        try:
            items = _loka_fetch_all(url, key)
            with _loka_lock:
                _write_cache_file(path, items, key)
            print(f"[loka-cache] {len(items)} {label} cached")
        except Exception as e:
            print(f"[loka-cache] {label} error: {e}")

def _loka_cache_loop():
    _refresh_loka_cache()          # populate immediately on startup
    while True:
        time.sleep(LOKA_CACHE_INTERVAL)
        _refresh_loka_cache()

# ── proxy: lokamc.com (browser can't call it directly — no CORS headers) ──────
@app.route("/api/lokamc/<path:path>")
def loka_proxy(path):
    # Serve alliances + towns from the file cache
    cache_file = None
    if path == "alliances":
        cache_file = LOKA_CACHE_ALLIANCES
    elif path == "towns/search/findAll":
        cache_file = LOKA_CACHE_TOWNS

    if cache_file and os.path.exists(cache_file):
        with _loka_lock:
            with open(cache_file) as f:
                body = f.read()
        return app.response_class(body, content_type="application/json")

    # Pass-through for player lookups and anything not yet cached
    qs  = request.query_string.decode()
    url = f"https://api.lokamc.com/{path}"
    if qs:
        url += f"?{qs}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LokaUtils/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        return app.response_class(data, content_type="application/json")
    except urllib.error.HTTPError as e:
        return jsonify({"error": str(e)}), e.code
    except Exception as e:
        return jsonify({"error": str(e)}), 502

# ── startup ───────────────────────────────────────────────────────────────────
import database as _db_module; _db_module.init_db()
ensure_indexes()
ensure_battle_tables()
threading.Thread(target=_warmup,          daemon=True).start()
threading.Thread(target=_loka_cache_loop, daemon=True).start()
threading.Thread(target=_fight_poll_loop,  daemon=True).start()
threading.Thread(target=_eldritch_init,    daemon=True).start()
threading.Thread(target=_rebuild_fights_index, daemon=True).start()  # build fights index on startup
# Note: background scrape worker is started inside _eldritch_init after a 5s delay.
# For a full first-time bulk scrape: python run_scraper.py --threads 8

def _get_alliances_for_poller():
    """Return alliances list from the loka cache file."""
    try:
        with _loka_lock:
            with open(LOKA_CACHE_ALLIANCES) as f:
                data = json.load(f)
        return data.get('_embedded', {}).get('alliances', [])
    except Exception:
        return []

from territory_poller import start_territory_poller
start_territory_poller(_get_alliances_for_poller)

if __name__ == "__main__":
    # For local dev only — production uses gunicorn (see gunicorn.conf.py)
    app.run(host="127.0.0.1", port=5000, debug=False)
