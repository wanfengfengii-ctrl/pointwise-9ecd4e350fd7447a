"""Order preserving, self-delimiting encoding for the logical composite key.

The encoding is a pure function of the logical key. It uses only integer
arithmetic and the standard library and never looks at a database, call order,
a global set of known keys or a precomputed ordering table.

Wire format (one tag byte per key part, parts concatenated in order):

    tag 0x00   null
    tag 0x01   rational: one sign byte, then the magnitude code if non-zero
                 sign 0x00 = negative, 0x01 = zero, 0x02 = positive
    tag 0x02   text:  UTF-8 bytes through the escaped byte code
    tag 0x03   bytes: raw bytes through the escaped byte code

Escaped byte code (used for text and byte values, including empty ones):

    0x00       -> 0x00 0xFF     (a literal NUL byte)
    0x01..0xFF -> itself
    end        -> 0x00 0x00     (terminator)

The tag order is exactly null < number < text < bytes. Every part code is
prefix free, so concatenation compares as the lexicographic sequence of parts
and a short key sorts before every strictly longer key that extends it.

Positive rationals are stored as their canonical finite simple continued
fraction with one stop/continue flag after every term:

    even (0-based) term a:  ascending code of a+1,  flag 0x01 continue
                                                    flag 0x00 stop
    odd term a:             descending code of a,   flag 0x00 continue
                                                    flag 0x01 stop

A stop flag at index i asserts that the next term, at index i+1, is the
symbolic term +infinity; extending a finite continued fraction by +infinity
at the end yields the same value, so stopping at any position is exactly
that extension and the flags never change the value, only its byte order.
Because even terms compare ascending and odd terms descending, and because
+infinity sorts after every finite term at an even position but before
every finite term at an odd one, the first differing byte decides the
comparison correctly. Negative rationals are the byte complement of the
magnitude code, which reverses that order. Zero has its own sign byte and is
the same code as 0/1, 0/7, 0/-3 and every other spelling of zero.

The ascending code of n >= 1 is: L-1 bytes 0x01, one 0x00, then ceil(L/8)
big endian bytes of n (no high zero padding), L = n.bit_length(). It is
order preserving, prefix free and 2L+1 bytes long. The descending code is its
mirror (0xFE leaders, 0xFF stop, complemented payload). See NOTES.md for the
ordering and length proofs.
"""
from fractions import Fraction

import legacy

_TAG_NULL = 0x00
_TAG_NUM = 0x01
_TAG_TEXT = 0x02
_TAG_BYTES = 0x03
_SIGN_NEG = 0x00
_SIGN_ZERO = 0x01
_SIGN_POS = 0x02
_MAX_PARTS = 8

_FLAG_CONTINUE_EVEN = 0x01
_FLAG_STOP_EVEN = 0x00
_FLAG_STOP_ODD = 0x01
_FLAG_CONTINUE_ODD = 0x00
_ASC_LEAD = 0x01
_ASC_STOP = 0x00
_DESC_LEAD = 0xFE
_DESC_STOP = 0xFF
_COMPLEMENT = 0xFF


# --------------------------------------------------------------------------
# Integer codes. All are order preserving and prefix free.
# --------------------------------------------------------------------------

