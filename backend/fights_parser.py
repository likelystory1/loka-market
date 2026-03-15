import re
import os
import json
from collections import defaultdict
from pathlib import Path

FIGHTLOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fightlogs')

RE_LINE       = re.compile(r'^\[([\d:.]+)\] (.*)')
RE_LOCATION   = re.compile(r'^Location: (.+)$')
RE_FIRE_POT   = re.compile(r'^(\S+) \([\d.]+ hp\) fired a (Splash Potion of .+)$')
RE_FIRE_PEARL = re.compile(r'^(\S+) \([\d.]+ hp\) fired a Thrown Ender Pearl$')
RE_CONSUME    = re.compile(r'^(\S+) \([\d.]+ hp\) consumed (.+)$')
RE_HIT        = re.compile(r'^(\S+) \([\d.]+ hp\) (hit|crit) (.+?) \([\d.]+ hp\) for ([\d.]+) damage')
RE_TOOK       = re.compile(r'^(\S+) \([\d.]+ hp\) took ([\d.]+) damage')
RE_KILL       = re.compile(r'^(\S+) \([\d.]+ hp\) (team )?killed (.+?) \(0\.0 hp\)( at .+)?$')
RE_SECTION    = re.compile(r'^===(.+)===$')
RE_TOWN       = re.compile(r'^(.+?): (.+)$')
RE_BROKE      = re.compile(r'^(\S+) \([\d.]+ hp\) broke a (.+?) at ')
RE_PLACED     = re.compile(r'^(\S+) \([\d.]+ hp\) placed a (.+?) at ')

WORLD_DISPLAY = {
    'south':  'Garama',
    'north':  'Kalros',
    'west':   'Ascalon',
    'lilboi': 'Rivina',
    'bigboi': 'Balak',
}
MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

ASSIST_TIME_WIN   = 5.0   # seconds before kill that a hit counts as assist


def _ts_to_sec(ts: str) -> float:
    """Convert HH:MM:SS.mmm timestamp string to total seconds."""
    try:
        parts = ts.split(':')
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except Exception:
        return 0.0


def parse_filename_meta(filename: str) -> dict:
    """Parse display metadata from filename like: south-47-TGen_03-10-2026_17-29"""
    stem = filename
    for ext in ('.txt', '.json'):
        if stem.endswith(ext):
            stem = stem[:-len(ext)]

    parts = stem.split('_')
    world = ''
    territory_num = ''
    date_display = ''
    time_display = ''

    if parts:
        wparts = parts[0].split('-')
        world = WORLD_DISPLAY.get(wparts[0].lower(), wparts[0].capitalize()) if wparts else ''
        territory_num = wparts[1] if len(wparts) > 1 else ''

    if len(parts) > 1:
        dparts = parts[1].split('-')
        if len(dparts) == 3:
            try:
                date_display = f"{MONTHS[int(dparts[0]) - 1]} {int(dparts[1])} {dparts[2]}"
            except (ValueError, IndexError):
                date_display = parts[1]

    if len(parts) > 2:
        tparts = parts[2].split('-')
        if len(tparts) >= 2:
            time_display = f"{tparts[0]}:{tparts[1]}"

    return {
        'world':         world,
        'territory_num': territory_num,
        'date_display':  date_display,
        'time_display':  time_display,
    }


