import re
import os
from collections import defaultdict
from pathlib import Path

FIGHTLOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fightlogs')

RE_LINE       = re.compile(r'^\[(\d+:\d+:\d+\.\d+)\] (.*)')
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
RE_DROPPED    = re.compile(r'^(\S+) \([\d.]+ hp\) dropped ')
RE_GOT_CHARGED = re.compile(r'^(\S+) .* got charged')
RE_OVERLOAD   = re.compile(r'^(\S+) \([\d.]+ hp\) overloaded a Power Source')
RE_BEGAN_OVL  = re.compile(r'^(\S+) \([\d.]+ hp\) began an overload')
RE_ANCIENT    = re.compile(r'^(\S+) .* repaired their .* Ancient Ingot')

# In-memory cache: cache_key -> parsed_result
_PARSE_CACHE: dict = {}


def parse_fight_log(filepath: str) -> dict:
    filename = os.path.basename(filepath)

    # ── Pass 1: scan metadata and team lists ─────────────────────────────────
    location = ''
    winner   = ''
    duration = ''
    mutator  = ''
    attacker_map: dict = {}   # player -> town
    defender_map: dict = {}

    in_section = None

    with open(filepath, encoding='utf-8', errors='replace') as f:
        for raw in f:
            body = RE_LINE.match(raw.rstrip('\n'))
            if not body:
                continue
            body = body.group(2)

            if not location:
                ml = RE_LOCATION.match(body)
                if ml:
                    location = ml.group(1).strip()
                    continue

            if not winner and body.startswith('Winner: '):
                winner = body[8:].strip()
                continue

            if not duration and body.startswith('Battle lasted '):
                duration = body[14:].strip()
                continue

            if not mutator and body.startswith('Mutator: '):
                mutator = body[9:].strip()
                continue

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

    all_known = set(attacker_map) | set(defender_map)

    def _team(p):
        if p in attacker_map: return 'attackers'
        if p in defender_map: return 'defenders'
        return None

    # ── Pass 2: event scan ────────────────────────────────────────────────────
    kills         = defaultdict(int)
    deaths        = defaultdict(int)
    assists       = defaultdict(int)
    pearls        = defaultdict(int)
    crits         = defaultdict(int)
    total_hits    = defaultdict(int)
    dmg_dealt     = defaultdict(float)
    dmg_taken     = defaultdict(float)
    potions       = defaultdict(lambda: defaultdict(int))
    food          = defaultdict(lambda: defaultdict(int))
    shulkers_broken  = defaultdict(int)
    shulkers_placed  = defaultdict(int)
    blocks_broken    = defaultdict(int)
    items_dropped    = defaultdict(int)
    golem_kills      = defaultdict(int)
    charges_taken    = defaultdict(int)
    charge_part      = defaultdict(int)
    ancient_ingots   = defaultdict(int)

    # For assist tracking: victim -> list of attackers who hit them
    victim_attackers = defaultdict(list)

    with open(filepath, encoding='utf-8', errors='replace') as f:
        for raw in f:
            raw = raw.rstrip('\n')
            m = RE_LINE.match(raw)
            if not m:
                continue
            body = m.group(2)

            if body == '=== Battle Ended ===':
                break

            # Potions
            mp = RE_FIRE_POT.match(body)
            if mp:
                player = mp.group(1)
                label  = mp.group(2)[len('Splash Potion of '):]
                if player in all_known:
                    potions[player][label] += 1
                continue

            # Pearls
            if RE_FIRE_PEARL.match(body):
                player = body.split()[0]
                if player in all_known:
                    pearls[player] += 1
                continue

            # Food
            mc = RE_CONSUME.match(body)
            if mc and mc.group(1) in all_known:
                food[mc.group(1)][mc.group(2)] += 1
                continue

            # Hit / crit
            mh = RE_HIT.match(body)
            if mh:
                attacker  = mh.group(1)
                hit_type  = mh.group(2)   # 'hit' or 'crit'
                victim    = mh.group(3)
                dmg       = float(mh.group(4))
                att_team  = _team(attacker)
                vic_team  = _team(victim)
                if attacker in all_known:
                    total_hits[attacker] += 1
                    if hit_type == 'crit':
                        crits[attacker] += 1
                    # Only count cross-team damage for team stats
                    if att_team and vic_team and att_team != vic_team:
                        dmg_dealt[attacker] += dmg
                if victim in all_known and attacker in all_known:
                    victim_attackers[victim].append(attacker)
                continue

            # Took damage (all sources → victim's perspective)
            mt2 = RE_TOOK.match(body)
            if mt2 and mt2.group(1) in all_known:
                dmg_taken[mt2.group(1)] += float(mt2.group(2))
                continue

            # Kill lines
            mk = RE_KILL.match(body)
            if mk:
                killer   = mk.group(1)
                is_team  = mk.group(2) is not None
                victim   = mk.group(3)
                has_coord = mk.group(4) is not None

                if not has_coord or victim not in all_known:
                    victim_attackers.pop(victim, None)
                    continue

                deaths[victim] += 1

                if not is_team and killer in all_known:
                    kills[killer] += 1
                    # Assists: same team as killer, not killer themselves
                    killer_team = _team(killer)
                    seen = set()
                    for hitter in victim_attackers.get(victim, []):
                        if hitter != killer and hitter not in seen and _team(hitter) == killer_team:
                            assists[hitter] += 1
                            seen.add(hitter)

                victim_attackers.pop(victim, None)
                continue

            # Blocks / shulkers broken
            mb = RE_BROKE.match(body)
            if mb and mb.group(1) in all_known:
                block = mb.group(2)
                if 'SHULKER' in block.upper() or 'Shulker' in block:
                    shulkers_broken[mb.group(1)] += 1
                else:
                    blocks_broken[mb.group(1)] += 1
                continue

            # Shulkers placed
            mpl = RE_PLACED.match(body)
            if mpl and mpl.group(1) in all_known:
                if 'Shulker' in mpl.group(2) or 'SHULKER' in mpl.group(2):
                    shulkers_placed[mpl.group(1)] += 1
                continue

            # Items dropped
            if RE_DROPPED.match(body):
                player = body.split()[0]
                if player in all_known:
                    items_dropped[player] += 1
                continue

            # Golem kills ("got charged")
            if 'got charged' in body:
                mg = RE_GOT_CHARGED.match(body)
                if mg and mg.group(1) in all_known:
                    golem_kills[mg.group(1)] += 1
                continue

            # Charges taken ("overloaded a Power Source")
            mo = RE_OVERLOAD.match(body)
            if mo and mo.group(1) in all_known:
                charges_taken[mo.group(1)] += 1
                continue

            # Charge participation ("began an overload")
            mbo = RE_BEGAN_OVL.match(body)
            if mbo and mbo.group(1) in all_known:
                charge_part[mbo.group(1)] += 1
                continue

            # Ancient ingots
            if 'Ancient Ingot' in body and 'repaired' in body:
                ma = RE_ANCIENT.match(body)
                if ma and ma.group(1) in all_known:
                    ancient_ingots[ma.group(1)] += 1
                continue

    # ── Build player dicts ────────────────────────────────────────────────────
    def _make_player(name, town):
        hits = total_hits[name]
        c    = crits[name]
        crit_ratio = round((c / hits * 100)) if hits else 0
        return {
            'name':             name,
            'town':             town,
            'kills':            kills[name],
            'deaths':           deaths[name],
            'assists':          assists[name],
            'pearls':           pearls[name],
            'damage_dealt':     round(dmg_dealt[name], 1),
            'damage_taken':     round(dmg_taken[name], 1),
            'total_hits':       hits,
            'crits':            c,
            'crit_ratio':       crit_ratio,
            'shulkers_broken':  shulkers_broken[name],
            'shulkers_placed':  shulkers_placed[name],
            'blocks_broken':    blocks_broken[name],
            'items_dropped':    items_dropped[name],
            'golem_kills':      golem_kills[name],
            'charges_taken':    charges_taken[name],
            'charge_part':      charge_part[name],
            'ancient_ingots':   ancient_ingots[name],
            'potions':          dict(sorted(potions[name].items(), key=lambda x: -x[1])),
            'food':             dict(sorted(food[name].items(),    key=lambda x: -x[1])),
        }

    att_players = sorted(
        [_make_player(n, t) for n, t in attacker_map.items()],
        key=lambda p: (-p['kills'], -p['damage_dealt'])
    )
    def_players = sorted(
        [_make_player(n, t) for n, t in defender_map.items()],
        key=lambda p: (-p['kills'], -p['damage_dealt'])
    )

    def _team_totals(players):
        tot = lambda k: sum(p[k] for p in players)
        pot_totals = defaultdict(int)
        for p in players:
            for ptype, cnt in p['potions'].items():
                pot_totals[ptype] += cnt
        food_total = sum(sum(p['food'].values()) for p in players)
        return {
            'kills':          tot('kills'),
            'damage':         round(sum(p['damage_dealt'] for p in players), 1),
            'pearls':         tot('pearls'),
            'food':           food_total,
            'golem_kills':    tot('golem_kills'),
            'charges':        tot('charges_taken'),
            'charge_part':    tot('charge_part'),
            'potions':        dict(sorted(pot_totals.items(), key=lambda x: -x[1])),
            'total_potions':  sum(pot_totals.values()),
        }

    return {
        'filename':       filename,
        'location':       location,
        'winner':         winner,
        'duration':       duration,
        'mutator':        mutator,
        'attackers':      att_players,
        'defenders':      def_players,
        'attacker_kills': sum(p['kills'] for p in att_players),
        'defender_kills': sum(p['kills'] for p in def_players),
        'attacker_totals': _team_totals(att_players),
        'defender_totals': _team_totals(def_players),
    }


def list_fights() -> list:
    results = []
    if not os.path.isdir(FIGHTLOGS_DIR):
        return results

    for path in sorted(Path(FIGHTLOGS_DIR).glob('*.txt'),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        fname  = path.name
        mtime  = path.stat().st_mtime
        ckey   = f'{fname}:{mtime}'

        if ckey not in _PARSE_CACHE:
            # Evict stale entries for this filename
            for k in [k for k in _PARSE_CACHE if k.startswith(f'{fname}:')]:
                del _PARSE_CACHE[k]
            try:
                _PARSE_CACHE[ckey] = parse_fight_log(str(path))
            except Exception as e:
                print(f'[fights_parser] error parsing {fname}: {e}')
                continue

        parsed = _PARSE_CACHE[ckey]
        results.append({
            'filename':       fname,
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
