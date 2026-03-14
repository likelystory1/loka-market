import re
import os
import json
from collections import defaultdict
from pathlib import Path

FIGHTLOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fightlogs')

RE_LINE       = re.compile(r'^\[[\d:.]+\] (.*)')
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


def _cache_path(filepath: str) -> str:
    return filepath + '.cache.json'


def _load_cache(filepath: str):
    cp = _cache_path(filepath)
    try:
        if os.path.exists(cp) and os.path.getmtime(cp) >= os.path.getmtime(filepath):
            with open(cp, encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _save_cache(filepath: str, data: dict):
    try:
        with open(_cache_path(filepath), 'w', encoding='utf-8') as f:
            json.dump(data, f, separators=(',', ':'))
    except Exception as e:
        print(f'[fights_parser] cache write failed: {e}')


def parse_fight_log(filepath: str) -> dict:
    cached = _load_cache(filepath)
    if cached:
        return cached

    filename = os.path.basename(filepath)

    # All stats initialised up front
    location = ''
    winner   = ''
    duration = ''
    mutator  = ''
    attacker_map: dict = {}
    defender_map: dict = {}

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

    # Single-pass: collect events first, then parse team sections at the end
    in_section      = None   # 'attackers' | 'defenders' | None
    past_battle_end = False

    with open(filepath, encoding='utf-8', errors='replace') as f:
        for raw in f:
            m = RE_LINE.match(raw)
            if not m:
                continue
            body = m.group(1)

            # ── Post-battle-end: only read team / gear sections ──────────────
            if past_battle_end:
                ms = RE_SECTION.match(body)
                if ms:
                    sec = ms.group(1).strip()
                    in_section = {'Attackers': 'attackers', 'Defenders': 'defenders'}.get(sec)
                    continue
                if in_section and not body.startswith('{'):
                    mt = RE_TOWN.match(body)
                    if mt:
                        town = mt.group(1).strip()
                        for p in [x.strip() for x in mt.group(2).split(',') if x.strip()]:
                            if in_section == 'attackers':
                                attacker_map[p] = town
                            else:
                                defender_map[p] = town
                continue

            # ── Pre-battle-end: metadata + events ───────────────────────────
            if body == '=== Battle Ended ===':
                past_battle_end = True
                continue

            if not location:
                ml = RE_LOCATION.match(body)
                if ml:
                    location = ml.group(1).strip()
                    continue

            if not winner and body.startswith('Winner: '):
                winner = body[8:].strip(); continue
            if not duration and body.startswith('Battle lasted '):
                duration = body[14:].strip(); continue
            if not mutator and body.startswith('Mutator: '):
                mutator = body[9:].strip(); continue

            # Events — we don't know team membership yet so we store raw data
            # and resolve teams after the full file is read.

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
                # Store raw dmg; filter cross-team after we know teams
                dmg_dealt[att] += dmg          # will subtract friendly later if needed
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
                    # Assists resolved after team data is loaded — store pending
                    # We tag the kill with the current attacker list
                    for (hitter, _) in victim_attackers.get(vic, []):
                        # Mark as assist candidate; team check done below
                        assists[f'__pending__{vic}__{killer}_{hitter}'] = 1
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

    # ── Resolve assists now that we know team membership ──────────────────────
    # Re-run kill logic using stored victim_attackers isn't possible since we
    # cleared it above.  Instead we encoded pending assists as special keys.
    # Simpler: redo a lightweight second pass only through kill lines.
    all_known = set(attacker_map) | set(defender_map)

    def _team(p):
        if p in attacker_map: return 'attackers'
        if p in defender_map: return 'defenders'
        return None

    # Clear the assist pseudo-keys
    assists.clear()
    # Redo cross-team damage_dealt filter
    dmg_dealt.clear()
    ASSIST_MIN_DMG = 8.0   # minimum cross-team damage to earn an assist
    victim_attackers2 = defaultdict(dict)  # vic -> {att: dmg_total}

    with open(filepath, encoding='utf-8', errors='replace') as f:
        for raw in f:
            m = RE_LINE.match(raw)
            if not m:
                continue
            body = m.group(1)
            if body == '=== Battle Ended ===':
                break

            mh = RE_HIT.match(body)
            if mh:
                att, vic, dmg = mh.group(1), mh.group(3), float(mh.group(4))
                if att in all_known and vic in all_known and _team(att) != _team(vic):
                    dmg_dealt[att] += dmg
                    victim_attackers2[vic][att] = victim_attackers2[vic].get(att, 0.0) + dmg
                continue

            mk = RE_KILL.match(body)
            if mk:
                killer, is_team, vic, has_coord = mk.group(1), mk.group(2) is not None, mk.group(3), mk.group(4) is not None
                if has_coord and vic in all_known and not is_team and killer in all_known:
                    kt = _team(killer)
                    for hitter, dmg in victim_attackers2.get(vic, {}).items():
                        if hitter != killer and _team(hitter) == kt and dmg >= ASSIST_MIN_DMG:
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
        key=lambda p: (-p['kills'], -p['damage_dealt'])
    )
    def_players = sorted(
        [_make_player(n, t) for n, t in defender_map.items()],
        key=lambda p: (-p['kills'], -p['damage_dealt'])
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

    result = {
        'filename':        filename,
        'location':        location,
        'winner':          winner,
        'duration':        duration,
        'mutator':         mutator,
        'attackers':       att_players,
        'defenders':       def_players,
        'attacker_kills':  sum(p['kills'] for p in att_players),
        'defender_kills':  sum(p['kills'] for p in def_players),
        'attacker_totals': _totals(att_players),
        'defender_totals': _totals(def_players),
    }

    _save_cache(filepath, result)
    return result


def list_fights() -> list:
    results = []
    if not os.path.isdir(FIGHTLOGS_DIR):
        return results

    for path in sorted(Path(FIGHTLOGS_DIR).glob('*.txt'),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            parsed = parse_fight_log(str(path))
        except Exception as e:
            print(f'[fights_parser] error parsing {path.name}: {e}')
            continue

        results.append({
            'filename':       path.name,
            'display_name':   path.stem.replace('_', ' '),
            'location':       parsed['location'],
            'winner':         parsed['winner'],
            'duration':       parsed['duration'],
            'attacker_count': len(parsed['attackers']),
            'defender_count': len(parsed['defenders']),
            'attacker_kills': parsed['attacker_kills'],
            'defender_kills': parsed['defender_kills'],
        })

    return results
