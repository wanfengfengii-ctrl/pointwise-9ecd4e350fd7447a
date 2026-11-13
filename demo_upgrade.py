"""Demo: create a real legacy database with seed.py, upgrade it, show pages.

Run: python3 demo_upgrade.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys

import keycodec
import migration
import warehouse

WORK = os.environ.get('DEMO_DIR', '/tmp/calibration_demo')


def show(title, page):
    print(title)
    for row in page['rows']:
        print('  amount=%-5s key=%s' % (row['value'].get('amount'),
                                        json.dumps(row['key'], ensure_ascii=False)))
    print('  after=%s' % json.dumps(page['after'], ensure_ascii=False))


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    path = os.path.join(WORK, 'old.db')
    subprocess.run([sys.executable, 'seed.py', path], check=True,
                   stdout=subprocess.DEVNULL)

    c = sqlite3.connect(path)
    fmt, generation = c.execute('SELECT format,generation FROM meta').fetchone()
    tables = sorted(r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"))
    c.close()
    print('format 1 database: format=%d generation=%d tables=%s'
          % (fmt, generation, tables))

    store = warehouse.Store(path)
    before = store.scan(lower=[['n', '1', '3']], upper=[['s', 'A']], limit=2)
    show('format 1: scan(lower=[["n","1","3"]], upper=[["s","A"]], limit=2)',
         before)
    rest = store.scan(after=before['after'])
    show('format 1: scan(after=<previous after>)', rest)
    state1 = store.snapshot()
    store.close()

    print('\nupgrade ->', migration.upgrade(path))

    c = sqlite3.connect(path)
    fmt, generation = c.execute('SELECT format,generation FROM meta').fetchone()
    tables = sorted(r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"))
    ddl = c.execute("SELECT sql FROM sqlite_master WHERE name='entries2'"
                    ).fetchone()[0]
    plain = [keycodec.unpack(k) for (k,) in c.execute(
        'SELECT k FROM entries2 ORDER BY k')]
    c.close()
    print('format 2 database: format=%d generation=%d tables=%s'
          % (fmt, generation, tables))
    print('entries2 DDL: %s' % ' '.join(ddl.split()))
    print('a plain sqlite3 connection (no KEY1) sees %d rows in k order'
          % len(plain))

    store = warehouse.Store(path)
    assert store.snapshot() == state1, 'state must not change on upgrade'
    after = store.scan(lower=[['n', '1', '3']], upper=[['s', 'A']], limit=2)
    show('format 2: same scan on the BLOB primary key', after)
    show('format 2: scan(after=<after from format 1>)',
         store.scan(after=before['after']))
    print('snapshot identical across formats: True')
    print('scan.after from format 1 reused on format 2: %s'
          % json.dumps(after['after'], ensure_ascii=False))
    store.close()


if __name__ == '__main__':
    main()
