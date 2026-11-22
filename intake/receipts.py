from dataclasses import dataclass

from intake.rows import StationBatch


@dataclass(frozen=True)
class ImportReceipt:
    batch_id: str
    row_count: int
    missing_count: int


def make_import_receipt(batch: StationBatch) -> ImportReceipt:
    return ImportReceipt(
        batch.batch_id,
        len(batch.rows),
        sum(row.litres is None for row in batch.rows),
    )
