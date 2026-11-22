from freezing.history import FrozenDay, make_frozen_day
from freezing.membership import select_members
from freezing.store import FrozenDayStore
from intake.provenance import require_day
from intake.store import BatchStore


def freeze(batches: BatchStore, days: FrozenDayStore, day, batch_ids) -> FrozenDay:
    day = require_day(day)
    members = select_members(batches, day, batch_ids)
    frozen = make_frozen_day(day, members)
    days.save_snapshot(frozen.day_id, frozen.to_payload())
    return frozen
