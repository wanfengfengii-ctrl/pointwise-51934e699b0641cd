from dataclasses import dataclass

from audit.journal import JournalLine, list_journal
from audit.receipts import read_receipt
from audit.reconcile import ReconciledTotals, reconcile_totals
from freezing.history import FrozenDay
from intake.provenance import require_name
from receive.receipts import AcceptanceReceipt, OffsetResult, receipt_from_record
from receive.store import AcceptanceStore


@dataclass(frozen=True)
class Daybook:
    base: FrozenDay
    journal: tuple[JournalLine, ...]
    totals: ReconciledTotals
    offsets: tuple[OffsetResult, ...] = ()


def _posting_result(record) -> OffsetResult:
    return OffsetResult(record.package_id, record.day_id, dict(record.by_station))


def read_offset(store: AcceptanceStore, offset_id):
    offset_id = require_name(offset_id, 'offset id')
    record = store.get_acceptance(offset_id)
    if record is None:
        return None
    receipt = receipt_from_record(record)
    if receipt.kind != 'offset':
        return None
    posting = store.get_offset_posting(offset_id)
    return receipt, None if posting is None else _posting_result(posting)


def read(store: AcceptanceStore, *, day_id=None, package_id=None) -> Daybook | AcceptanceReceipt | None:
    if (day_id is None) == (package_id is None):
        raise ValueError('choose either a day id or a package id')
    if package_id is not None:
        return read_receipt(store, require_name(package_id, 'package id'))
    snapshot = store.get_base(require_name(day_id, 'day id'))
    if snapshot is None:
        return None
    day = FrozenDay.from_payload(snapshot)
    journal = list_journal(store, day_id)
    postings = tuple(_posting_result(record) for record in store.list_offset_postings(day_id))
    deducted = {}
    for result in postings:
        for station, quantity in result.by_station.items():
            deducted[station] = deducted.get(station, 0) + quantity
    return Daybook(day, journal, reconcile_totals(day.totals, journal, deducted), postings)