def _asc_code(n):
    """Ascending code of n >= 1: L-1 x 0x01, 0x00, ceil(L/8) big endian bytes."""
    bits = n.bit_length()
    return (b'\x01' * (bits - 1) + b'\x00'
            + n.to_bytes((bits + 7) // 8, 'big'))


def _desc_code(n):
    """Descending code of n >= 1; larger n sorts earlier."""
    bits = n.bit_length()
    payload = n.to_bytes((bits + 7) // 8, 'big')
    return (b'\xfe' * (bits - 1) + b'\xff'
            + bytes(_COMPLEMENT - byte for byte in payload))


def _read_asc(data, pos, end):
    start = pos
    while pos < end and data[pos] == _ASC_LEAD:
        pos += 1
    if pos >= end or data[pos] != _ASC_STOP:
        raise ValueError('KEY')
    bits = pos - start + 1
    width = (bits + 7) // 8
    pos += 1
    if pos + width > end:
        raise ValueError('KEY')
    value = int.from_bytes(data[pos:pos + width], 'big')
    if value.bit_length() != bits:
        raise ValueError('KEY')
    return value, pos + width


def _read_desc(data, pos, end):
    start = pos
    while pos < end and data[pos] == _DESC_LEAD:
        pos += 1
    if pos >= end or data[pos] != _DESC_STOP:
        raise ValueError('KEY')
    bits = pos - start + 1
    width = (bits + 7) // 8
    pos += 1
    if pos + width > end:
        raise ValueError('KEY')
    raw = bytes(_COMPLEMENT - byte for byte in data[pos:pos + width])
    value = int.from_bytes(raw, 'big')
    if value.bit_length() != bits:
        raise ValueError('KEY')
    return value, pos + width


# --------------------------------------------------------------------------
# Rationals.
# --------------------------------------------------------------------------

def _magnitude_code(numerator, denominator):
    """Encode a positive canonical fraction; terms and stop flags."""
    terms = []
    p, q = numerator, denominator
    index = 0
    while True:
        if index % 2 == 0:
            a = p // q
            terms.append((index, _asc_code(a + 1)))
            p, q = q, p - a * q
        else:
            a = p // q
            terms.append((index, _desc_code(a)))
            p, q = q, p - a * q
        if q == 0:
            break
        index += 1
    out = bytearray()
    for index, code in terms:
        out.extend(code)
        last = index == len(terms) - 1
        if index % 2 == 0:
            out.append(_FLAG_STOP_EVEN if last else _FLAG_CONTINUE_EVEN)
        else:
            out.append(_FLAG_STOP_ODD if last else _FLAG_CONTINUE_ODD)
    return bytes(out)


def _read_magnitude(data, pos, end, negative=False):
    terms = []
    index = 0
    while True:
        if index % 2 == 0:
            # The encoded body is complemented as a whole for negatives, so
            # a complemented ascending code is read as a descending one.
            if negative:
                value, pos = _read_desc(data, pos, end)
            else:
                value, pos = _read_asc(data, pos, end)
            term = value - 1
        else:
            if negative:
                value, pos = _read_asc(data, pos, end)
            else:
                value, pos = _read_desc(data, pos, end)
            term = value
        if pos >= end:
            raise ValueError('KEY')
        flag = data[pos]
        pos += 1
        if index % 2 == 0:
            want_stop = _COMPLEMENT - _FLAG_STOP_EVEN if negative else _FLAG_STOP_EVEN
            want_cont = (_COMPLEMENT - _FLAG_CONTINUE_EVEN if negative
                         else _FLAG_CONTINUE_EVEN)
        else:
            want_stop = _COMPLEMENT - _FLAG_STOP_ODD if negative else _FLAG_STOP_ODD
            want_cont = (_COMPLEMENT - _FLAG_CONTINUE_ODD if negative
                         else _FLAG_CONTINUE_ODD)
        if flag == want_stop:
            stopped = True
        elif flag == want_cont:
            stopped = False
        else:
            raise ValueError('KEY')
        terms.append(term)
        index += 1
        if stopped:
            break
    if len(terms) > 1 and (terms[-1] < 2 or any(t < 1 for t in terms[1:])):
        raise ValueError('KEY')
    value = Fraction(terms[-1], 1)
    for term in reversed(terms[1:-1]):
        value = term + 1 / value
    if len(terms) > 1:
        value = terms[0] + 1 / value
    else:
        value = Fraction(terms[0], 1)
    return value, pos


def _number_code(value):
    if value.numerator == 0:
        return bytes((_SIGN_ZERO,))
    body = _magnitude_code(abs(value.numerator), value.denominator)
    if value.numerator < 0:
        return bytes((_SIGN_NEG,)) + bytes(_COMPLEMENT - b for b in body)
    return bytes((_SIGN_POS,)) + body


def _read_number(data, pos, end):
    if pos >= end:
        raise ValueError('KEY')
    sign = data[pos]
    pos += 1
    if sign == _SIGN_ZERO:
        return Fraction(0), pos
    if sign not in (_SIGN_NEG, _SIGN_POS):
        raise ValueError('KEY')
    negative = sign == _SIGN_NEG
    value, pos = _read_magnitude(data, pos, end, negative)
    return (-value if negative else value), pos


# --------------------------------------------------------------------------
# Text and byte values.
# --------------------------------------------------------------------------

def _escape(raw):
    return raw.replace(b'\x00', b'\x00\xff') + b'\x00\x00'


def _unescape(data, pos, end):
    out = bytearray()
    while True:
        if pos >= end:
            raise ValueError('KEY')
        byte = data[pos]
        pos += 1
        if byte:
            out.append(byte)
            continue
        if pos >= end:
            raise ValueError('KEY')
        byte = data[pos]
        pos += 1
        if byte == 0x00:
            return bytes(out), pos
        if byte != 0xFF:
            raise ValueError('KEY')
        out.append(0x00)


# --------------------------------------------------------------------------
# Public API.
# --------------------------------------------------------------------------

def pack(key):
    """Encode a logical key (accepted by legacy.normalize) into bytes."""
    parts = legacy.normalize(key)
    out = bytearray()
    for tag, value in parts:
        if tag == 0:
            out.append(_TAG_NULL)
        elif tag == 1:
            out.append(_TAG_NUM)
            out.extend(_number_code(value))
        elif tag == 2:
            out.append(_TAG_TEXT)
            out.extend(_escape(value.encode('utf-8')))
        else:
            out.append(_TAG_BYTES)
            out.extend(_escape(bytes(value)))
    return bytes(out)


def unpack(blob):
    """Decode bytes produced by pack into the canonical logical key."""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ValueError('KEY')
    data = bytes(blob)
    end = len(data)
    pos = 0
    out = []
    while pos < end:
        if len(out) >= _MAX_PARTS:
            raise ValueError('KEY')
        tag = data[pos]
        pos += 1
        if tag == _TAG_NULL:
            out.append(['z'])
        elif tag == _TAG_NUM:
            value, pos = _read_number(data, pos, end)
            out.append(['n', str(value.numerator), str(value.denominator)])
        elif tag == _TAG_TEXT:
            raw, pos = _unescape(data, pos, end)
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                raise ValueError('KEY')
            if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
                raise ValueError('KEY')
            out.append(['s', text])
        elif tag == _TAG_BYTES:
            raw, pos = _unescape(data, pos, end)
            out.append(['b', raw.hex()])
        else:
            raise ValueError('KEY')
    return legacy.canonical(out)
