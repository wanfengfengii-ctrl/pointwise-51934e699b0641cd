import argparse
import json
import sqlite3
import unittest
from contextlib import closing, nullcontext
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.daybook import Daybook, read, read_offset
from audit.receipts import read_receipt
from freezing.days import freeze
from freezing.store import FrozenDayStore
from intake.batches import submit
from intake.store import BatchStore
from issue.corrections import amend
from issue.register import IssueRegister, export, issue_offset
from issue.withdrawals import withdraw
from receive.intake import accept, post_offset
from receive.receipts import AcceptanceReceipt
from receive.store import AcceptanceStore

_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class HandoverReport:
    daybook: Daybook
    receipts: tuple[AcceptanceReceipt, ...]


def prepare_handover(store: AcceptanceStore, day_id, package_ids) -> HandoverReport:
    connection = store.connection
    if connection.in_transaction:
        snapshot = nullcontext()
    else:
        connection.execute('BEGIN')
        snapshot = connection
    with snapshot:
        daybook = read(store, day_id=day_id)
        if daybook is None:
            raise LookupError(f'day not found: {day_id}')
        receipts = []
        for package_id in package_ids:
            receipt = read_receipt(store, package_id)
            if receipt is None:
                raise LookupError(f'receipt not found: {package_id}')
            if receipt.day_id != day_id:
                raise ValueError(f'receipt belongs to another day: {package_id}')
            receipts.append(receipt)
        return HandoverReport(daybook, tuple(receipts))


def _initialize(source_path, ledger_path):
    source_path = Path(source_path).resolve()
    ledger_path = Path(ledger_path).resolve()
    same_file = source_path == ledger_path
    if not same_file and source_path.exists() and ledger_path.exists():
        same_file = source_path.samefile(ledger_path)
    if same_file:
        raise ValueError('source and ledger must use separate databases')
    with closing(sqlite3.connect(source_path)) as source:
        BatchStore(source).initialize()
        FrozenDayStore(source).initialize()
        IssueRegister(source).initialize()
    with closing(sqlite3.connect(ledger_path)) as ledger:
        AcceptanceStore(ledger).initialize()


def _report_scenario(case_id, inputs, expected, actual, downstream_expected, downstream_actual,
                     code_paths, oracle):
    if actual != expected or downstream_actual != downstream_expected:
        raise AssertionError(
            f'{case_id}: measured {actual!r}, downstream {downstream_actual!r}; '
            f'expected {expected!r}, downstream {downstream_expected!r}'
        )
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': case_id,
        'input': inputs,
        'expected': expected,
        'actual': actual,
        'downstream_expected': downstream_expected,
        'downstream_actual': downstream_actual,
        'code_paths': code_paths,
        'oracle': oracle,
    }, ensure_ascii=False, sort_keys=True))


def _issue_sample(source_path, sample):
    with closing(sqlite3.connect(source_path)) as source:
        batches = BatchStore(source)
        days = FrozenDayStore(source)
        register = IssueRegister(source)
        _, imported = submit(batches, sample['batch_id'], sample['station'], sample['day'], sample['rows'])
        day = freeze(batches, days, sample['day'], [sample['batch_id']])
        base = export(days, register, day.day_id)
        for correction_id, delta in sample['corrections'].items():
            amend(days, register, correction_id, day.day_id, sample['station'], delta)
        withdrawal = sample['withdrawal']
        withdraw(register, withdrawal['id'], withdrawal['target'])
        ids = (*sample['corrections'], withdrawal['id'])
        actions = {package_id: export(days, register, package_id) for package_id in ids}
    return day.day_id, base, actions, imported


def _check_base(sample, ledger, day_id, base_receipt, imported):
    report = prepare_handover(ledger, day_id, [day_id])
    if report.receipts != (base_receipt,):
        raise AssertionError('v1: base receipt changed')
    book = report.daybook
    _report_scenario(
        'v1', {'day': sample['day'], 'batch_id': sample['batch_id'], 'rows': sample['rows']},
        {'row_count': 3, 'missing_count': 1, 'base_litres': 20},
        {'row_count': imported.row_count, 'missing_count': imported.missing_count,
         'base_litres': book.base.totals.litres},
        {'litres_in_order': [7, None, 13], 'source_lines': [2, 3, 4], 'current_litres': 20},
        {'litres_in_order': [row.litres for row in book.base.rows],
         'source_lines': [row.source.line for row in book.base.rows],
         'current_litres': book.totals.current_litres},
        ['intake/batches.py', 'freezing/totals.py', 'receive/base.py', 'audit/daybook.py'],
        '手算 7 + 13 = 20，原文件第 3 行缺测且保持在原位。',
    )