def parse_fight_log(filepath: str) -> dict:
    filename = os.path.basename(filepath)

    location = ''
    winner   = ''
    duration = ''
    mutator  = ''
    attacker_map: dict = {}
    defender_map: dict = {}
    attacker_primary_town = ''
    defender_primary_town = ''

    kills          = defaultdict(int)
    deaths         = defaultdict(int)
    assists        = defaultdict(int)
    pearls         = defaultdict(int)
    crits          = defaultdict(int)
    total_hits     = defaultdict(int)
    dmg_dealt      = defaultdict(float)
    dmg_taken      = defaultdict(float)
    potions        = defaultdict(lambda: defaultdict(int))
    food           = defaultdict(lambda: defaultdict(int))
    shulkers_broken  = defaultdict(int)
    shulkers_placed  = defaultdict(int)
    blocks_broken    = defaultdict(int)
    items_dropped    = defaultdict(int)
    golem_kills      = defaultdict(int)
    charges_taken    = defaultdict(int)
    charge_part      = defaultdict(int)
    ancient_ingots   = defaultdict(int)

    victim_attackers = defaultdict(list)

    in_section      = None
    past_battle_end = False

    with open(filepath, encoding='utf-8', errors='replace') as f:
        for raw in f:
            m = RE_LINE.match(raw)
            if not m:
                continue
            body = m.group(2)

            if past_battle_end:
                if not winner and body.startswith('Winner: '):
                    winner = body[8:].strip(); continue
                if not duration and body.startswith('Battle lasted '):
                    duration = body[14:].strip(); continue
                ms = RE_SECTION.match(body)
                if ms:
                    sec = ms.group(1).strip()
                    in_section = {'Attackers': 'attackers', 'Defenders': 'defenders'}.get(sec)
                    continue
                if in_section and not body.startswith('{'):
                    mt = RE_TOWN.match(body)
                    if mt:
                        town = mt.group(1).strip()
                        players = [x.strip() for x in mt.group(2).split(',') if x.strip()]
                        for p in players:
                            if in_section == 'attackers':
                                attacker_map[p] = town
                            else:
                                defender_map[p] = town
                        if players:
                            if in_section == 'attackers' and not attacker_primary_town:
                                attacker_primary_town = town
                            elif in_section == 'defenders' and not defender_primary_town:
                                defender_primary_town = town
                continue

            if body == '=== Battle Ended ===':
                past_battle_end = True
                continue

            if not location:
                ml = RE_LOCATION.match(body)
                if ml:
                    location = ml.group(1).strip()
                    continue

            if not winner and body.startswith('Winner: '):
                winner = body[8:].strip(); continue  # fallback for non-standard log ordering
            if not duration and body.startswith('Battle lasted '):
                duration = body[14:].strip(); continue
            if not mutator and body.startswith('Mutator: '):
                mutator = body[9:].strip(); continue

            mp = RE_FIRE_POT.match(body)
            if mp:
                potions[mp.group(1)][mp.group(2)[len('Splash Potion of '):]] += 1
                continue

            if RE_FIRE_PEARL.match(body):
                pearls[body.split()[0]] += 1
                continue

            mc = RE_CONSUME.match(body)
            if mc:
                food[mc.group(1)][mc.group(2)] += 1
                continue

            mh = RE_HIT.match(body)
            if mh:
                att, htype, vic, dmg = mh.group(1), mh.group(2), mh.group(3), float(mh.group(4))
                total_hits[att] += 1
                if htype == 'crit':
                    crits[att] += 1
                dmg_dealt[att] += dmg
                victim_attackers[vic].append((att, dmg))
                continue

            mt2 = RE_TOOK.match(body)
            if mt2:
                dmg_taken[mt2.group(1)] += float(mt2.group(2))
                continue

            mk = RE_KILL.match(body)
            if mk:
                killer, is_team, vic, has_coord = mk.group(1), mk.group(2) is not None, mk.group(3), mk.group(4) is not None
                if not has_coord:
                    victim_attackers.pop(vic, None)
                    continue
                deaths[vic] += 1
                if not is_team:
                    kills[killer] += 1
                victim_attackers.pop(vic, None)
                continue

            mb = RE_BROKE.match(body)
            if mb:
                blk = mb.group(2)
                if 'SHULKER' in blk.upper():
                    shulkers_broken[mb.group(1)] += 1
                else:
                    blocks_broken[mb.group(1)] += 1
                continue

            mpl = RE_PLACED.match(body)
            if mpl and ('Shulker' in mpl.group(2) or 'SHULKER' in mpl.group(2).upper()):
                shulkers_placed[mpl.group(1)] += 1
                continue

            if ' dropped ' in body:
                items_dropped[body.split()[0]] += 1
                continue

            if 'got charged' in body:
                golem_kills[body.split()[0]] += 1
                continue

            if 'overloaded a Power Source' in body:
                charges_taken[body.split()[0]] += 1
                continue

            if 'began an overload' in body:
                charge_part[body.split()[0]] += 1
                continue

            if 'Ancient Ingot' in body and 'repaired' in body:
                ancient_ingots[body.split()[0]] += 1
                continue

    # ── Second pass: cross-team damage, assists with 5-second window ──────────
    all_known = set(attacker_map) | set(defender_map)

    def _team(p):
        if p in attacker_map: return 'attackers'
        if p in defender_map: return 'defenders'
        return None

    dmg_dealt.clear()
    # vic -> {att: (total_dmg, last_hit_ts)}
    victim_attackers2 = defaultdict(dict)

    with open(filepath, encoding='utf-8', errors='replace') as f:
        for raw in f:
            m = RE_LINE.match(raw)
            if not m:
                continue
            cur_ts = _ts_to_sec(m.group(1))
            body   = m.group(2)
            if body == '=== Battle Ended ===':
                break

            mh = RE_HIT.match(body)
            if mh:
                att, vic, dmg = mh.group(1), mh.group(3), float(mh.group(4))
                if att in all_known and vic in all_known and _team(att) != _team(vic):
                    dmg_dealt[att] += dmg
                    prev_dmg, _ = victim_attackers2[vic].get(att, (0.0, None))
                    victim_attackers2[vic][att] = (prev_dmg + dmg, cur_ts)
                continue

            mk = RE_KILL.match(body)
            if mk:
                killer, is_team, vic, has_coord = mk.group(1), mk.group(2) is not None, mk.group(3), mk.group(4) is not None
                if has_coord and vic in all_known and not is_team and killer in all_known:
                    kt = _team(killer)
                    for hitter, (dmg, last_ts) in victim_attackers2.get(vic, {}).items():
                        if (hitter != killer
                                and _team(hitter) == kt
                                and last_ts is not None
                                and cur_ts - last_ts <= ASSIST_TIME_WIN):
                            assists[hitter] += 1
                victim_attackers2.pop(vic, None)

    # ── Build player dicts ────────────────────────────────────────────────────
    def _make_player(name, town):
        h = total_hits[name]
        c = crits[name]
        return {
            'name':            name,
            'town':            town,
            'kills':           kills[name],
            'deaths':          deaths[name],
            'assists':         assists[name],
            'pearls':          pearls[name],
            'damage_dealt':    round(dmg_dealt[name], 1),
            'damage_taken':    round(dmg_taken[name], 1),
            'total_hits':      h,
            'crits':           c,
            'crit_ratio':      round(c / h * 100) if h else 0,
            'shulkers_broken': shulkers_broken[name],
            'shulkers_placed': shulkers_placed[name],
            'blocks_broken':   blocks_broken[name],
            'items_dropped':   items_dropped[name],
            'golem_kills':     golem_kills[name],
            'charges_taken':   charges_taken[name],
            'charge_part':     charge_part[name],
            'ancient_ingots':  ancient_ingots[name],
            'potions':         dict(sorted(potions[name].items(), key=lambda x: -x[1])),
            'food':            dict(sorted(food[name].items(),    key=lambda x: -x[1])),
        }

    att_players = sorted(
        [_make_player(n, t) for n, t in attacker_map.items()],
        key=lambda p: (-p['kills'], -p['assists'], -p['damage_dealt'])
    )
    def_players = sorted(
        [_make_player(n, t) for n, t in defender_map.items()],
        key=lambda p: (-p['kills'], -p['assists'], -p['damage_dealt'])
    )

    def _totals(players):
        pot = defaultdict(int)
        for p in players:
            for k, v in p['potions'].items():
                pot[k] += v
        return {
            'kills':         sum(p['kills'] for p in players),
            'damage':        round(sum(p['damage_dealt'] for p in players), 1),
            'pearls':        sum(p['pearls'] for p in players),
            'food':          sum(sum(p['food'].values()) for p in players),
            'golem_kills':   sum(p['golem_kills'] for p in players),
            'charges':       sum(p['charges_taken'] for p in players),
            'charge_part':   sum(p['charge_part'] for p in players),
            'total_potions': sum(pot.values()),
            'potions':       dict(sorted(pot.items(), key=lambda x: -x[1])),
        }

    meta = parse_filename_meta(filename)

    return {
        'filename':        filename,
        'location':        location,
        'winner':          winner,
        'duration':        duration,
        'mutator':         mutator,
        'attacker_town':   attacker_primary_town,
        'defender_town':   defender_primary_town,
        'world':           meta['world'],
        'territory_num':   meta['territory_num'],
        'date_display':    meta['date_display'],
        'time_display':    meta['time_display'],
        'attackers':       att_players,
        'defenders':       def_players,
        'attacker_kills':  sum(p['kills'] for p in att_players),
        'defender_kills':  sum(p['kills'] for p in def_players),
        'attacker_totals': _totals(att_players),
        'defender_totals': _totals(def_players),
    }
