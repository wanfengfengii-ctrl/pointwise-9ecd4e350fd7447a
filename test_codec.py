"""Encoding tests: round trip, equivalence classes, order, length bound."""
import random
import sqlite3
from fractions import Fraction

import keycodec
import legacy


def length_bound(key):
    total = 1
    for tag, value in legacy.normalize(key):
        total += 1
        if tag == 1:
            total += abs(value.numerator).bit_length() + value.denominator.bit_length()
        elif tag == 2:
            total += len(value.encode('utf-8'))
        elif tag == 3:
            total += len(value)
    return 32 + 8 * total


def random_key(rng):
    parts = []
    for _ in range(rng.randint(0, 8)):
        kind = rng.choice('znsb')
        if kind == 'z':
            parts.append(['z'])
        elif kind == 'n':
            p = rng.getrandbits(rng.choice([0, 1, 2, 3, 8, 20, 64, 100, 500, 2000, 4096]))
            p = p * rng.choice([1, -1]) or 0
            q = rng.getrandbits(rng.choice([0, 1, 2, 3, 8, 20, 64, 100, 500, 2000, 4096]))
            q = q * rng.choice([1, -1]) or 1
            parts.append(['n', str(p), str(q)])
        elif kind == 's':
            chars = [0, 1, 65, 97, 0x6E, 0x4E00, 0x1F600, 0x10000, 0xFFFF, 0xE000]
            parts.append(['s', ''.join(chr(rng.choice(chars))
                                       for _ in range(rng.randint(0, 6)))])
        else:
            parts.append(['b', bytes(rng.choice([0, 1, 0xFE, 0xFF])
                                    for _ in range(rng.randint(0, 6))).hex()])
    return parts


def check_basic():
    keys = [
        [], [['z']], [['n', '0', '1']], [['n', '1', '-3']], [['n', '-1', '3']],
        [['s', '']], [['s', '\x00']], [['s', 'A\u0000井']], [['b', '']],
        [['b', '00ff']], [['n', '1', '3'], ['z']], [['n', '1', '3']],
        [['n', '1', '3'], ['s', '']], [['s', 'A'], ['z']],
        [['n', str(2**4096 - 1), str(2**4096 - 1)]],
        [['n', str(-(2**4096 - 1)), str(2**4096 - 1)]],
        [['s', '\U0010ffff'], ['b', '00'], ['z'], ['n', '1', '1']],
    ]
    for key in keys:
        blob = keycodec.pack(key)
        assert keycodec.unpack(blob) == legacy.canonical(key)
        assert len(blob) <= length_bound(key)

    equivalent = [
        ([['n', '2', '6']], [['n', '1', '3']]),
        ([['n', '-2', '-6']], [['n', '1', '3']]),
        ([['n', '0002', '0006']], [['n', '+1', '+3']]),
        ([['n', '-1', '3']], [['n', '1', '-3']]),
        ([['n', '0', '-7']], [['n', '+0', '9']]),
        ([['b', '00FF']], [['b', '00ff']]),
    ]
    for left, right in equivalent:
        assert legacy.compare(left, right) == 0
        assert keycodec.pack(left) == keycodec.pack(right)

    for bad in ([['n', '1', '0']], [['n', '1.5', '1']], [['s', '\ud800']],
                [['b', '0']], [['z', 'x']], 5, [['n', '1', '2', '3']]):
        try:
            legacy.normalize(bad)
        except ValueError as exc:
            assert str(exc) == 'KEY'
        else:
            raise AssertionError(bad)
        try:
            keycodec.pack(bad)
        except ValueError as exc:
            assert str(exc) == 'KEY'
        else:
            raise AssertionError(bad)


def check_random(rounds=2500, seed=5):
    rng = random.Random(seed)
    keys = [[]] + [random_key(rng) for _ in range(rounds)]
    for key in keys:
        blob = keycodec.pack(key)
        assert keycodec.unpack(blob) == legacy.canonical(key), key
        assert len(blob) <= length_bound(key), (key, len(blob), length_bound(key))
    sample = [random_key(rng) for _ in range(200)]
    for i, left in enumerate(sample):
        for right in sample[i:i + 20]:
            c = legacy.compare(left, right)
            bl, br = keycodec.pack(left), keycodec.pack(right)
            assert (c < 0) == (bl < br) and (c == 0) == (bl == br) \
                and (c > 0) == (bl > br), (left, right)


def check_numbers():
    values = []
    rng = random.Random(11)
    for _ in range(120):
        p = rng.getrandbits(rng.choice([1, 2, 5, 10, 40, 200, 1000, 4096])) or 1
        q = rng.getrandbits(rng.choice([1, 2, 5, 10, 40, 200, 1000])) or 1
        values += [Fraction(p, q), Fraction(-p, q)]
    values += [Fraction(n, 1) for n in range(-6, 7)]
    values += [Fraction(a, b) for a, b in
               [(1, 2), (-1, 2), (3, 2), (-3, 2), (5, 3), (7, 5), (13, 8),
                (21, 13), (2, 3), (100, 99), (-100, 99), (99, 100),
                (-99, 100), (1, 10**20), (-1, 10**20), (1, 10**60),
                (-1, 10**60)]]
    fib = [1, 1]
    while fib[-1] < 2**900:
        fib.append(fib[-1] + fib[-2])
    values += [Fraction(fib[i], fib[i - 1]) for i in range(2, len(fib))]
    values += [Fraction(fib[i - 1], fib[i]) for i in range(2, len(fib))]
    values.sort(key=Fraction)
    for left, right in zip(values, values[1:]):
        assert left <= right
        kl = [['n', str(left.numerator), str(left.denominator)]]
        kr = [['n', str(right.numerator), str(right.denominator)]]
        assert keycodec.pack(kl) <= keycodec.pack(kr), (left, right)
    # Adjacent values with a very long shared prefix must still order.
    for base in [Fraction(1, 3), Fraction(7, 5), Fraction(-7, 5), Fraction(2, 1)]:
        for delta in [Fraction(1, 10**40), Fraction(-1, 10**40), Fraction(1, 2)]:
            lo, hi = sorted([base, base + delta])
            kl = [['n', str(lo.numerator), str(lo.denominator)]]
            kr = [['n', str(hi.numerator), str(hi.denominator)]]
            assert keycodec.pack(kl) < keycodec.pack(kr)


def check_sqlite_order():
    rng = random.Random(7)
    keys = [[]] + [random_key(rng) for _ in range(300)]
    blobs = sorted({keycodec.pack(k) for k in keys})
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE t(k BLOB PRIMARY KEY) WITHOUT ROWID')
    for blob in blobs:
        c.execute('INSERT INTO t VALUES(?)', (blob,))
    assert [r[0] for r in c.execute('SELECT k FROM t ORDER BY k')] == blobs
    c.close()


def check_purity():
    key = [['n', '2', '6'], ['s', '井'], ['b', '00ff']]
    first = keycodec.pack(key)
    assert keycodec.pack(key) == first
    assert keycodec.pack(legacy.canonical(key)) == first


if __name__ == '__main__':
    check_basic()
    check_purity()
    check_random()
    check_numbers()
    check_sqlite_order()
    print('CODEC_OK')
