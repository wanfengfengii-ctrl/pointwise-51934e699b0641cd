from collections.abc import Iterable
from dataclasses import dataclass

from audit.journal import JournalLine
from freezing.totals import BaseTotals


@dataclass(frozen=True)
class StationBalance:
    base_litres: int
    delta_litres: int
    current_litres: int


@dataclass(frozen=True)
class ReconciledTotals:
    by_station: dict[str, StationBalance]
    base_litres: int
    current_litres: int


def reconcile_totals(base: BaseTotals, journal: Iterable[JournalLine]) -> ReconciledTotals:
    deltas = dict.fromkeys(base.by_station, 0)
    for entry in journal:
        deltas[entry.station] += entry.delta
    balances = {
        station: StationBalance(quantity, deltas[station], quantity + deltas[station])
        for station, quantity in base.by_station.items()
    }
    return ReconciledTotals(
        balances, base.litres, sum(balance.current_litres for balance in balances.values())
    )
