from dataclasses import dataclass

from receive.store import AcceptanceStore


@dataclass(frozen=True)
class JournalLine:
    sequence: int
    package_id: str
    correction_id: str
    station: str
    delta: int


def list_journal(store: AcceptanceStore, day_id) -> tuple[JournalLine, ...]:
    return tuple(
        JournalLine(entry.sequence, entry.package_id, entry.correction_id, entry.station, entry.delta)
        for entry in store.list_journal(day_id)
    )
