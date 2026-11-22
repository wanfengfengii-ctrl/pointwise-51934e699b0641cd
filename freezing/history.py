import hashlib
import json
from dataclasses import dataclass

from freezing.membership import FrozenMember, iter_frozen_rows
from freezing.store import FrozenDayStore
from freezing.totals import BaseTotals, compute_base_totals
from intake.provenance import require_day, require_fields, require_name

DAY_ID_PREFIX = 'day:'


def _day_identity(day, members):
    body = {'day': day, 'members': [member.to_payload() for member in members]}
    canonical = json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return DAY_ID_PREFIX + hashlib.sha256(canonical.encode('ascii')).hexdigest()


@dataclass(frozen=True)
class FrozenDay:
    day_id: str
    day: str
    members: tuple[FrozenMember, ...]
    totals: BaseTotals

    @property
    def rows(self):
        return tuple(iter_frozen_rows(self.members))

    def to_payload(self):
        return {
            'day_id': self.day_id,
            'day': self.day,
            'members': [member.to_payload() for member in self.members],
            'totals': self.totals.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload):
        require_fields(payload, {'day_id', 'day', 'members', 'totals'}, 'frozen day')
        day = require_day(payload['day'])
        supplied_members = payload['members']
        if not isinstance(supplied_members, list) or not supplied_members:
            raise ValueError('frozen members must be a nonempty array')
        members = tuple(FrozenMember.from_payload(member) for member in supplied_members)
        if len({member.batch_id for member in members}) != len(members):
            raise ValueError('duplicate frozen batch')
        frozen = make_frozen_day(day, members)
        if payload['day_id'] != frozen.day_id:
            raise ValueError('day identity disagrees with frozen members')
        if BaseTotals.from_payload(payload['totals']) != frozen.totals:
            raise ValueError('base totals disagree with frozen readings')
        return frozen


def make_frozen_day(day, members):
    return FrozenDay(_day_identity(day, members), day, members, compute_base_totals(members))


def load_frozen_day(store: FrozenDayStore, day_id):
    payload = store.load_snapshot(day_id)
    return None if payload is None else FrozenDay.from_payload(payload)


def require_frozen_station(day: FrozenDay, station):
    require_name(station, 'station')
    if station not in day.totals.by_station:
        raise ValueError(f'station is not in frozen day: {station}')
    return station
