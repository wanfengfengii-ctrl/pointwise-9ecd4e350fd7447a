"""One shot, atomic upgrade from format 1 to format 2.

The maintenance window guarantees that no other writer is active. The whole
switch (encoding every state row, creating entries2, copying the raw row
text, receipts and generation untouched, dropping entries, setting
meta.format=2) happens inside a single ``BEGIN IMMEDIATE`` transaction, so a
process interruption leaves either the complete format 1 database or the
complete format 2 database. No custom collation is registered: the legacy
table is only scanned, never ordered or compared through KEY1.
"""
import json
import sqlite3

import keycodec
import storage2


def _format(c):
    try:
        row = c.execute('SELECT format FROM meta').fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def upgrade(path):
    """Upgrade ``path`` in place and return {"format": 2, "rows": N}."""
    c = sqlite3.connect(path, isolation_level=None)
    try:
        c.execute('BEGIN IMMEDIATE')
        try:
            fmt = _format(c)
            if fmt == 2:
                rows = c.execute('SELECT COUNT(*) FROM entries2').fetchone()[0]
                c.execute('COMMIT')
                return {'format': 2, 'rows': rows}
            if fmt != 1:
                raise ValueError('FORMAT')
            state = c.execute(
                'SELECT raw,rev,value,deleted FROM entries').fetchall()
            # Encode everything and validate the destination schema before
            # touching the database in any durable way.
            encoded = [(keycodec.pack(json.loads(raw)), raw, rev, value, deleted)
                       for raw, rev, value, deleted in state]
            storage2.create(c)
            for k, raw, rev, value, deleted in encoded:
                storage2.put(c, k, raw, rev, value, deleted)
            c.execute('DROP TABLE entries')
            c.execute('UPDATE meta SET format=2')
            c.execute('COMMIT')
            return {'format': 2, 'rows': len(encoded)}
        except BaseException:
            if c.in_transaction:
                c.execute('ROLLBACK')
            raise
    finally:
        c.close()
