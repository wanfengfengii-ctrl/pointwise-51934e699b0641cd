from collections.abc import Sequence
from dataclasses import dataclass

from intake.provenance import SourceReference, normalize_source_reference, require_fields

LITRE = 'L'
_DECIMAL_CHUNK = 10**9


def require_integer(value):
    if type(value) is not int:
        raise ValueError('litres must be an integer')
    return value


def litres_text(value):
    require_integer(value)
    try:
        return str(value)
    except ValueError:
        # Python can limit direct decimal conversion. Convert large values in chunks.
        magnitude = abs(value)
        chunks = []
        while magnitude >= _DECIMAL_CHUNK:
            magnitude, remainder = divmod(magnitude, _DECIMAL_CHUNK)
            chunks.append(f'{remainder:09d}')
        sign = '-' if value < 0 else ''
        return sign + str(magnitude) + ''.join(reversed(chunks))


def text_litres(text):
    if not isinstance(text, str):
        raise ValueError('litres must be decimal text in storage and on the wire')
    negative = text.startswith('-')
    digits = text[1:] if negative else text
    if not digits or not digits.isascii() or not digits.isdecimal():
        raise ValueError('invalid decimal litres')
    if digits[0] == '0' and (negative or len(digits) > 1):
        raise ValueError('noncanonical decimal litres')
    try:
        return int(text)
    except ValueError:
        value = 0
        for offset in range(0, len(digits), 9):
            chunk = digits[offset:offset + 9]
            value = value * 10**len(chunk) + int(chunk)
        return -value if negative else value


@dataclass(frozen=True)
class ReadingRow:
    litres: int | None
    source: SourceReference

    def to_payload(self):
        return {
            'unit': LITRE,
            'litres': None if self.litres is None else litres_text(self.litres),
            'source': self.source.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload, batch_id, position):
        require_fields(payload, {'unit', 'litres', 'source'}, 'frozen reading')
        if payload['unit'] != LITRE:
            raise ValueError('reading unit must be L')
        quantity = payload['litres']
        source = SourceReference.from_payload(payload['source'], batch_id, position)
        return cls(None if quantity is None else text_litres(quantity), source)


@dataclass(frozen=True)
class StationBatch:
    batch_id: str
    station: str
    day: str
    rows: tuple[ReadingRow, ...]


def normalize_rows(batch_id, rows):
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise ValueError('readings must be an ordered sequence')
    normalized = []
    for position, row in enumerate(rows, 1):
        require_fields(row, {'unit', 'litres', 'source'}, 'reading')
        if row['unit'] != LITRE:
            raise ValueError('reading unit must be L')
        quantity = row['litres']
        if quantity is not None:
            require_integer(quantity)
        normalized.append(ReadingRow(
            quantity, normalize_source_reference(row['source'], batch_id, position)
        ))
    return tuple(normalized)
