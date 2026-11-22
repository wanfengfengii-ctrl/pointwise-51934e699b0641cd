from dataclasses import dataclass

from audit.journal import JournalLine, list_journal
from audit.receipts import read_receipt
from audit.reconcile import ReconciledTotals, reconcile_totals
from freezing.history import FrozenDay
from intake.provenance import require_name
from receive.receipts import AcceptanceReceipt
from receive.store import AcceptanceStore


@dataclass(frozen=True)
class Daybook:
    base: FrozenDay
    journal: tuple[JournalLine, ...]
    totals: ReconciledTotals


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
    return Daybook(day, journal, reconcile_totals(day.totals, journal))
