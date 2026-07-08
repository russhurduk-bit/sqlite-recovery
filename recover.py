#!/usr/bin/env python3
"""Recover rows from a (possibly corrupted) SQLite database by parsing
table-leaf B-tree cells directly, without relying on the SQLite engine.

The tool scans the main database file, any WAL side files, and any number of
backup copies. It decodes each record with a hand-written varint parser,
follows overflow-page chains for large payloads, and deduplicates results by
`id`, keeping the row with the most recent `updated_at`.

It assumes a generic 7-column "records" table with the shape:

    id         TEXT   -- a UUID (used as the dedup key)
    title      TEXT
    content    TEXT
    type       TEXT   -- one of a small enum, see --types
    tags       TEXT
    created_at TEXT   -- ISO-8601 timestamp
    updated_at TEXT   -- ISO-8601 timestamp

The record shape and type-enum are configurable via CLI flags so the same
recovery core works for any similarly-shaped table.
"""
import argparse
import glob
import json
import re
import struct
import sys

# Default page size; overridden by reading the DB header (bytes 16-17) when
# a valid header is present. SQLite page sizes are powers of two, 512..65536.
DEFAULT_PAGE_SIZE = 4096

UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)


def read_page_size(data):
    """Return the page size from the SQLite header if the magic string is
    present, otherwise fall back to DEFAULT_PAGE_SIZE.

    Header layout: bytes 0-15 are the magic "SQLite format 3\\000",
    bytes 16-17 are the page size as a big-endian u16 (with 1 meaning 65536)."""
    if len(data) >= 18 and data[:16] == b'SQLite format 3\x00':
        ps = struct.unpack('>H', data[16:18])[0]
        if ps == 1:
            return 65536
        if ps >= 512 and (ps & (ps - 1)) == 0:
            return ps
    return DEFAULT_PAGE_SIZE


def varint(buf, off):
    """Decode a SQLite varint (1-9 bytes, big-endian, 7 bits per byte).
    The 9th byte, if present, contributes a full 8 bits.
    Returns (value, next_offset)."""
    val = 0
    for i in range(9):
        b = buf[off + i]
        if i == 8:
            val = (val << 8) | b
            return val, off + 9
        val = (val << 7) | (b & 0x7f)
        if not (b & 0x80):
            return val, off + i + 1
    return val, off + 9


def read_pages(data, ps):
    """Yield (pageno, pagebytes) for a raw DB file image. Pages are 1-indexed."""
    n = len(data) // ps
    for p in range(n):
        yield p + 1, data[p * ps:(p + 1) * ps]


def page_map(data, ps):
    return {p: b for p, b in read_pages(data, ps)}


def parse_overflow(pages, first, need):
    """Follow an overflow-page chain and return the collected bytes
    (best-effort). Each overflow page begins with a 4-byte big-endian pointer
    to the next page in the chain (0 terminates it), followed by content."""
    out = b''
    pg = first
    seen = set()
    while pg and pg not in seen and len(out) < need:
        seen.add(pg)
        page = pages.get(pg)
        if not page:
            break
        nxt = struct.unpack('>I', page[0:4])[0]
        out += page[4:]
        pg = nxt
    return out[:need]


