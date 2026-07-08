#!/usr/bin/env python3
"""Build a synthetic sample SQLite database (plus a WAL) for demoing recover.py.

Creates a generic 7-column `records` table, fills it with ~15 fake rows of
lorem-style text, forces at least one row large enough to spill onto an
overflow page, enables WAL journaling, and intentionally leaves the last few
rows only in the WAL (no checkpoint) so the WAL-recovery path is exercised.

No real or personal data is used — everything is randomly generated.
"""
import argparse
import os
import random
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone

TYPES = ['note', 'task', 'event', 'log']

WORDS = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua enim ad minim veniam "
    "quis nostrud exercitation ullamco laboris nisi aliquip ex ea commodo "
    "consequat duis aute irure reprehenderit voluptate velit esse cillum "
    "fugiat nulla pariatur excepteur sint occaecat cupidatat non proident"
).split()


def lorem(n_words, rng):
    return ' '.join(rng.choice(WORDS) for _ in range(n_words))


def iso(dt):
    return dt.isoformat()


def main():
    ap = argparse.ArgumentParser(description="Generate a synthetic sample SQLite DB + WAL for the recovery demo.")
    ap.add_argument('--out', default='sample.db', help="Output DB path (default: sample.db).")
    ap.add_argument('--rows', type=int, default=15, help="Total rows to insert (default: 15).")
    ap.add_argument('--wal-rows', type=int, default=4,
                    help="How many trailing rows to leave only in the WAL, uncheckpointed (default: 4).")
    ap.add_argument('--seed', type=int, default=42, help="RNG seed for reproducible data (default: 42).")
    args = ap.parse_args()

    rng = random.Random(args.seed)

    out = os.path.abspath(args.out)
    # Start clean so re-runs are deterministic.
    for ext in ('', '-wal', '-shm'):
        try:
            os.remove(out + ext)
        except FileNotFoundError:
            pass

    conn = sqlite3.connect(out)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA page_size=4096;")
    conn.execute("PRAGMA wal_autocheckpoint=0;")  # never auto-checkpoint
    conn.execute("""
        CREATE TABLE records (
            id         TEXT PRIMARY KEY,
            title      TEXT,
            content    TEXT,
            type       TEXT,
            tags       TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    total = args.rows
    wal_rows = min(args.wal_rows, total)
    main_rows = total - wal_rows
    overflow_idx = rng.randrange(main_rows) if main_rows else 0

    def make_row(i):
        created = base + timedelta(days=i, hours=rng.randint(0, 23))
        updated = created + timedelta(hours=rng.randint(1, 72))
        # One row gets an oversized content field (~5-8 KB) to force an
        # overflow-page chain (a 4096-byte page holds far less than that).
        if i == overflow_idx:
            n_words = rng.randint(900, 1300)  # ~6-8 KB of text
        else:
            n_words = rng.randint(8, 40)
        return (
            str(uuid.uuid4()),
            lorem(rng.randint(3, 8), rng).title(),
            lorem(n_words, rng),
            rng.choice(TYPES),
            ','.join(rng.sample(['alpha', 'beta', 'gamma', 'delta', 'demo', 'sample'], rng.randint(1, 3))),
            iso(created),
            iso(updated),
        )

    ins = "INSERT INTO records (id,title,content,type,tags,created_at,updated_at) VALUES (?,?,?,?,?,?,?)"

    # First batch: committed and checkpointed into the main DB file.
    for i in range(main_rows):
        conn.execute(ins, make_row(i))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")  # flush these into the .db

    # Second batch: committed but left ONLY in the WAL, so recover.py must read
    # them out of the -wal side file. The commit fsyncs these frames to the WAL.
    for i in range(main_rows, total):
        conn.execute(ins, make_row(i))
    conn.commit()

    # NOTE: we deliberately do NOT call conn.close(). Closing the last SQLite
    # connection runs a checkpoint that would fold the WAL frames back into the
    # main .db file, defeating the WAL-recovery demo. Terminating the process
    # with os._exit() skips SQLite's close-time checkpoint, leaving the last
    # rows present ONLY in the -wal file.
    wal = out + '-wal'
    wal_note = ('present' if os.path.exists(wal) and os.path.getsize(wal) > 0
                else 'MISSING')
    print(f"sample DB written: {out}")
    print(f"  rows total:            {total}")
    print(f"  rows in main file:     {main_rows} (checkpointed)")
    print(f"  rows only in WAL:      {wal_rows}")
    print(f"  oversized/overflow row index: {overflow_idx}")
    print(f"  WAL file: {wal} ({wal_note})")
    print()
    print("Next: python3 recover.py", os.path.basename(out))
    sys.stdout.flush()
    os._exit(0)


if __name__ == '__main__':
    main()
