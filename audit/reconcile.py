from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from audit.journal import JournalLine
from freezing.totals import BaseTotals


@dataclass(frozen=True)
class StationBalance:
    base_litres: int
    delta_litres: int
    current_litres: int
    offset_litres: int = 0


@dataclass(frozen=True)
class ReconciledTotals:
    by_station: dict[str, StationBalance]
    base_litres: int
    current_litres: int


def reconcile_totals(
    base: BaseTotals,
    journal: Iterable[JournalLine],
    offsets: Mapping[str, int] | None = None,
) -> ReconciledTotals:
    offsets = offsets or {}
    deltas = dict.fromkeys(base.by_station, 0)
    for entry in journal:
        deltas[entry.station] += entry.delta
    balances = {}
    for station, quantity in base.by_station.items():
        deducted = offsets.get(station, 0)
        balances[station] = StationBalance(
            quantity, deltas[station], quantity + deltas[station] - deducted, deducted
        )
    return ReconciledTotals(
        balances, base.litres, sum(balance.current_litres for balance in balances.values())
    )
