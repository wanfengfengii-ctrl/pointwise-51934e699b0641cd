from receive.receipts import AcceptanceReceipt, receipt_from_record
from receive.store import AcceptanceStore


def read_receipt(store: AcceptanceStore, package_id) -> AcceptanceReceipt | None:
    record = store.get_acceptance(package_id)
    return None if record is None else receipt_from_record(record)
