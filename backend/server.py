import os
import re
import time
import json
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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "market_history.db")
FRONTEND = os.path.join(BASE_DIR, "..", "frontend")

# ── app ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)

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
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' https://mc-heads.net https://raw.githubusercontent.com data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    return resp

# ── UUID → username resolution ────────────────────────────────────────────────
_UUID_CACHE: dict    = {}   # uuid -> resolved username (or '' if failed)
_UUID_LOCK           = threading.Lock()
_STD_UUID_RE         = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

def _is_standard_uuid(uid: str) -> bool:
    """Standard Minecraft UUID has dashes (8-4-4-4-12 format)."""
    return bool(uid and _STD_UUID_RE.match(uid))

def _fetch_one_uuid(uid: str) -> tuple[str, str]:
    """Returns (uuid, username). Username is '' on failure."""
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
    """Persist resolved names to DB so they survive restarts."""
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
    Resolve a list of UUIDs to display names.
    Standard-format UUIDs  → Mojang API lookup (parallel, cached).
    Non-standard (Loka)   → truncated hex shown as-is.
    Returns dict: uuid → display_name.
    """
    result: dict[str, str] = {}
    to_fetch: list[str]    = []

    for uid in uuids:
        if not uid:
            result[uid] = "Unknown"
            continue
        if not _is_standard_uuid(uid):
            # Loka-specific short ID — show first 8 chars
            result[uid] = uid[:8] + "…"
            continue
        with _UUID_LOCK:
            if uid in _UUID_CACHE:
                result[uid] = _UUID_CACHE[uid] or (uid[:8] + "…")
                continue
        to_fetch.append(uid)

    if to_fetch:
        workers = min(10, len(to_fetch))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_fetch_one_uuid, u): u for u in to_fetch}
            for fut in as_completed(futures, timeout=8):
                try:
                    uid, name = fut.result()
                    with _UUID_LOCK:
                        _UUID_CACHE[uid] = name
                    result[uid] = name if name else (uid[:8] + "…")
                except Exception as e:
                    uid = futures[fut]
                    result[uid] = uid[:8] + "…"

        # Persist to DB in background
        pairs = [(u, _UUID_CACHE.get(u, "")) for u in to_fetch if _UUID_CACHE.get(u)]
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

# ── build items list ──────────────────────────────────────────────────────────
def _build_items():
    conn = get_db()
    c    = conn.cursor()
    rows = c.execute(
        "SELECT item, COUNT(*) AS vol, MAX(ts) AS last_ts FROM trades GROUP BY item ORDER BY vol DESC"
    ).fetchall()
    results = []
    for row in rows:
        name   = row["item"]
        prows  = c.execute(
            "SELECT price FROM trades WHERE item=? ORDER BY ts DESC, rowid DESC LIMIT 200",
            (name,)
        ).fetchall()
        prices = [p[0] for p in prows]
        stats  = _compute_stats(prices)
        if stats is None:
            continue
        results.append({"item": name, "volume": row["vol"],
                        "last_price": prices[0] if prices else None,
                        "last_ts": row["last_ts"], **stats})
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
    trades = c.execute("""
        SELECT price, buyer_uuid, seller_uuid, ts
        FROM trades WHERE item=?
        ORDER BY ts DESC, rowid DESC LIMIT 100
    """, (item_name,)).fetchall()
    total  = c.execute("SELECT COUNT(*) FROM trades WHERE item=?", (item_name,)).fetchone()[0]
    prows  = c.execute(
        "SELECT price FROM trades WHERE item=? ORDER BY ts DESC, rowid DESC LIMIT 200",
        (item_name,)
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
        ORDER BY ts DESC, rowid DESC
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

# ── proxy: lokamc.com (browser can't call it directly — no CORS headers) ──────
@app.route("/api/lokamc/<path:path>")
def loka_proxy(path):
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
ensure_indexes()
threading.Thread(target=_warmup, daemon=True).start()

if __name__ == "__main__":
    # For local dev only — production uses gunicorn (see gunicorn.conf.py)
    app.run(host="127.0.0.1", port=5000, debug=False)
