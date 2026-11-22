from freezing.history import require_frozen_station
from issue.packages import decode_package, encode_package
from receive.base import require_local_base, validate_base_package
from receive.receipts import AcceptanceReceipt, receipt_from_record
from receive.settlement import SettlementDecision, decide_correction, decide_withdrawal
from receive.store import AcceptanceStore
from receive.transactions import acceptance_transaction, publish_acceptance


def accept(store: AcceptanceStore, wire, before_commit=None) -> AcceptanceReceipt:
    package = decode_package(wire)
    canonical = encode_package(package)
    with acceptance_transaction(store):
        original = store.get_acceptance(package.package_id)
        if original is not None:
            if original.wire != canonical:
                raise ValueError(f'conflicting delivery: {package.package_id}')
            return receipt_from_record(original)

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
