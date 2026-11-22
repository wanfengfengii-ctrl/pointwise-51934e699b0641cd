from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date


def require_fields(value, fields, label):
    if not isinstance(value, Mapping) or value.keys() != fields:
        raise ValueError(f'invalid fields for {label}')
    return value


def require_name(value, label):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError(f'invalid {label}')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError(f'invalid {label}') from exc
    return value


def require_day(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError('day must be YYYY-MM-DD')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('day must be YYYY-MM-DD') from exc
    if parsed.isoformat() != value:
        raise ValueError('day must be YYYY-MM-DD')
    return value


@dataclass(frozen=True)
class SourceReference:
    document: str
    line: int
    batch_id: str
    position: int

    def to_payload(self):
        return {
            'document': self.document,
            'line': self.line,
            'batch_id': self.batch_id,
            'position': self.position,
        }

    @classmethod
    def from_payload(cls, payload, batch_id, position):
        require_fields(payload, {'document', 'line', 'batch_id', 'position'}, 'frozen source')
        if payload['batch_id'] != batch_id:
            raise ValueError('source belongs to another batch')
        if type(payload['position']) is not int or payload['position'] != position:
            raise ValueError('source position disagrees with row order')
        return normalize_source_reference(
            {'document': payload['document'], 'line': payload['line']}, batch_id, position
        )


def normalize_source_reference(value, batch_id, position):
    require_fields(value, {'document', 'line'}, 'source reference')
    document = require_name(value['document'], 'source document')
    line = value['line']
    if type(line) is not int or not 0 < line < 2**63:
        raise ValueError('source line must be a positive SQLite integer')
    return SourceReference(document, line, batch_id, position)
