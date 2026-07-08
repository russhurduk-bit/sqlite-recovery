# SQLite Record Recovery

Low-level recovery of rows from a **corrupted or unopenable SQLite database** —
by parsing the file's B-tree structure by hand instead of relying on the SQLite
engine.

When a SQLite file is damaged (a truncated write, a mangled header, a
half-applied WAL, an interrupted `VACUUM`), `sqlite3` may refuse to open it or
return `database disk image is malformed`. But the raw row data is usually still
sitting in the file's pages. This tool walks those pages directly and pulls the
records back out.

It targets a generic 7-column `records` table:

| column       | type | notes                                   |
|--------------|------|-----------------------------------------|
| `id`         | TEXT | UUID; used as the deduplication key     |
| `title`      | TEXT |                                         |
| `content`    | TEXT | may be large enough to spill to overflow|
| `type`       | TEXT | one of a small enum (configurable)      |
| `tags`       | TEXT |                                         |
| `created_at` | TEXT | ISO-8601 timestamp                      |
| `updated_at` | TEXT | ISO-8601 timestamp; newest wins on dedup|

The table shape and the allowed `type` values are configurable via CLI flags
(`--types`), so the same recovery core works for any similarly shaped table.
The default type enum is `note,task,event,log`.

## Technique

The recovery relies only on the [SQLite file format](https://www.sqlite.org/fileformat.html)
and the Python standard library — no third-party dependencies, no SQLite engine.

1. **Page size detection** — read bytes 16–17 of the database header to get the
   page size (falling back to 4096 when the header is unreadable).
2. **Manual varint decoding** — SQLite stores lengths, row IDs, and serial
   types as 1–9 byte big-endian varints (7 bits per byte). A hand-written
   decoder reads them.
3. **Table-leaf cell parsing** — for every table-leaf page (`0x0D`), walk the
   cell-pointer array, and for each cell decode `payload_len`, `rowid`, the
   record header (serial types), and the record body (one value per column,
   decoded by serial type).
4. **Overflow-chain following** — when a record's payload is too large to fit on
   one page, the tail spills into a linked list of overflow pages. Each overflow
   page starts with a 4-byte pointer to the next; the chain is followed until
   the full payload is reassembled.
5. **WAL frame scanning** — the write-ahead log (`-wal`) holds page images that
   may be newer than the main file. All frames are scanned and the **latest
   frame per page** wins, so uncommitted-to-main but committed-to-WAL rows are
   recovered.
6. **Multi-source merge + dedup** — the main file, any WAL side files, and any
   number of backup copies are all scanned, then rows are deduplicated by `id`,
   keeping the one with the most recent `updated_at`.

`extract_main_text.py` is a text-level fallback for the harder case where the
file's binary structure is too damaged for structured parsing (for example, a
file that was corrupted by a UTF-8 text round-trip). It splits the lossy-decoded
bytes on UUID boundaries and recovers the trailing metadata heuristically.

## Files

| file                    | purpose                                                        |
|-------------------------|----------------------------------------------------------------|
| `recover.py`            | Structured recovery: varint + leaf cells + overflow + WAL merge|
| `extract_main_text.py`  | Text-level fallback for structurally-mangled files             |
| `make_sample_db.py`     | Generates a synthetic sample DB + WAL to run the demo          |
| `.env.example`          | No secrets needed; kept for convention                         |

## Quick Start

Python 3.8+ and the standard library only — nothing to install.

```bash
# 1. Build a synthetic sample database (includes an overflow row and
#    rows left only in the WAL).
python3 make_sample_db.py
# -> writes sample.db and sample.db-wal

# 2. Recover rows from it (the -wal sidecar is picked up automatically).
python3 recover.py sample.db
# -> prints per-source counts and writes recovered_rows.json
```

Recover across the main file plus explicit WAL and backup copies:

```bash
python3 recover.py sample.db \
    --wal sample.db-wal \
    --backup 'backups/*.db' \
    --out recovered_rows.json \
    --types note,task,event,log
```

Text-level fallback (only when the structured parse can't reach some records):

```bash
python3 extract_main_text.py sample.db --known recovered_rows.json --out main_only.json
```

## Notes and limits

- This is a **best-effort forensic tool**. Recovery quality depends on how much
  of the file survived; badly overwritten pages can't be reconstructed.
- It reads a specific table shape. Adapting to a different schema means adjusting
  the column validation in `harvest()`.
- It never writes to the source files — output goes only to the `--out` JSON.