def _run_delivery(path, sample, case_id, day_id, base, actions, imported):
    order = sample['deliveries'][case_id]
    balances = []
    first_receipts = []
    with closing(sqlite3.connect(path)) as connection:
        ledger = AcceptanceStore(connection)
        base_receipt = accept(ledger, base)
        if case_id == 'v5':
            _check_base(sample, ledger, day_id, base_receipt, imported)
        for package_id in order:
            first_receipts.append(accept(ledger, actions[package_id]))
            balances.append(read(ledger, day_id=day_id).totals.current_litres)
    with closing(sqlite3.connect(path)) as connection:
        ledger = AcceptanceStore(connection)
        report = prepare_handover(ledger, day_id, order)
        repeated = [accept(ledger, actions[package_id]) for package_id in order]
        reread_book = read(ledger, day_id=day_id)
        receipts_preserved = tuple(first_receipts) == report.receipts == tuple(repeated)
        if reread_book != report.daybook:
            raise AssertionError(f'{case_id}: replay changed the daybook')
    if case_id == 'v5':
        expected_balances = [20, 24, 24]
        expected_entries = [['D', 'D', 4]]
        expected_outcomes = ['withdrawal_without_entry', 'correction_posted', 'correction_withdrawn']
    else:
        expected_balances = [25, 29, 24]
        expected_entries = [['C', 'C', 5], ['D', 'D', 4], ['R', 'C', -5]]
        expected_outcomes = ['correction_posted', 'correction_posted', 'withdrawal_reversed']
    _report_scenario(
        case_id, {'delivery_order': order, 'corrections': sample['corrections'],
                  'withdrawal': sample['withdrawal'], 'station': sample['station']},
        {'balances': expected_balances, 'journal': expected_entries},
        {'balances': balances,
         'journal': [[line.package_id, line.correction_id, line.delta] for line in report.daybook.journal]},
        {'station_litres': 24, 'outcomes': expected_outcomes, 'receipts_preserved': True},
        {'station_litres': report.daybook.totals.by_station[sample['station']].current_litres,
         'outcomes': [receipt.outcome for receipt in report.receipts],
         'receipts_preserved': receipts_preserved},
        ['issue/withdrawals.py', 'receive/settlement.py', 'receive/transactions.py',
         'audit/journal.py', 'audit/reconcile.py', 'audit/handover.py'],
        '从 20 L 手算。D 始终加 4 L。C 已入账时 R 反记 5 L，C 未入账时两件均无分录。',
    )


def _demo():
    sample = json.loads((_ROOT / 'samples' / 'day.json').read_text(encoding='utf-8'))
    with TemporaryDirectory(prefix='meter-daybook-demo-') as directory:
        root = Path(directory)
        source_path = root / 'source.sqlite'
        early_path = root / 'early.sqlite'
        late_path = root / 'late.sqlite'
        _initialize(source_path, early_path)
        _initialize(source_path, late_path)
        day_id, base, actions, imported = _issue_sample(source_path, sample)
        _run_delivery(early_path, sample, 'v5', day_id, base, actions, imported)
        _run_delivery(late_path, sample, 'v6', day_id, base, actions, imported)
        _run_offset_demo(root)


OFFSET_STATIONS = ('甲', '乙', '丙', '丁')
OFFSET_DAY = '2026-07-14'
OFFSET_BATCHES = (
    ('offset-a', '甲', 5, 'north.csv', 2),
    ('offset-b', '乙', 5, 'east.csv', 3),
    ('offset-c', '丙', 5, 'south.csv', 4),
    ('offset-d', '丁', 5, 'west.csv', 5),
)
OFFSET_ORDERS = (
    ('P', OFFSET_STATIONS, 5),
    ('Q', ('甲', '乙'), 5),
    ('R', ('甲', '乙'), 5),
)


def _issue_offset_day(source_path):
    with closing(sqlite3.connect(source_path)) as source:
        batches = BatchStore(source)
        days = FrozenDayStore(source)
        register = IssueRegister(source)
        for store in (batches, days, register):
            store.initialize()
        for batch_id, station, litres, document, line in OFFSET_BATCHES:
            submit(batches, batch_id, station, OFFSET_DAY, [
                {'unit': 'L', 'litres': litres, 'source': {'document': document, 'line': line}}
            ])
        day = freeze(batches, days, OFFSET_DAY, [batch[0] for batch in OFFSET_BATCHES])
        base = export(days, register, day.day_id)
        orders = {
            offset_id: issue_offset(days, register, offset_id, day.day_id, stations, litres)
            for offset_id, stations, litres in OFFSET_ORDERS
        }
        return day.day_id, base, orders


