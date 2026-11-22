from intake.provenance import require_day, require_name
from intake.receipts import ImportReceipt, make_import_receipt
from intake.rows import StationBatch as StationBatch
from intake.rows import normalize_rows
from intake.store import BatchStore


def submit(store: BatchStore, batch_id, station, day, rows) -> tuple[StationBatch, ImportReceipt]:
    batch_id = require_name(batch_id, 'batch id')
    station = require_name(station, 'station')
    day = require_day(day)
    batch = StationBatch(batch_id, station, day, normalize_rows(batch_id, rows))
    with store.transaction():
        original = store.get_batch(batch_id)
        if original is not None:
            if original != batch:
                raise ValueError(f'conflicting batch: {batch_id}')
            receipt = store.get_receipt(batch_id)
            if receipt is None:
                raise RuntimeError(f'import receipt missing for batch: {batch_id}')
            return original, receipt
        receipt = make_import_receipt(batch)
        store.save_batch(batch, receipt)
    return batch, receipt
