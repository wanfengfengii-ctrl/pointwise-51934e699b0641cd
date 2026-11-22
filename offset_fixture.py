"""Raw four-station handover inputs and a check of the existing V1 base."""
import json
import sqlite3
from contextlib import closing

from audit.daybook import read, read_offset
from freezing.days import freeze
from freezing.store import FrozenDayStore
from intake.batches import submit
from intake.store import BatchStore
from issue.register import IssueRegister, export, issue_offset
from receive.intake import accept, post_offset
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


def issue_offsets(connection):
    batches = BatchStore(connection)
    days = FrozenDayStore(connection)
    register = IssueRegister(connection)
    for store in (batches, days, register):
        store.initialize()
    for batch_id, station, litres, document, line in BATCHES:
        rows = [{"unit": "L", "litres": litres, "source": {"document": document, "line": line}}]
        submit(batches, batch_id, station, DAY, rows)
    frozen = freeze(batches, days, DAY, [batch[0] for batch in BATCHES])
    base = export(days, register, frozen.day_id)
    wires = {
        offset_id: issue_offset(days, register, offset_id, frozen.day_id, stations, litres)
        for offset_id, stations, litres in ORDERS
    }
    return frozen.day_id, base, wires


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


def check_offsets():
    with closing(sqlite3.connect(":memory:")) as source:
        day_id, base_wire, wires = issue_offsets(source)
    with closing(sqlite3.connect(":memory:")) as passive:
        ledger = AcceptanceStore(passive)
        ledger.initialize()
        base_receipt = accept(ledger, base_wire)
        base_before = read(ledger, day_id=day_id)
        receipts = {}
        pending_balances = []
        for offset_id in RECEIVE_ORDER:
            receipts[offset_id] = accept(ledger, wires[offset_id])
            if receipts[offset_id].outcome != "offset_pending":
                raise AssertionError(f"{offset_id}: not pending after acceptance")
            pending_balances.append(read(ledger, day_id=day_id).totals.current_litres)
        if pending_balances != [20, 20, 20]:
            raise AssertionError(f"acceptance changed the ledger: {pending_balances}")
        posting = post_offset(ledger, POST_ORDER[0])
        if set(posting.by_station) - {"丙", "丁"} or sum(posting.by_station.values()) != 5:
            raise AssertionError(f"P split is not confined to 丙丁: {posting.by_station}")
        after_p = read(ledger, day_id=day_id)
        if after_p.totals.current_litres != 15:
            raise AssertionError(f"P did not deduct 5 L: {after_p.totals.current_litres}")
        postings = [posting] + [post_offset(ledger, offset_id) for offset_id in POST_ORDER[1:]]
        final = read(ledger, day_id=day_id)
        by_station = {station: value.current_litres for station, value in final.totals.by_station.items()}
        if (by_station["甲"], by_station["乙"]) != (0, 0):
            raise AssertionError(f"甲乙 residual is not zero: {by_station}")
        if by_station["丙"] + by_station["丁"] != 5:
            raise AssertionError(f"丙丁 residual is not 5 L: {by_station}")
        if final.totals.current_litres != 5:
            raise AssertionError(f"day does not reconcile to 5 L: {final.totals.current_litres}")
        if final.base.totals.by_station != {"甲": 5, "乙": 5, "丙": 5, "丁": 5}:
            raise AssertionError("frozen basis changed")
        if [result.package_id for result in final.offsets] != list(POST_ORDER):
            raise AssertionError(f"posting order is not preserved: {final.offsets}")
        if read_offset(ledger, POST_ORDER[0]) != (receipts[POST_ORDER[0]], posting):
            raise AssertionError("read_offset did not return P's receipt and first posting")
        if read(ledger, package_id=POST_ORDER[0]) != receipts[POST_ORDER[0]]:
            raise AssertionError("read(package_id) did not return P's original receipt")
        if base_before.journal:
            raise AssertionError("offset acceptance wrote journal entries")
        print(json.dumps({
            "day_id": day_id,
            "accept_balances": pending_balances,
            "P_by_station": posting.by_station,
            "final_by_station": by_station,
            "final_litres": final.totals.current_litres,
            "posting_order": [result.package_id for result in final.offsets],
        }, ensure_ascii=False))


if __name__ == "__main__":
    check_base()
    check_offsets()