def _run_offset_demo(root):
    source_path = root / 'offset-source.sqlite'
    ledger_path = root / 'offset-ledger.sqlite'
    _initialize(source_path, ledger_path)
    day_id, base, orders = _issue_offset_day(source_path)
    receipts = {}
    balances = []
    with closing(sqlite3.connect(ledger_path)) as connection:
        ledger = AcceptanceStore(connection)
        accept(ledger, base)
        for offset_id in ('P', 'Q', 'R'):
            receipts[offset_id] = accept(ledger, orders[offset_id])
            balances.append(read(ledger, day_id=day_id).totals.current_litres)
        posting = post_offset(ledger, 'P')
        after_p = read(ledger, day_id=day_id)
        touched = next(iter(posting.by_station))
        residual = 5 - posting.by_station[touched]
    # The source register is the authority for issuing the two dedicated orders.
    with closing(sqlite3.connect(source_path)) as source:
        days = FrozenDayStore(source)
        register = IssueRegister(source)
        rejected = issue_offset(days, register, 'X', day_id, (touched,), 5)
        accepted = issue_offset(days, register, 'Y', day_id, (touched,), residual)
    with closing(sqlite3.connect(ledger_path)) as connection:
        ledger = AcceptanceStore(connection)
        try:
            accept(ledger, rejected)
            rejected_ok = False
        except ValueError as exc:
            rejected_ok = 'jointly fulfilled' in str(exc)
        accepted_ok = accept(ledger, accepted).outcome
        postings = {}
        for offset_id in ('Q', 'R'):
            postings[offset_id] = post_offset(ledger, offset_id)
        final = read(ledger, day_id=day_id)
    with closing(sqlite3.connect(ledger_path)) as connection:
        reopened = read_offset(AcceptanceStore(connection), 'P')
    _report_offset_scenario(
        day_id, receipts, balances, posting, after_p, postings, final, rejected_ok, accepted_ok,
        residual, reopened,
    )
    _run_multi_station_demo(root, source_path, day_id)


def _run_multi_station_demo(root, source_path, day_id):
    """7 L over 丙丁, a failing commit checkpoint, reopen and a lost reply."""
    ledger_path = root / 'offset-seven.sqlite'
    _initialize(source_path, ledger_path)
    with closing(sqlite3.connect(source_path)) as source:
        days = FrozenDayStore(source)
        register = IssueRegister(source)
        seven = issue_offset(days, register, 'S', day_id, ('丙', '丁'), 7)
    with closing(sqlite3.connect(ledger_path)) as connection:
        ledger = AcceptanceStore(connection)
        with closing(sqlite3.connect(root / 'offset-source.sqlite')) as base_source:
            base = export(FrozenDayStore(base_source), IssueRegister(base_source), day_id)
        accept(ledger, base)
        receipt = accept(ledger, seven)
        observed = []

        def fail(result):
            observed.append(result)
            if read(ledger, day_id=day_id).totals.current_litres != 13:
                raise AssertionError('pending posting not visible inside the callback')
            raise RuntimeError('lost reply')

        try:
            post_offset(ledger, 'S', before_commit=fail)
            rolled_back = False
        except RuntimeError as exc:
            rolled_back = str(exc) == 'lost reply'
    with closing(sqlite3.connect(ledger_path)) as connection:
        ledger = AcceptanceStore(connection)
        after_failure = read(ledger, day_id=day_id)
        preserved = read_offset(ledger, 'S')
    with closing(sqlite3.connect(ledger_path)) as connection:
        ledger = AcceptanceStore(connection)
        result = post_offset(ledger, 'S')
    with closing(sqlite3.connect(ledger_path)) as connection:
        ledger = AcceptanceStore(connection)
        reopened = read_offset(ledger, 'S')
        replayed = post_offset(ledger, 'S')
        final = read(ledger, day_id=day_id)
    if not rolled_back:
        raise AssertionError('failed commit checkpoint did not propagate')
    if after_failure.totals.current_litres != 20 or preserved[1] is not None:
        raise AssertionError('failed posting was not fully rolled back')
    if preserved[0] != receipt:
        raise AssertionError('first acceptance receipt was not preserved after rollback')
    if sum(result.by_station.values()) != 7 or set(result.by_station) - {'丙', '丁'}:
        raise AssertionError(f'7 L split is not confined to 丙丁: {result.by_station}')
    if final.totals.current_litres != 13:
        raise AssertionError(f'7 L posting did not leave 13 L: {final.totals.current_litres}')
    if reopened != (receipt, result) or replayed != result:
        raise AssertionError('reopened or replayed S did not return the first posting')
    if len(observed) != 1:
        raise AssertionError('the commit checkpoint did not receive exactly one result')
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': 'offset-multi',
        'input': {'order': ['S', ['丙', '丁'], 7]},
        'expected': {'initial_litres': 20, 'after_failure': 20, 'after_retry': 13,
                     'split_total': 7},
        'actual': {'initial_litres': after_failure.totals.current_litres,
                   'after_retry': final.totals.current_litres, 'split': result.by_station,
                   'split_total': sum(result.by_station.values())},
        'downstream_expected': {'rollback_propagated': True, 'receipt_preserved': True,
                                'reopened_split': 'first posting', 'replayed_same': True},
        'downstream_actual': {'rollback_propagated': rolled_back,
                              'receipt_preserved': preserved[0] == receipt,
                              'reopened_split': reopened[1].by_station if reopened[1] else None,
                              'replayed_same': replayed == result},
        'code_paths': ['receive/intake.py', 'receive/offsets.py', 'receive/store.py',
                       'audit/daybook.py', 'audit/handover.py'],
        'oracle': '丙丁各 5 L，7 L 单须跨两站整数拆扣；回调失败回滚为 20 L，'
                  '重试后为 13 L，重开按号可查回原分摊。',
    }, ensure_ascii=False, sort_keys=True))


