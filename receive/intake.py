from itertools import combinations

from freezing.history import require_frozen_station
from intake.provenance import require_name
from issue.packages import decode_package, encode_package
from receive.base import require_local_base, validate_base_package
from receive.offsets import allocate, balanced_split, station_allowances
from receive.receipts import AcceptanceReceipt, OffsetResult, receipt_from_record
from receive.settlement import SettlementDecision, decide_correction, decide_withdrawal
from receive.store import AcceptanceStore
from receive.transactions import acceptance_transaction, publish_acceptance


def _posted_by_station(store, day_id):
    totals = {}
    for posting in store.list_offset_postings(day_id):
        for station, quantity in posting.by_station.items():
            totals[station] = totals.get(station, 0) + quantity
    return totals


def _candidate_orders(store, day_id):
    pending = []
    for package_id, _, stations, litres in store.list_offset_orders(day_id):
        if store.get_offset_posting(package_id) is None:
            pending.append((package_id, litres, stations))
    return pending


def _require_joint_feasibility(store, day_id, local_day, extra=None):
    base = local_day.totals.by_station
    posted = _posted_by_station(store, day_id)
    allowances = station_allowances(base, posted)
    requests = _candidate_orders(store, day_id)
    if extra is not None:
        requests = requests + [extra]
    if allocate(requests, allowances) is None:
        raise ValueError('pending offsets cannot be jointly fulfilled')


def accept_offset(store, package, canonical, before_commit=None):
    local_day = require_local_base(store, package.day_id)
    for station in package.stations:
        require_frozen_station(local_day, station)
    if store.get_offset_order(package.package_id) is not None:
        raise ValueError(f'offset id already bound: {package.package_id}')
    if store.find_accepted_withdrawal(package.package_id) is not None:
        raise ValueError(f'offset id is bound to an accepted withdrawal: {package.package_id}')
    _require_joint_feasibility(
        store, package.day_id, local_day, (package.package_id, package.litres, package.stations)
    )
    decision = SettlementDecision('offset_pending', None, None)
    return publish_acceptance(
        store, package, canonical, decision, None, before_commit,
        offset_order=(package.stations, package.litres),
    )


def accept(store: AcceptanceStore, wire, before_commit=None) -> AcceptanceReceipt:
    package = decode_package(wire)
    canonical = encode_package(package)
    with acceptance_transaction(store):
        original = store.get_acceptance(package.package_id)
        if original is not None:
            if original.wire != canonical:
                raise ValueError(f'conflicting delivery: {package.package_id}')
            return receipt_from_record(original)

        if package.kind == 'offset':
            return accept_offset(store, package, canonical, before_commit)

        base = None
        if package.kind == 'base':
            base = validate_base_package(package)
            decision = SettlementDecision('base_accepted', None, None)
        else:
            local_day = require_local_base(store, package.day_id)
            require_frozen_station(local_day, package.station)
            if package.kind == 'correction':
                decision = decide_correction(store, package)
            else:
                decision = decide_withdrawal(store, package)
        receipt = publish_acceptance(store, package, canonical, decision, base, before_commit)
    return receipt


def _result(record):
    return OffsetResult(record.package_id, record.day_id, dict(record.by_station))


def _choose_split(store, day_id, order, allowances):
    """Pick a split for ``order`` that keeps every remaining pending order feasible.

    A larger candidate set (more even spread, positive residual on several
    stations) is preferred; each candidate set is tried with a balanced split.
    The fallback is the joint allocation, which is feasible whenever the
    pre-existing invariant holds.
    """
    package_id, _, stations, litres = order
    remainder = [request for request in _candidate_orders(store, day_id) if request[0] != package_id]
    candidates = [station for station in stations if allowances.get(station, 0) > 0]
    if len(candidates) <= 12:
        for size in range(len(candidates), 0, -1):
            for subset in combinations(candidates, size):
                split = balanced_split(subset, allowances, litres)
                if split is None:
                    continue
                residual = dict(allowances)
                for station, quantity in split.items():
                    residual[station] -= quantity
                if allocate(remainder, residual) is None:
                    continue
                return split
    joint = allocate(_candidate_orders(store, day_id), allowances)
    if joint is None:
        return None
    split = {station: quantity for station, quantity in joint[package_id].items() if quantity > 0}
    residual = dict(allowances)
    for station, quantity in split.items():
        residual[station] -= quantity
    if allocate(remainder, residual) is None:
        return None
    return split


def post_offset(store: AcceptanceStore, offset_id, before_commit=None) -> OffsetResult:
    offset_id = require_name(offset_id, 'offset id')
    with acceptance_transaction(store):
        existing = store.get_offset_posting(offset_id)
        if existing is not None:
            return _result(existing)
        order = store.get_offset_order(offset_id)
        if order is None:
            raise LookupError(f'offset not accepted: {offset_id}')
        _, day_id, stations, litres = order
        local_day = require_local_base(store, day_id)
        posted = _posted_by_station(store, day_id)
        allowances = station_allowances(local_day.totals.by_station, posted)
        split = _choose_split(store, day_id, order, allowances)
        if split is None:
            raise ValueError('offset cannot be allocated within remaining station allowances')
        if sum(split.values()) != litres:
            raise ValueError('offset cannot be fully allocated over its candidate stations')
        store.insert_offset_posting(offset_id, day_id, split)
        result = OffsetResult(offset_id, day_id, split)
        if before_commit is not None:
            before_commit(result)
        return result
