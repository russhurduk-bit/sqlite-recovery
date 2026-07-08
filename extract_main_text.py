#!/usr/bin/env python3
"""Salvage text fields from a DB file whose binary structure was mangled.

Sometimes a database file gets corrupted by a text-mode round-trip (for
example, being written through a UTF-8 codec), which turns non-UTF-8 header
bytes into the replacement character U+FFFD while leaving the human-readable
TEXT columns intact. When the B-tree structure is too damaged for the
cell-level parser in recover.py, this script falls back to a purely textual
recovery:

  1. Decode the whole file as UTF-8 (lossy).
  2. Split into per-record regions on UUID boundaries.
  3. From each region's tail, pull the two trailing ISO-8601 timestamps
     (created_at, updated_at) and the trailing type/tags.
  4. Everything before that is the title+content blob (best-effort).

This is deliberately heuristic and complements recover.py; use it only when
the structured parse cannot reach some records.
"""
import argparse
import json
import re
import sys

UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
ISO = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2})?')
REPL = '�'  # Unicode replacement character left by the lossy decode.


def extract(txt, enum):
    """Return {uuid: record_dict} recovered from the lossy-decoded text."""
    ms = list(UUID.finditer(txt))
    recs = {}
    for i, m in enumerate(ms):
        u = m.group(0)
        start = m.end()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(txt)
        region = txt[start:end]
        # The two trailing ISO timestamps are created_at then updated_at.
        isos = list(ISO.finditer(region))
        if len(isos) < 2:
            continue
        ca_m, ua_m = isos[-2], isos[-1]
        ca = ca_m.group(0)
        ua = ua_m.group(0)
        head = region[:ca_m.start()]  # title + content + type + tags

        # Find the last enum word in the head: everything after it is 'tags',
        # everything before it is title+content.
        typ = None
        tags = ''
        body = head
        best = -1
        bt = None
        for e in enum:
            idx = head.rfind(e)
            if idx > best:
                cand = head[idx + len(e):]
                if REPL not in cand and len(cand) < 300:
                    best = idx
                    bt = e
        if bt is not None:
            typ = bt
            tags = head[best + len(bt):]
            body = head[:best]
        elif enum:
            typ = next(iter(enum))
        # Clean trailing replacement chars from the mangled next-record header.
        body = body.rstrip(REPL)
        recs[u] = {
            'id': u,
            'title_content': body,
            'type': typ,
            'tags': tags,
            'created_at': ca,
            'updated_at': ua,
        }
    return recs


def main():
    ap = argparse.ArgumentParser(
        description="Salvage text fields from a UTF-8-mangled DB file by "
                    "splitting on UUIDs and recovering trailing metadata.")
    ap.add_argument('db', help="Path to the mangled database file.")
    ap.add_argument('--out', default='main_only.json',
                    help="Output JSON path (default: main_only.json).")
    ap.add_argument('--known', default=None,
                    help="Optional path to a recover.py output JSON. Records "
                         "whose id already appears there are skipped, so the "
                         "output holds only text-recovered rows not seen by "
                         "the structured parse.")
    ap.add_argument('--types', default='note,task,event,log',
                    help="Comma-separated allowed values for the 'type' column "
                         "(default: note,task,event,log).")
    args = ap.parse_args()

    enum = [t for t in (t.strip() for t in args.types.split(',')) if t]

    with open(args.db, 'rb') as f:
        data = f.read()
    txt = data.decode('utf-8', 'replace')

    recs = extract(txt, enum)
    print('regions parsed:', len(recs))

    known = set()
    if args.known:
        with open(args.known, encoding='utf-8') as f:
            prev = json.load(f)
        known = {r[0] for r in prev}

    only = {k: v for k, v in recs.items() if k not in known}
    label = 'text-only ids (not in structured recovery)' if args.known else 'ids'
    print(f'{label}: {len(only)}')

    print('\n=== recovered records (updated_at | type | first 90 chars) ===')
    out = []
    for k, v in sorted(only.items(), key=lambda x: x[1]['updated_at']):
        tc = v['title_content'].replace(chr(10), ' ')
        print(v['updated_at'][:10], (v['type'] or '')[:4], '|', tc[:90])
        out.append(v)

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    print(f'\nsaved -> {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