def _report_offset_scenario(day_id, receipts, balances, posting, after_p, postings, final,
                            rejected_ok, accepted_ok, residual, reopened):
    expected_balance_snapshot = (20, 20, 20)
    if tuple(balances) != expected_balance_snapshot:
        raise AssertionError(f'offset accept changed the balance: {balances}')
    if not (set(posting.by_station) <= {'丙', '丁'} and sum(posting.by_station.values()) == 5):
        raise AssertionError(f'P split is not confined to 丙丁: {posting.by_station}')
    if after_p.totals.current_litres != 15:
        raise AssertionError(f'P did not deduct 5 L: {after_p.totals.current_litres}')
    final_by_station = {station: value.current_litres
                        for station, value in final.totals.by_station.items()}
    if (final_by_station['甲'], final_by_station['乙']) != (0, 0):
        raise AssertionError(f'甲乙 residual is not zero: {final_by_station}')
    if final_by_station['丙'] + final_by_station['丁'] != 5:
        raise AssertionError(f'丙丁 residual is not 5 L: {final_by_station}')
    if final.totals.current_litres != 5:
        raise AssertionError(f'day total is not 5 L: {final.totals.current_litres}')
    if not rejected_ok:
        raise AssertionError('an over-committed dedicated station order was accepted')
    if accepted_ok != 'offset_pending':
        raise AssertionError(f'residual dedicated order was rejected: {accepted_ok}')
    if reopened != (receipts['P'], posting):
        raise AssertionError(f'reopened P does not preserve the first posting: {reopened}')
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': 'offset',
        'input': {'orders': [list(order) for order in OFFSET_ORDERS],
                  'post_order': ['P', 'Q', 'R']},
        'expected': {'accept_balances': list(expected_balance_snapshot),
                     'P_split': '丙丁合计 5 L', 'final': {'甲': 0, '乙': 0, '丙丁': 5}},
        'actual': {'accept_balances': list(balances), 'P_split': posting.by_station,
                   'P_current': after_p.totals.current_litres,
                   'final_by_station': final_by_station,
                   'final_total': final.totals.current_litres},
        'downstream_expected': {'dedicated_5L_rejected': True,
                                'residual_dedicated_accepted': True,
                                'reopened_P': '首执与实际分摊不变'},
        'downstream_actual': {'dedicated_5L_rejected': rejected_ok,
                              'residual_dedicated_accepted': accepted_ok == 'offset_pending',
                              'residual_litres': residual,
                              'reopened_P': [reopened[0].outcome, reopened[1].by_station]},
        'code_paths': ['issue/register.py', 'issue/packages.py', 'receive/intake.py',
                       'receive/offsets.py', 'receive/store.py', 'audit/daybook.py',
                       'audit/reconcile.py', 'audit/handover.py'],
        'oracle': '四站各 5 L。P 先只扣丙丁合计 5 L；Q、R 扣尽甲乙。'
                  '已过账的 P 分摊固定，重开按号仍可查回。',
    }, ensure_ascii=False, sort_keys=True))


def _verify():
    suite = unittest.defaultTestLoader.discover(str(_ROOT / 'tests'), top_level_dir=str(_ROOT))
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='Meter warehouse handover')
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='initialize the source and isolated ledger')
    init.add_argument('--source', type=Path, required=True)
    init.add_argument('--ledger', type=Path, required=True)
    commands.add_parser('demo', help='run the sample real handover')
    commands.add_parser('verify', help='run all business regressions')
    args = parser.parse_args(argv)
    if args.command == 'verify':
        return _verify()
    if args.command == 'init':
        _initialize(args.source, args.ledger)
    else:
        _demo()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
