from dataclasses import dataclass

from issue.packages import Package, decode_package
from receive.store import AcceptanceStore


@dataclass(frozen=True)
class SettlementDecision:
    outcome: str
    correction_id: str | None
    delta: int | None


def _check_binding(withdrawal, day_id, station, correction_delta):
    if (withdrawal.day_id, withdrawal.station, withdrawal.delta) != (
        day_id, station, -correction_delta,
    ):
        raise ValueError('withdrawal binding disagrees with the correction')


def decide_correction(store: AcceptanceStore, package: Package) -> SettlementDecision:
    accepted_withdrawal = store.find_accepted_withdrawal(package.package_id)
    if accepted_withdrawal is None:
        return SettlementDecision('correction_posted', package.package_id, package.delta)
    withdrawal = decode_package(accepted_withdrawal.wire)
    _check_binding(withdrawal, package.day_id, package.station, package.delta)
    return SettlementDecision('correction_withdrawn', package.package_id, None)


def decide_withdrawal(store: AcceptanceStore, package: Package) -> SettlementDecision:
    target_id = package.target_id
    if store.find_accepted_withdrawal(package.package_id) is not None:
        raise ValueError('withdrawal id is already bound as a correction')
    if store.find_accepted_withdrawal(target_id) is not None:
        raise ValueError(f'correction already has an accepted withdrawal: {target_id}')
    entry = store.find_correction_entry(target_id)
    if entry is None:
        if store.get_acceptance(target_id) is not None:
            raise ValueError('withdrawal target was accepted without an original correction entry')
        return SettlementDecision('withdrawal_without_entry', target_id, None)
    _check_binding(package, entry.day_id, entry.station, entry.delta)
    return SettlementDecision('withdrawal_reversed', target_id, -entry.delta)
