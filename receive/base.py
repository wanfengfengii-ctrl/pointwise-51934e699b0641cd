from freezing.history import FrozenDay
from issue.packages import Package
from receive.store import AcceptanceStore


def validate_base_package(package: Package) -> FrozenDay:
    if package.kind != 'base':
        raise ValueError('expected a base package')
    day = FrozenDay.from_payload(package.snapshot)
    if day.day_id != package.day_id or day.day_id != package.package_id:
        raise ValueError('package identity disagrees with the base snapshot')
    return day


def require_local_base(store: AcceptanceStore, day_id) -> FrozenDay:
    snapshot = store.get_base(day_id)
    if snapshot is None:
        raise LookupError(f'base not accepted: {day_id}')
    return FrozenDay.from_payload(snapshot)
