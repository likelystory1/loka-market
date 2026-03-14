import os
import sqlite3
import threading
import time
import json
import requests
import logging

logger = logging.getLogger(__name__)

POLL_INTERVAL = 300  # 5 minutes
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'territories.db')
PRUNE_HOURS = 48

def _get_db():
    return sqlite3.connect(DB_PATH)

def _snapshot_alliance_strengths(db, poll_ts, alliances_cache):
    """Snapshot current alliance strengths."""
    if not alliances_cache:
        return {}
    c = db.cursor()
    strength_map = {}  # alliance_id -> (strength, bb_strength, name)
    for alliance in alliances_cache:
        aid = str(alliance.get('id', ''))
        name = alliance.get('name', '')
        strength = alliance.get('strength', 0) or 0
        bb_strength = alliance.get('bbStrength', 0) or 0
        if aid:
            strength_map[aid] = (strength, bb_strength, name)
            try:
                c.execute(
                    'INSERT OR IGNORE INTO alliance_strength_snapshots (poll_ts, alliance_id, alliance_name, strength, bb_strength) VALUES (?,?,?,?,?)',
                    (poll_ts, aid, name, strength, bb_strength)
                )
            except Exception as e:
                logger.warning(f'Error saving alliance strength: {e}')
    db.commit()
    return strength_map

def _build_town_alliance_map(alliances_cache):
    """Build town_id -> {alliance_id, alliance_name} lookup."""
    mapping = {}
    if not alliances_cache:
        return mapping
    for alliance in alliances_cache:
        aid = str(alliance.get('id', ''))
        aname = alliance.get('name', '')
        for town in alliance.get('towns', []):
            tid = str(town.get('id', ''))
            tname = town.get('name', '')
            if tid:
                mapping[tid] = {'alliance_id': aid, 'alliance_name': aname, 'town_name': tname}
    return mapping

def _build_town_name_map(alliances_cache):
    """Build town_id -> town_name from alliances cache."""
    mapping = {}
    if not alliances_cache:
        return mapping
    for alliance in alliances_cache:
        for town in alliance.get('towns', []):
            tid = str(town.get('id', ''))
            tname = town.get('name', '')
            if tid:
                mapping[tid] = tname
    return mapping

def _fetch_territories(base_url='https://api.lokamc.com'):
    """Fetch all territories paginated. API uses HAL _embedded format."""
    territories = []
    page = 0
    size = 100
    while True:
        try:
            resp = requests.get(f'{base_url}/territories', params={'size': size, 'page': page}, timeout=15)
            if resp.status_code != 200:
                break
            data = resp.json()
            # HAL format: { _embedded: { territories: [...] }, page: { totalPages: N } }
            if isinstance(data, dict) and '_embedded' in data:
                items = data['_embedded'].get('territories', [])
                page_info = data.get('page', {})
                total_pages = page_info.get('totalPages', 1)
            elif isinstance(data, list):
                items = data
                total_pages = 1
            else:
                items = data.get('content', [])
                total_pages = data.get('totalPages', 1)
            if not items:
                break
            territories.extend(items)
            if page + 1 >= total_pages:
                break
            page += 1
        except Exception as e:
            logger.warning(f'Error fetching territories page {page}: {e}')
            break
    return territories

def _snapshot_territories(db, poll_ts, territories):
    """Save territory snapshots. Fields may be nested under 'tg'."""
    c = db.cursor()
    for i, t in enumerate(territories):
        if not isinstance(t, dict):
            continue
        try:
            tg = t.get('tg') or {}
            town_id = tg.get('townId') or t.get('townId') or t.get('town_id')
            last_battle = tg.get('lastBattle') if tg.get('lastBattle') is not None else t.get('lastBattle', t.get('last_battle'))
            inhibitor = tg.get('inhibitor') or t.get('inhibitorTown') or t.get('inhibitor_town')
            c.execute(
                '''INSERT OR IGNORE INTO territory_snapshots
                   (poll_ts, territory_num, world, area_name, mutator, town_id, last_battle, inhibitor_town, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?)''',
                (
                    poll_ts,
                    t.get('num', i),
                    t.get('world', ''),
                    t.get('areaName', t.get('area_name', '')),
                    t.get('mutator', ''),
                    str(town_id) if town_id else None,
                    last_battle,
                    str(inhibitor) if inhibitor else None,
                    json.dumps(t)
                )
            )
        except Exception as e:
            logger.warning(f'Error saving territory snapshot: {e}')
    db.commit()

def _get_prev_poll_ts(db, current_poll_ts):
    """Get the most recent poll_ts before the current one."""
    c = db.cursor()
    row = c.execute(
        'SELECT MAX(poll_ts) FROM territory_snapshots WHERE poll_ts < ?',
        (current_poll_ts,)
    ).fetchone()
    return row[0] if row and row[0] else None

