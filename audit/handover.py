import argparse
import json
import sqlite3
import unittest
from contextlib import closing, nullcontext
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.daybook import Daybook, read
from audit.receipts import read_receipt
from freezing.days import freeze
from freezing.store import FrozenDayStore
from intake.batches import submit
from intake.store import BatchStore
from issue.corrections import amend
from issue.register import IssueRegister, export
from issue.withdrawals import withdraw
from receive.intake import accept
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
