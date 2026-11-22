from collections.abc import Sequence
from dataclasses import dataclass

from intake.provenance import require_fields, require_name
from intake.rows import ReadingRow
from intake.store import BatchStore


@dataclass(frozen=True)
class FrozenMember:
    batch_id: str
    station: str
    rows: tuple[ReadingRow, ...]

    def to_payload(self):
        return {
            'batch_id': self.batch_id,
            'station': self.station,
            'rows': [row.to_payload() for row in self.rows],
        }

    @classmethod
    def from_payload(cls, payload):
        require_fields(payload, {'batch_id', 'station', 'rows'}, 'frozen member')
        batch_id = require_name(payload['batch_id'], 'batch id')
        station = require_name(payload['station'], 'station')
        if not isinstance(payload['rows'], list):
            raise ValueError('frozen rows must be an array')
        return cls(batch_id, station, tuple(
            ReadingRow.from_payload(row, batch_id, position)
            for position, row in enumerate(payload['rows'], 1)
        ))


def select_members(store: BatchStore, day, batch_ids):
    if not isinstance(batch_ids, Sequence) or isinstance(batch_ids, (str, bytes, bytearray)):
        raise ValueError('batch ids must be an ordered sequence')
    if not batch_ids:
        raise ValueError('select at least one batch')
    seen = set()
    members = []
    for batch_id in batch_ids:
        require_name(batch_id, 'batch id')
        if batch_id in seen:
            raise ValueError(f'batch selected twice: {batch_id}')
        batch = store.get_batch(batch_id)
        if batch is None:
            raise LookupError(f'batch not found: {batch_id}')
        if batch.day != day:
            raise ValueError(f'batch belongs to another day: {batch_id}')
        seen.add(batch_id)
        members.append(FrozenMember(batch.batch_id, batch.station, batch.rows))
    return tuple(members)


def iter_frozen_rows(members):
    for member in members:
        yield from member.rows