def _detect_battles(db, poll_ts, prev_poll_ts, town_alliance_map, strength_map, prev_strength_map):
    """Diff current vs previous snapshots to detect battles."""
    if not prev_poll_ts:
        return
    c = db.cursor()
    cur_snaps = c.execute(
        'SELECT territory_num, world, area_name, mutator, town_id, last_battle FROM territory_snapshots WHERE poll_ts=?',
        (poll_ts,)
    ).fetchall()
    prev_snaps = {
        (row[0], row[1]): row
        for row in c.execute(
            'SELECT territory_num, world, area_name, mutator, town_id, last_battle FROM territory_snapshots WHERE poll_ts=?',
            (prev_poll_ts,)
        ).fetchall()
    }

    now = int(time.time())
    for row in cur_snaps:
        tnum, world, area_name, mutator, new_town_id, new_last_battle = row
        prev = prev_snaps.get((tnum, world))
        if not prev:
            continue
        _, _, _, _, old_town_id, old_last_battle = prev

        town_changed = old_town_id != new_town_id
        battle_changed = old_last_battle != new_last_battle

        if not (town_changed or battle_changed):
            continue

        # Determine winner
        if town_changed:
            won_by = 'attacker'
        elif battle_changed:
            won_by = 'defender'
        else:
            won_by = 'unknown'

        # Get alliance info
        old_info = town_alliance_map.get(old_town_id, {}) if old_town_id else {}
        new_info = town_alliance_map.get(new_town_id, {}) if new_town_id else {}
        old_alliance_id = old_info.get('alliance_id', '')
        new_alliance_id = new_info.get('alliance_id', '')
        old_alliance_name = old_info.get('alliance_name', '')
        new_alliance_name = new_info.get('alliance_name', '')
        old_town_name = old_info.get('town_name', '')
        new_town_name = new_info.get('town_name', '')

        # Compute strength delta
        strength_delta = None
        if new_alliance_id and old_alliance_id:
            new_str = strength_map.get(new_alliance_id, (0, 0, ''))[0]
            old_str = prev_strength_map.get(new_alliance_id, (0, 0, ''))[0]
            strength_delta = new_str - old_str if (new_str and old_str) else None

        try:
            c.execute(
                '''INSERT INTO battle_events
                   (detected_ts, territory_num, world, area_name, mutator,
                    old_town_id, new_town_id, old_alliance_id, new_alliance_id,
                    old_alliance_name, new_alliance_name, old_town_name, new_town_name,
                    strength_delta, territory_won_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (now, tnum, world, area_name, mutator,
                 old_town_id, new_town_id, old_alliance_id, new_alliance_id,
                 old_alliance_name, new_alliance_name, old_town_name, new_town_name,
                 strength_delta, won_by)
            )
        except Exception as e:
            logger.warning(f'Error saving battle event: {e}')
    db.commit()

def _get_prev_strength_map(db, prev_poll_ts):
    """Get alliance strength map from previous poll."""
    if not prev_poll_ts:
        return {}
    c = db.cursor()
    rows = c.execute(
        'SELECT alliance_id, strength, bb_strength, alliance_name FROM alliance_strength_snapshots WHERE poll_ts=?',
        (prev_poll_ts,)
    ).fetchall()
    return {row[0]: (row[1], row[2], row[3]) for row in rows}

def _prune_old_snapshots(db):
    """Remove territory_snapshots older than PRUNE_HOURS."""
    cutoff = int(time.time()) - PRUNE_HOURS * 3600
    c = db.cursor()
    c.execute('DELETE FROM territory_snapshots WHERE poll_ts < ?', (cutoff,))
    db.commit()

def poll_cycle(alliances_cache_getter, base_url='https://api.lokamc.com'):
    """Run one complete poll cycle."""
    poll_ts = int(time.time())
    db = _get_db()
    try:
        alliances_cache = alliances_cache_getter() or []

        # Snapshot alliance strengths
        strength_map = _snapshot_alliance_strengths(db, poll_ts, alliances_cache)

        # Build town -> alliance map
        town_alliance_map = _build_town_alliance_map(alliances_cache)

        # Fetch territories
        territories = _fetch_territories(base_url)
        if not territories:
            logger.warning('No territories fetched')
            return

        # Snapshot territories
        _snapshot_territories(db, poll_ts, territories)

        # Get previous poll
        prev_poll_ts = _get_prev_poll_ts(db, poll_ts)
        prev_strength_map = _get_prev_strength_map(db, prev_poll_ts)

        # Detect battles
        _detect_battles(db, poll_ts, prev_poll_ts, town_alliance_map, strength_map, prev_strength_map)

        # Prune old data
        _prune_old_snapshots(db)

        logger.info(f'Territory poll complete: {len(territories)} territories, poll_ts={poll_ts}')
    except Exception as e:
        logger.error(f'Territory poll cycle error: {e}')
    finally:
        db.close()

def start_territory_poller(alliances_cache_getter, base_url='https://api.lokamc.com'):
    """Start the territory poller daemon thread.
    Runs an immediate first cycle, then a quick second cycle 30s later to
    generate the first battle diffs without a full 5-minute wait, then
    continues on the normal POLL_INTERVAL cadence.
    """
    def _loop():
        # Cycle 1 — immediate snapshot
        try:
            poll_cycle(alliances_cache_getter, base_url)
        except Exception as e:
            logger.error(f'Territory poller cycle 1 error: {e}')
        # Cycle 2 — 30s later to produce first battle diffs quickly
        time.sleep(30)
        try:
            poll_cycle(alliances_cache_getter, base_url)
        except Exception as e:
            logger.error(f'Territory poller cycle 2 error: {e}')
        # Subsequent cycles on normal interval
        while True:
            time.sleep(POLL_INTERVAL)
            try:
                poll_cycle(alliances_cache_getter, base_url)
            except Exception as e:
                logger.error(f'Territory poller loop error: {e}')

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    logger.info('Territory poller started (rapid first two cycles)')
    return t
