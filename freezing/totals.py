from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from freezing.membership import FrozenMember
from intake.provenance import require_fields, require_name
from intake.rows import litres_text, text_litres


@dataclass(frozen=True)
class BaseTotals:
    by_station: dict[str, int]
    litres: int

    def to_payload(self):
        return {
            'by_station': {station: litres_text(value) for station, value in self.by_station.items()},
            'litres': litres_text(self.litres),
        }

    @classmethod
    def from_payload(cls, payload):
        require_fields(payload, {'by_station', 'litres'}, 'base totals')
        if not isinstance(payload['by_station'], Mapping):
            raise ValueError('station totals must be an object')
        by_station = {
            require_name(station, 'station'): text_litres(value)
            for station, value in payload['by_station'].items()
        }
        return cls(by_station, text_litres(payload['litres']))


def compute_base_totals(members: Iterable[FrozenMember]) -> BaseTotals:
    by_station = {}
    for member in members:
        by_station.setdefault(member.station, 0)
        for row in member.rows:
            if row.litres is not None:
                by_station[member.station] += row.litres
    return BaseTotals(by_station, sum(by_station.values()))
