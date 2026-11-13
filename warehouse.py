"""Business entry point. Keeps the legacy Store API for both formats.

Format details, encoding rules and the upgrade procedure are described in
NOTES.md. This module imports the shipped reference implementation rather
than copying it.
"""
import copy
import json
import sqlite3

import keycodec
import legacy
import storage2


def _format(c):
    try:
        row = c.execute('SELECT format FROM meta').fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _create(c, fmt):
    for statement in (
            'CREATE TABLE IF NOT EXISTS meta(format INTEGER NOT NULL,'
            ' generation INTEGER NOT NULL)',
            'CREATE TABLE IF NOT EXISTS receipts(request TEXT PRIMARY KEY,'
            ' payload TEXT NOT NULL,result TEXT NOT NULL)'):
        c.execute(statement)
    if fmt == 1:
        c.execute('CREATE TABLE IF NOT EXISTS entries('
                  'raw TEXT COLLATE KEY1 PRIMARY KEY, rev INTEGER NOT NULL,'
                  ' value TEXT NOT NULL, deleted INTEGER NOT NULL)')
    else:
        storage2.create(c)
    if not c.execute('SELECT 1 FROM meta').fetchone():
        c.execute('INSERT INTO meta VALUES(?,0)', (fmt,))


def _new_format():
    """A database created through the business entry point starts at 2."""
    return 2


class Store:
    def __init__(self, path):
        self.c = sqlite3.connect(path, isolation_level=None)
        fmt = _format(self.c)
        if fmt is None:
            fmt = _new_format()
            self.c.execute('BEGIN IMMEDIATE')
            try:
                if fmt == 1:
                    self.c.create_collation('KEY1', legacy.collation)
                _create(self.c, fmt)
                self.c.execute('COMMIT')
            except BaseException:
                if self.c.in_transaction:
                    self.c.execute('ROLLBACK')
                self.c.close()
                raise
        elif fmt not in (1, 2):
            self.c.close()
            raise ValueError('FORMAT')
        self.format = fmt
        if fmt == 1:
            # Only format 1 needs the legacy collation; format 2 never
            # registers or calls a custom SQLite comparator.
            self.c.create_collation('KEY1', legacy.collation)

    def close(self):
        self.c.close()

    # -- writes -----------------------------------------------------------

    def _apply_v1(self, rows, payload):
        accepted = 0
        for row in rows:
            raw = legacy.dump(row['key'])
            old = self.c.execute(
                'SELECT raw,rev,value,deleted FROM entries WHERE raw=?',
                (raw,)).fetchone()
            if old and row['rev'] < old[1]:
                continue
            value = legacy.dump(row['value'])
            if old and row['rev'] == old[1]:
                if value != old[2] or int(row['deleted']) != old[3]:
                    raise ValueError('REVISION_CONFLICT')
                continue
            if old:
                self.c.execute('DELETE FROM entries WHERE raw=?', (raw,))
            self.c.execute('INSERT INTO entries VALUES(?,?,?,?)',
                           (raw, row['rev'], value, int(row['deleted'])))
            accepted += 1
        return accepted

    def _apply_v2(self, rows, payload):
        accepted = 0
        for row in rows:
            k = keycodec.pack(row['key'])
            raw = legacy.dump(row['key'])
            old = storage2.get(self.c, k)
            if old and row['rev'] < old[1]:
                continue
            value = legacy.dump(row['value'])
            if old and row['rev'] == old[1]:
                if value != old[2] or int(row['deleted']) != old[3]:
                    raise ValueError('REVISION_CONFLICT')
                continue
            storage2.put(self.c, k, raw, row['rev'], value,
                         int(row['deleted']))
            accepted += 1
        return accepted

    def apply(self, request, rows):
        rows, payload = legacy.prepare(request, rows)
        self.c.execute('BEGIN IMMEDIATE')
        try:
            replay = legacy.receipt(self.c, request, payload)
            if replay is not None:
                self.c.execute('COMMIT')
                return replay
            if self.format == 1:
                accepted = self._apply_v1(rows, payload)
            else:
                accepted = self._apply_v2(rows, payload)
            result = legacy.record(self.c, request, payload, accepted)
            self.c.execute('COMMIT')
            return result
        except BaseException:
            if self.c.in_transaction:
                self.c.execute('ROLLBACK')
            raise

    # -- reads ------------------------------------------------------------

    def snapshot(self):
        self.c.execute('BEGIN')
        try:
            generation = self.c.execute(
                'SELECT generation FROM meta').fetchone()[0]
            if self.format == 1:
                source = self.c.execute(
                    'SELECT raw,rev,value,deleted FROM entries '
                    'ORDER BY raw COLLATE KEY1')
            else:
                source = storage2.all_rows(self.c)
            rows = [dict(key=json.loads(raw), rev=rev, value=json.loads(value),
                         deleted=bool(deleted))
                    for raw, rev, value, deleted in source]
            receipts = [list(r) for r in self.c.execute(
                'SELECT request,payload,result FROM receipts ORDER BY request')]
            self.c.execute('COMMIT')
            return {'generation': generation, 'rows': rows,
                    'receipts': receipts}
        except BaseException:
            if self.c.in_transaction:
                self.c.execute('ROLLBACK')
            raise

    def scan(self, lower=None, upper=None, after=None, limit=20):
        for bound in (lower, upper, after):
            if bound is not None:
                legacy.normalize(bound)
        if type(limit) is not int or limit <= 0:
            raise ValueError('LIMIT')
        self.c.execute('BEGIN')
        try:
            generation = self.c.execute(
                'SELECT generation FROM meta').fetchone()[0]
            if self.format == 1:
                rows = self._scan_v1(lower, upper, after, limit)
            else:
                rows = self._scan_v2(lower, upper, after, limit)
            self.c.execute('COMMIT')
            return {'generation': generation, 'rows': rows,
                    'after': copy.deepcopy(rows[-1]['key']) if rows else None}
        except BaseException:
            if self.c.in_transaction:
                self.c.execute('ROLLBACK')
            raise

    def _scan_v1(self, lower, upper, after, limit):
        clauses = ['deleted=0']
        args = []
        for bound, op in ((lower, '>='), (upper, '<'), (after, '>')):
            if bound is not None:
                clauses.append('raw ' + op + ' ? COLLATE KEY1')
                args.append(legacy.dump(bound))
        rows = [dict(key=json.loads(k), rev=r, value=json.loads(v),
                     deleted=False)
                for k, r, v in self.c.execute(
                    'SELECT raw,rev,value FROM entries WHERE '
                    + ' AND '.join(clauses) + ' ORDER BY raw COLLATE KEY1 '
                    'LIMIT ?', args + [limit])]
        return rows

    def _scan_v2(self, lower, upper, after, limit):
        lower_k = keycodec.pack(lower) if lower is not None else None
        upper_k = keycodec.pack(upper) if upper is not None else None
        after_k = keycodec.pack(after) if after is not None else None
        rows = [dict(key=json.loads(raw), rev=rev, value=json.loads(value),
                     deleted=False)
                for raw, rev, value, _ in storage2.range_rows(
                    self.c, lower_k, upper_k, after_k, limit)]
        return rows
