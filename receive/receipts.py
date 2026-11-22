from dataclasses import dataclass

from receive.store import AcceptanceRecord


@dataclass(frozen=True)
class AcceptanceReceipt:
    package_id: str
    day_id: str
    kind: str
    outcome: str
    correction_id: str | None
    journal_sequence: int | None


@dataclass(frozen=True)
class OffsetResult:
    package_id: str
    day_id: str
    by_station: dict


def make_acceptance_receipt(package, outcome, correction_id, journal_sequence):
    return AcceptanceReceipt(
        package.package_id, package.day_id, package.kind, outcome, correction_id, journal_sequence
    )


def receipt_from_record(record: AcceptanceRecord) -> AcceptanceReceipt:
    return AcceptanceReceipt(**record.receipt)
