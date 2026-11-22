from contextlib import contextmanager
from dataclasses import asdict

from freezing.history import FrozenDay
from receive.receipts import make_acceptance_receipt
from receive.settlement import SettlementDecision
from receive.store import AcceptanceRecord, AcceptanceStore


@contextmanager
def acceptance_transaction(store: AcceptanceStore):
    store.connection.execute('BEGIN IMMEDIATE')
    with store.connection:
        yield


def publish_acceptance(
    store: AcceptanceStore, package, wire, decision: SettlementDecision,
    base: FrozenDay | None = None, before_commit=None,
):
    if base is not None:
        store.insert_base(base.day_id, base.to_payload())
    sequence = None
    if decision.delta is not None:
        sequence = store.append_journal(
            package.day_id, package.package_id, decision.correction_id, package.station, decision.delta
        )
    receipt = make_acceptance_receipt(package, decision.outcome, decision.correction_id, sequence)
    store.insert_acceptance(AcceptanceRecord(package.package_id, wire, package.target_id, asdict(receipt)))
    if before_commit is not None:
        before_commit(receipt)
    return receipt
