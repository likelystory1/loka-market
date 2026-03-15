"""
eb_parse_txts.py — Parse existing fightlogs/*.txt files into JSON.

Globs all .txt files in fightlogs/, skips any that already have a
corresponding .json in fightlogs/parsed/, and parses the rest using
fights_parser.parse_fight_log().

Run from the backend/ directory:
    python3 eb_parse_txts.py          # skip already-parsed
    python3 eb_parse_txts.py --force  # re-parse all (overwrites old JSONs)
"""

import os, json, glob, argparse
from fights_parser import parse_fight_log
from eb_fight_scraper import PARSED_DIR, FIGHTLOGS_DIR

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true', help='Re-parse even if JSON already exists')
    args = ap.parse_args()

    txt_files = sorted(glob.glob(os.path.join(FIGHTLOGS_DIR, '*.txt')))
    print(f'[parse] found {len(txt_files)} .txt files in fightlogs/')

    parsed = skipped = errors = 0

    for txt_path in txt_files:
        stem = os.path.splitext(os.path.basename(txt_path))[0]
        json_path = os.path.join(PARSED_DIR, stem + '.json')

        if os.path.exists(json_path) and not args.force:
            skipped += 1
            continue

        try:
            result = parse_fight_log(txt_path)
        except Exception as e:
            print(f'[parse] ERR {stem}: {e}')
            errors += 1
            continue

        try:
            tmp = json_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(result, f, separators=(',', ':'))
            os.replace(tmp, json_path)
        except Exception as e:
            print(f'[parse] write ERR {stem}: {e}')
            errors += 1
            continue

        atk     = result.get('attacker_town', '?')
        dfn     = result.get('defender_town', '?')
        loc     = result.get('location', '?')
        winner  = result.get('winner', '?')
        total_p = len(result.get('attackers', [])) + len(result.get('defenders', []))
        print(f'[parse] +{stem} | {atk} vs {dfn} | {loc} | {total_p}p | winner: {winner}')
        parsed += 1

    print(f'\n[parse] done — {parsed} parsed  {skipped} skipped  {errors} errors')

if __name__ == '__main__':
    main()
