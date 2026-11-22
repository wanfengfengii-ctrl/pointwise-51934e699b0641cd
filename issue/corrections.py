from freezing.history import load_frozen_day, require_frozen_station
from freezing.store import FrozenDayStore
from issue.packages import make_correction_package
from issue.register import IssueRegister


def amend(days: FrozenDayStore, register: IssueRegister, correction_id, day_id, station, delta):
    package = make_correction_package(correction_id, day_id, station, delta)
    with register.transaction():
        original = register.find(correction_id)
        if original is not None:
            if original.package != package:
                raise ValueError(f'conflicting issue: {correction_id}')
            return original.package.package_id
        day = load_frozen_day(days, day_id)
        if day is None:
            raise LookupError(f'frozen day not found: {day_id}')
        require_frozen_station(day, station)
        register.save(package)
    return correction_id