def parse_cell(page, cell_off, pages, ps):
    """Parse one table-leaf cell starting at cell_off within the page bytes.
    Returns the list of column values, or None on failure.

    A table-leaf cell is: payload_len (varint), rowid (varint), then the
    record payload. When the payload exceeds what fits locally, the tail
    spills into an overflow chain whose first page number is a 4-byte pointer
    right after the local bytes."""
    try:
        payload_len, off = varint(page, cell_off)
        rowid, off = varint(page, off)
        # Usable page size (no reserved region assumed).
        U = ps
        # Maximum local payload for a table-leaf page.
        X = U - 35
        if payload_len <= X:
            payload = page[off:off + payload_len]
        else:
            # Spilled payload: compute how many bytes stay on this page.
            M = ((U - 12) * 32 // 255) - 23
            K = M + (payload_len - M) % (U - 4)
            local = K if K <= X else M
            local_bytes = page[off:off + local]
            ovf_ptr = struct.unpack('>I', page[off + local:off + local + 4])[0]
            rest = parse_overflow(pages, ovf_ptr, payload_len - local)
            payload = local_bytes + rest
        if len(payload) < 5:
            return None
        # Record header: header length (varint) followed by one serial-type
        # varint per column.
        hlen, ho = varint(payload, 0)
        serials = []
        po = ho
        while po < hlen:
            s, po = varint(payload, po)
            serials.append(s)
        # Record body: decode each column by its serial type.
        bo = hlen
        vals = []
        for s in serials:
            if s == 0:
                vals.append(None)
            elif s == 1:
                vals.append(int.from_bytes(payload[bo:bo + 1], 'big', signed=True)); bo += 1
            elif s == 2:
                vals.append(int.from_bytes(payload[bo:bo + 2], 'big', signed=True)); bo += 2
            elif s == 3:
                vals.append(int.from_bytes(payload[bo:bo + 3], 'big', signed=True)); bo += 3
            elif s == 4:
                vals.append(int.from_bytes(payload[bo:bo + 4], 'big', signed=True)); bo += 4
            elif s == 5:
                vals.append(int.from_bytes(payload[bo:bo + 6], 'big', signed=True)); bo += 6
            elif s == 6:
                vals.append(int.from_bytes(payload[bo:bo + 8], 'big', signed=True)); bo += 8
            elif s == 7:
                vals.append(struct.unpack('>d', payload[bo:bo + 8])[0]); bo += 8
            elif s == 8:
                vals.append(0)  # integer constant 0
            elif s == 9:
                vals.append(1)  # integer constant 1
            elif s >= 12 and s % 2 == 0:
                ln = (s - 12) // 2  # BLOB
                vals.append(payload[bo:bo + ln]); bo += ln
            elif s >= 13:
                ln = (s - 13) // 2  # TEXT
                vals.append(payload[bo:bo + ln].decode('utf-8', 'replace')); bo += ln
            else:
                vals.append(None)
        return vals
    except Exception:
        return None


def harvest(pages, ps, enum):
    """Scan all pages, decode every plausible records-table row, and keep the
    latest version per id (by updated_at). Rows are validated by shape: a UUID
    id, a known type, and string title/content."""
    rows = {}
    for pgno, page in pages.items():
        if not page:
            continue
        # Page 1 carries the 100-byte database header before the b-tree page.
        base = 100 if pgno == 1 else 0
        if len(page) <= base:
            continue
        ptype = page[base]
        if ptype != 0x0D:  # 0x0D == table-leaf page
            continue
        try:
            ncell = struct.unpack('>H', page[base + 3:base + 5])[0]
        except Exception:
            continue
        if ncell == 0 or ncell > 100000:
            continue
        cp_start = base + 8  # cell-pointer array follows the 8-byte leaf header
        for c in range(ncell):
            try:
                cell_ptr = struct.unpack('>H', page[cp_start + c * 2:cp_start + c * 2 + 2])[0]
            except Exception:
                continue
            if cell_ptr < base or cell_ptr >= ps:
                continue
            vals = parse_cell(page, cell_ptr, pages, ps)
            if not vals or len(vals) < 7:
                continue
            id_, title, content, typ, tags, ca, ua = vals[0:7]
            if not isinstance(id_, str) or not UUID_RE.match(id_):
                continue
            if enum and typ not in enum:
                continue
            if not isinstance(title, str) or not isinstance(content, str):
                continue
            ua_s = ua if isinstance(ua, str) else ''
            prev = rows.get(id_)
            if prev is None or ua_s > (prev[6] or ''):
                rows[id_] = [id_, title, content, typ, tags, ca, ua]
    return rows


def load_wal_pages(path, ps):
    """Return {pageno: pagebytes} from a WAL file, keeping the LATEST frame per
    page. A WAL file is a 32-byte header followed by frames; each frame is a
    24-byte frame header plus one page image. Later frames for the same page
    supersede earlier ones."""
    with open(path, 'rb') as f:
        data = f.read()
    if len(data) < 32:
        return {}
    pages = {}
    off = 32
    while off + 24 + ps <= len(data):
        pgno = struct.unpack('>I', data[off:off + 4])[0]
        page = data[off + 24:off + 24 + ps]
        pages[pgno] = page  # later frames overwrite -> latest version wins
        off += 24 + ps
    return pages


def main():
    ap = argparse.ArgumentParser(
        description="Recover rows from a corrupted SQLite DB by parsing "
                    "table-leaf cells directly.")
    ap.add_argument('db', help="Path to the (possibly corrupted) SQLite database file.")
    ap.add_argument('--wal', action='append', default=[],
                    help="Path to a WAL file to scan. May be repeated. "
                         "If omitted, '<db>-wal' is used when present.")
    ap.add_argument('--backup', action='append', default=[],
                    help="Path (or glob) to a backup DB copy to also scan. "
                         "May be repeated.")
    ap.add_argument('--out', default='recovered_rows.json',
                    help="Output JSON path (default: recovered_rows.json).")
    ap.add_argument('--types', default='note,task,event,log',
                    help="Comma-separated allowed values for the 'type' column "
                         "(default: note,task,event,log). Pass an empty string "
                         "to accept any type.")
    ap.add_argument('--page-size', type=int, default=0,
                    help="Force a page size instead of reading it from the "
                         "header (0 = auto-detect, then fall back to %d)."
                         % DEFAULT_PAGE_SIZE)
    args = ap.parse_args()

    enum = {t for t in (t.strip() for t in args.types.split(',')) if t}

    all_rows = {}

    def merge(rows, src):
        for k, v in rows.items():
            prev = all_rows.get(k)
            if prev is None or (v[6] or '') >= (prev[6] or ''):
                all_rows[k] = v
        print(f'  {src}: {len(rows)} rows (total unique {len(all_rows)})')

    def resolve_ps(data):
        return args.page_size if args.page_size > 0 else read_page_size(data)

    # Main database file.
    print(f'=== main db: {args.db} ===')
    try:
        with open(args.db, 'rb') as f:
            main_data = f.read()
        ps = resolve_ps(main_data)
        print(f'  page size: {ps}')
        merge(harvest(page_map(main_data, ps), ps, enum), 'main')
    except FileNotFoundError:
        print(f'  ERROR: {args.db} not found', file=sys.stderr)
        return 1

    # WAL files: explicit --wal args, or the conventional '<db>-wal' sidecar.
    wal_paths = list(args.wal)
    if not wal_paths:
        default_wal = args.db + '-wal'
        try:
            open(default_wal, 'rb').close()
            wal_paths = [default_wal]
        except OSError:
            pass
    for wal in wal_paths:
        print(f'=== WAL (latest frame per page): {wal} ===')
        try:
            merge(harvest(load_wal_pages(wal, ps), ps, enum), 'wal')
        except OSError as e:
            print(f'  WAL error: {e}')

    # Backup copies (each may be a glob).
    for pattern in args.backup:
        for path in sorted(glob.glob(pattern)) or [pattern]:
            print(f'=== backup: {path} ===')
            try:
                with open(path, 'rb') as f:
                    bdata = f.read()
                bps = resolve_ps(bdata)
                merge(harvest(page_map(bdata, bps), bps, enum), 'backup')
            except OSError as e:
                print(f'  backup error: {e}')

    print(f'\nTOTAL recovered unique rows: {len(all_rows)}')
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(list(all_rows.values()), f, ensure_ascii=False)
    print(f'saved -> {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
