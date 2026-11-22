"""Raw four-station handover inputs and a check of the existing V1 base."""
import json
import sqlite3
from contextlib import closing

from audit.daybook import read
from freezing.days import freeze
from freezing.store import FrozenDayStore
from intake.batches import submit
from intake.store import BatchStore
from issue.register import IssueRegister, export
from receive.intake import accept
from receive.store import AcceptanceStore

DAY = "2026-07-14"
BATCHES = (
    ("offset-a", "甲", 5, "north.csv", 2),
    ("offset-b", "乙", 5, "east.csv", 3),
    ("offset-c", "丙", 5, "south.csv", 4),
    ("offset-d", "丁", 5, "west.csv", 5),
)
ORDERS = (
    ("P", ("甲", "乙", "丙", "丁"), 5),
    ("Q", ("甲", "乙"), 5),
    ("R", ("甲", "乙"), 5),
)
RECEIVE_ORDER = ("P", "Q", "R")
POST_ORDER = ("P", "Q", "R")


def issue_base(connection):
    batches = BatchStore(connection)
    days = FrozenDayStore(connection)
    register = IssueRegister(connection)
    for store in (batches, days, register):
        store.initialize()
    for batch_id, station, litres, document, line in BATCHES:
        rows = [{"unit": "L", "litres": litres, "source": {"document": document, "line": line}}]
        submit(batches, batch_id, station, DAY, rows)
    frozen = freeze(batches, days, DAY, [batch[0] for batch in BATCHES])
    return frozen.day_id, export(days, register, frozen.day_id)


def check_base():
    with closing(sqlite3.connect(":memory:")) as source:
        day_id, base_wire = issue_base(source)
    with closing(sqlite3.connect(":memory:")) as passive:
        ledger = AcceptanceStore(passive)
        ledger.initialize()
        receipt = accept(ledger, base_wire)
        daybook = read(ledger, day_id=day_id)
        rows = [(row.source.batch_id, row.litres, row.source.document, row.source.line)
                for row in daybook.base.rows]
        expected_rows = [(batch_id, litres, document, line)
                         for batch_id, _, litres, document, line in BATCHES]
        if (receipt != read(ledger, package_id=day_id) or rows != expected_rows or
                daybook.base.totals.by_station != {"甲": 5, "乙": 5, "丙": 5, "丁": 5} or
                daybook.totals.current_litres != 20 or daybook.journal):
            raise AssertionError("四站基础件的实收量、出处或回执不符")
        print(json.dumps({"day_id": day_id, "by_station": daybook.base.totals.by_station,
                          "litres": daybook.totals.current_litres, "rows": rows}, ensure_ascii=False))


if __name__ == "__main__":
    check_base()
