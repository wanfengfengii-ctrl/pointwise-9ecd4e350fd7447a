"""Verification through a real legacy database created the seed.py way."""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys

import keycodec
import legacy
import migration
import seed
import storage2
import warehouse


def expect_error(call, marker):
    try:
        call()
    except ValueError as exc:
        assert str(exc) == marker, (str(exc), marker)
    else:
        raise AssertionError('missing ' + marker)


def make_legacy(path):
    """Create the real old database exactly the way seed.py does."""
    proc = subprocess.run([sys.executable, 'seed.py', path],
                          capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def boom(*args, **kwargs):
    raise RuntimeError('disk failure')


def table_names(c):
    return {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def raw_state(path):
    c = sqlite3.connect(path)
    try:
        fmt = c.execute('SELECT format,generation FROM meta').fetchall()
        receipts = c.execute('SELECT * FROM receipts ORDER BY request').fetchall()
        return fmt, receipts
    finally:
        c.close()


def check_plain_reader(path):
    """A bare sqlite3 connection (no KEY1 collation) must read k-ordered rows."""
    c = sqlite3.connect(path)
    try:
        rows = c.execute('SELECT k FROM entries2 ORDER BY k').fetchall()
        assert rows and all(isinstance(k, bytes) for (k,) in rows)
        assert c.execute("SELECT sql FROM sqlite_master WHERE name='entries2'"
                         ).fetchone()[0].strip().endswith('WITHOUT ROWID')
        assert 'entries' not in table_names(c)
        keys = [keycodec.unpack(k) for (k,) in rows]
        ordered = sorted(keys, key=legacy.normalize)
        assert keys == ordered
    finally:
        c.close()


def verify():
    work = os.environ.get('UPGRADE_DIR', '/tmp/upgrade_verify')
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    old = os.path.join(work, 'old.db')

    # Encoding suite first, then the real upgrade flow.
    import test_codec
    test_codec.check_basic()
    test_codec.check_purity()
    test_codec.check_random()
    test_codec.check_numbers()
    test_codec.check_sqlite_order()

    legacy_state = make_legacy(old)

    before_fmt, before_receipts = raw_state(old)
    assert before_fmt == [(1, 2)]
    s1 = warehouse.Store(old)
    assert s1.format == 1, 'warehouse must open an existing format 1 database as is'
    pre_page = s1.scan(lower=[['n', '1', '3']], upper=[['s', 'A']], limit=2)
    pre_after = pre_page['after']
    assert [r['value']['amount'] for r in pre_page['rows']] == [25, 22]
    pre_snapshot = s1.snapshot()
    s1.close()

    result = migration.upgrade(old)
    assert result == {'format': 2, 'rows': 9}, result
    after_fmt, after_receipts = raw_state(old)
    assert after_fmt == [(2, 2)], 'meta keeps generation, only format changes'
    assert after_receipts == before_receipts, 'receipts are untouched'
    check_plain_reader(old)

    # Idempotent: same shape, no change to rows, generation or receipts.
    again = migration.upgrade(old)
    assert again == {'format': 2, 'rows': 9}
    assert raw_state(old) == (after_fmt, after_receipts)

    s2 = warehouse.Store(old)
    assert s2.format == 2
    assert s2.snapshot() == pre_snapshot, 'state is identical across formats'
    # after from format 1 continues to work on format 2
    assert [r['value']['amount'] for r in
            s2.scan(after=pre_after)['rows']] == [31, 30, -40]
    page = s2.scan(lower=[['n', '1', '3']], upper=[['s', 'A']], limit=2)
    assert [r['value']['amount'] for r in page['rows']] == [25, 22]
    assert page['after'] == pre_after

    # Replays and old assertions against the upgraded database.
    batches = json.loads(open('samples.json', encoding='utf-8').read())
    assert s2.apply(batches[0]['request'], batches[0]['rows']) == {
        'generation': 1, 'accepted': 9}
    assert s2.apply(batches[1]['request'], batches[1]['rows']) == {
        'generation': 2, 'accepted': 3}
    expect_error(lambda: s2.apply('load', []), 'REQUEST_CONFLICT')
    bad = [{'key': [['s', 'rollback']], 'rev': 1, 'value': {}, 'deleted': False},
           {'key': [['n', '3', '9']], 'rev': 2, 'value': {'amount': 999},
            'deleted': False}]
    expect_error(lambda: s2.apply('conflict', bad), 'REVISION_CONFLICT')
    duplicate = [{'key': [['n', a, b]], 'rev': 5, 'value': {}, 'deleted': False}
                 for a, b in [('1', '2'), ('-2', '-4')]]
    expect_error(lambda: s2.apply('duplicate', duplicate), 'DUPLICATE_KEY')
    assert s2.snapshot() == pre_snapshot

    # New writes in format 2 keep the raw spelling apart from canonical identity.
    assert s2.apply('rawspell', [{'key': [['n', '0004', '0006'], ['s', 'x']],
                                  'rev': 1, 'value': {'amount': 1},
                                  'deleted': False}]) == {'generation': 3,
                                                          'accepted': 1}
    snap = s2.snapshot()
    stored = [r for r in snap['rows'] if legacy.canonical(r['key']) ==
              [['n', '2', '3'], ['s', 'x']]]
    assert len(stored) == 1 and stored[0]['key'] == [['n', '0004', '0006'],
                                                     ['s', 'x']]
    # The same revision under an equivalent spelling is a no-op, not a
    # conflict: logical identity is the encoded key, not the raw text.
    assert s2.apply('rawspell-old', [{'key': [['n', '2', '3'], ['s', 'x']],
                                      'rev': 1, 'value': {'amount': 1},
                                      'deleted': False}]) == {'generation': 4,
                                                              'accepted': 0}
    s2.close()

    # Reopen and resurrect a deleted key through the v2 path.
    s3 = warehouse.Store(old)
    assert s3.format == 2
    assert s3.apply('resurrect', [{'key': [['n', '2', '6'], ['z']], 'rev': 3,
                                   'value': {'amount': 90},
                                   'deleted': False}])['accepted'] == 1
    snap = s3.snapshot()
    tomb = [r for r in snap['rows'] if legacy.canonical(r['key']) ==
            [['n', '1', '3'], ['z']]]
    assert tomb[0]['deleted'] is False and tomb[0]['value'] == {'amount': 90}
    s3.close()
    check_plain_reader(old)

    # Failure during the copy must roll back and leave format 1 untouched.
    bad = os.path.join(work, 'bad.db')
    make_legacy(bad)
    original = open(bad, 'rb').read()
    saved = storage2.put
    storage2.put = boom
    try:
        try:
            migration.upgrade(bad)
        except RuntimeError:
            pass
        else:
            raise AssertionError('upgrade should have failed')
    finally:
        storage2.put = saved
    assert open(bad, 'rb').read() == original, 'failed upgrade must not alter the file'
    c = sqlite3.connect(bad)
    try:
        assert c.execute('SELECT format FROM meta').fetchone() == (1,)
        assert len(c.execute(
            'SELECT raw,rev,value,deleted FROM entries').fetchall()) == 9
        assert 'entries2' not in table_names(c)
    finally:
        c.close()
    assert warehouse.Store(bad).format == 1
    assert migration.upgrade(bad) == {'format': 2, 'rows': 9}

    # Simulated process interruption mid transaction: nothing is durable.
    bad2 = os.path.join(work, 'bad2.db')
    make_legacy(bad2)
    crash = (
        "import os, sys\n"
        "import migration, storage2\n"
        "calls = {'n': 0}\n"
        "saved = storage2.put\n"
        "def put(*args, **kwargs):\n"
        "    calls['n'] += 1\n"
        "    if calls['n'] == 4:\n"
        "        os._exit(9)\n"
        "    return saved(*args, **kwargs)\n"
        "storage2.put = put\n"
        "migration.upgrade(sys.argv[1])\n"
    )
    proc = subprocess.run([sys.executable, '-c', crash, bad2])
    assert proc.returncode == 9
    c = sqlite3.connect(bad2)
    try:
        assert c.execute('SELECT format FROM meta').fetchone() == (1,)
        assert len(c.execute(
            'SELECT raw,rev,value,deleted FROM entries').fetchall()) == 9
        assert 'entries2' not in table_names(c)
    finally:
        c.close()
    assert migration.upgrade(bad2) == {'format': 2, 'rows': 9}
    check_plain_reader(bad2)

    # New databases may start in either format; both pass the same checks.
    for new in (':memory:', os.path.join(work, 'new.db')):
        store = warehouse.Store(new)
        assert store.format == 2
        assert store.apply('x', [{'key': [['b', '00']], 'rev': 1,
                                  'value': {}, 'deleted': False}]) == {
            'generation': 1, 'accepted': 1}
        store.close()

    print('UPGRADE_OK', json.dumps(result))


if __name__ == '__main__':
    verify()
