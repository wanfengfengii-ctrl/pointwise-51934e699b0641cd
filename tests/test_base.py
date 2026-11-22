import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.daybook import read
from audit.handover import main, prepare_handover
from freezing.days import freeze
from freezing.history import load_frozen_day
from freezing.store import FrozenDayStore
from intake.batches import submit
from intake.store import BatchStore
from issue.corrections import amend
from issue.register import IssueRegister, export
from receive.intake import accept
from receive.store import AcceptanceStore

DAY = '2026-07-14'


def reading(litres, line, document='station.csv', unit='L'):
    return {'litres': litres, 'unit': unit, 'source': {'document': document, 'line': line}}


class WarehouseCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix='meter-daybook-test-')))
        self.source_path = self.root / 'source.sqlite'
        self.ledger_path = self.root / 'ledger.sqlite'

    def connect(self, path):
        return self.enterContext(closing(sqlite3.connect(path)))

    def source(self, path=None):
        connection = self.connect(path or self.source_path)
        batches = BatchStore(connection)
        days = FrozenDayStore(connection)
        register = IssueRegister(connection)
        for store in (batches, days, register):
            store.initialize()
        return connection, batches, days, register

    def ledger(self, path=None):
        store = AcceptanceStore(self.connect(path or self.ledger_path))
        store.initialize()
        return store

    def sample_day(self, batches, days):
        submit(batches, 'batch', 'S1', DAY, [reading(7, 2), reading(None, 3), reading(13, 4)])
        return freeze(batches, days, DAY, ['batch'])


class BaseTests(WarehouseCase):
    def test_base_day_with_missing_reading(self):
        source, batches, days, register = self.source()
        day = self.sample_day(batches, days)
        self.assertEqual(batches.get_receipt('batch').missing_count, 1)
        wire = export(days, register, day.day_id)
        source.close()

        ledger = self.ledger()
        receipt = accept(ledger, wire)
        report = prepare_handover(ledger, day.day_id, [day.day_id])
        book = report.daybook
        self.assertEqual([row.litres for row in book.base.rows], [7, None, 13])
        self.assertEqual(
            [(row.source.document, row.source.line, row.source.batch_id, row.source.position)
             for row in book.base.rows],
            [('station.csv', 2, 'batch', 1), ('station.csv', 3, 'batch', 2),
             ('station.csv', 4, 'batch', 3)],
        )
        self.assertEqual(book.base.totals.by_station, {'S1': 20})
        self.assertEqual(book.totals.current_litres, 20)
        self.assertEqual(book.journal, ())
        self.assertEqual(report.receipts, (receipt,))
        self.assertEqual(receipt.outcome, 'base_accepted')
        self.assertEqual(accept(ledger, json.dumps(json.loads(wire), indent=2)), receipt)
        self.assertIsNone(read(ledger, package_id='unreceived'))
        self.assertIsNone(read(ledger, day_id='unknown'))

    def test_batch_replay(self):
        source, batches, _, _ = self.source()
        rows = [reading(0, 2), reading(None, 3)]
        original, receipt = submit(batches, 'batch', 'S1', DAY, rows)
        self.assertEqual((receipt.batch_id, receipt.row_count, receipt.missing_count), ('batch', 2, 1))
        source.close()
        _, reopened, _, _ = self.source()
        self.assertEqual(submit(reopened, 'batch', 'S1', DAY, rows), (original, receipt))
        self.assertEqual(reopened.get_receipt('batch'), receipt)
        self.assertEqual(reopened.connection.execute('SELECT count(*) FROM reading_rows').fetchone()[0], 2)

    def test_selected_membership(self):
        source, batches, days, register = self.source()
        submit(batches, 'A-selected', 'west', DAY, [reading(13, 8, 'west.csv')])
        submit(batches, 'Z-selected', 'east', DAY, [reading(7, 4, 'east.csv'), reading(None, 7, 'east.csv')])
        submit(batches, 'M-standby', 'west', DAY, [reading(11, 5, 'standby.csv')])
        submit(batches, 'N-missing', 'north', DAY, [reading(None, 2, 'north.csv')])
        day = freeze(batches, days, DAY, ['Z-selected', 'A-selected', 'N-missing'])
        self.assertEqual(day.totals.by_station, {'east': 7, 'west': 13, 'north': 0})
        wire = export(days, register, day.day_id)
        source.close()
        ledger = self.ledger()
        accept(ledger, wire)
        book = read(ledger, day_id=day.day_id)
        self.assertEqual([member.batch_id for member in book.base.members], ['Z-selected', 'A-selected', 'N-missing'])
        self.assertEqual([(row.source.document, row.source.line) for row in book.base.rows],
                         [('east.csv', 4), ('east.csv', 7), ('west.csv', 8), ('north.csv', 2)])
        self.assertEqual([row.litres for row in book.base.rows], [7, None, 13, None])
        self.assertEqual(book.base.totals.litres, 20)
        self.assertEqual(book.totals.current_litres, 20)

    def test_invalid_selection(self):
        connection, batches, days, _ = self.source()
        submit(batches, 'right-day', 'S1', DAY, [reading(7, 1)])
        submit(batches, 'wrong-day', 'S1', '2026-07-13', [reading(13, 2)])
        with self.assertRaisesRegex(LookupError, 'batch not found'):
            freeze(batches, days, DAY, ['right-day', 'absent'])
        with self.assertRaisesRegex(ValueError, 'another day'):
            freeze(batches, days, DAY, ['right-day', 'wrong-day'])
        with self.assertRaisesRegex(ValueError, 'selected twice'):
            freeze(batches, days, DAY, ['right-day', 'right-day'])
        self.assertEqual(connection.execute('SELECT count(*) FROM frozen_days').fetchone()[0], 0)
        self.assertEqual(connection.execute('SELECT count(*) FROM issued_packages').fetchone()[0], 0)

    def test_freeze_stability(self):
        source, batches, days, register = self.source()
        original = self.sample_day(batches, days)
        wire = export(days, register, original.day_id)
        submit(batches, 'later', 'S1', DAY, [reading(11, 5)])
        expanded = freeze(batches, days, DAY, ['batch', 'later'])
        self.assertEqual(expanded.totals.litres, 31)
        self.assertNotEqual(original.day_id, expanded.day_id)
        source.close()
        _, batches, days, register = self.source()
        self.assertEqual(load_frozen_day(days, original.day_id), original)
        self.assertEqual(freeze(batches, days, DAY, ['batch']), original)
        self.assertEqual(export(days, register, original.day_id), wire)

    def test_zero_and_missing(self):
        source, batches, days, register = self.source()
        submit(batches, 'zero', 'S1', DAY, [reading(7, 2), reading(None, 3), reading(0, 4), reading(13, 5)])
        day = freeze(batches, days, DAY, ['zero'])
        wire = export(days, register, day.day_id)
        source.close()
        ledger = self.ledger()
        accept(ledger, wire)
        book = read(ledger, day_id=day.day_id)
        self.assertEqual([row.litres for row in book.base.rows], [7, None, 0, 13])
        self.assertEqual([row.source.position for row in book.base.rows], [1, 2, 3, 4])
        self.assertEqual([row.source.line for row in book.base.rows], [2, 3, 4, 5])
        self.assertEqual(book.totals.current_litres, 20)

    def test_exact_integer(self):
        source, batches, days, register = self.source()
        huge = 10**4500 + 39
        submit(batches, 'large', 'S1', DAY, [reading(huge, 2), reading(7, 3)])
        self.assertTrue(batches.get_batch('large').rows[0].litres == huge)
        day = freeze(batches, days, DAY, ['large'])
        self.assertTrue(day.totals.litres == huge + 7)
        base_wire = export(days, register, day.day_id)
        amend(days, register, 'negative', day.day_id, 'S1', -huge)
        correction_wire = export(days, register, 'negative')
        source.close()
        ledger = self.ledger()
        accept(ledger, base_wire)
        accept(ledger, correction_wire)
        ledger.connection.close()
        book = read(self.ledger(), day_id=day.day_id)
        self.assertTrue(book.base.rows[0].litres == huge)
        self.assertTrue(book.base.totals.litres == huge + 7)
        self.assertTrue(book.journal[0].delta == -huge)
        self.assertEqual(book.totals.current_litres, 7)

    def test_init_reuse(self):
        arguments = ['init', '--source', str(self.source_path), '--ledger', str(self.ledger_path)]
        self.assertEqual(main(arguments), 0)
        source, batches, days, register = self.source()
        ledger = self.ledger()
        for connection, note in [(source, 'source note'), (ledger.connection, 'ledger note')]:
            with connection:
                connection.execute('CREATE TABLE operator_notes (note TEXT)')
                connection.execute('INSERT INTO operator_notes VALUES (?)', (note,))
        day = self.sample_day(batches, days)
        import_receipt = batches.get_receipt('batch')
        base = export(days, register, day.day_id)
        amend(days, register, 'C', day.day_id, 'S1', 5)
        correction = export(days, register, 'C')
        accept(ledger, base)
        receipt = accept(ledger, correction)
        original = read(ledger, day_id=day.day_id)
        source.close()
        ledger.connection.close()
        self.assertEqual(main(arguments), 0)
        self.assertEqual(main(arguments), 0)
        source, batches, days, register = self.source()
        ledger = self.ledger()
        self.assertEqual(source.execute('SELECT note FROM operator_notes').fetchone()[0], 'source note')
        self.assertEqual(ledger.connection.execute('SELECT note FROM operator_notes').fetchone()[0], 'ledger note')
        self.assertEqual(batches.get_receipt('batch'), import_receipt)
        self.assertEqual(load_frozen_day(days, day.day_id), day)
        self.assertEqual(export(days, register, 'C'), correction)
        self.assertEqual(read(ledger, package_id='C'), receipt)
        self.assertEqual(read(ledger, day_id=day.day_id), original)
        self.assertEqual(original.totals.current_litres, 25)

    def test_invalid_row_rejects_batch(self):
        source, batches, _, _ = self.source()
        with self.assertRaisesRegex(ValueError, 'unit must be L'):
            submit(batches, 'invalid', 'S1', DAY, [reading(7, 2), reading(13, 3, unit='mL')])
        source.close()
        source, batches, _, _ = self.source()
        self.assertIsNone(batches.get_batch('invalid'))
        self.assertIsNone(batches.get_receipt('invalid'))
        self.assertEqual(source.execute('SELECT count(*) FROM reading_rows').fetchone()[0], 0)
        batch, receipt = submit(batches, 'invalid', 'S1', DAY, [reading(7, 2), reading(13, 3)])
        self.assertEqual([row.litres for row in batch.rows], [7, 13])
        self.assertEqual(receipt.row_count, 2)

    def test_conflicting_batch(self):
        _, batches, _, _ = self.source()
        original, receipt = submit(batches, 'batch', 'S1', DAY, [reading(7, 2)])
        with self.assertRaisesRegex(ValueError, 'conflicting batch'):
            submit(batches, 'batch', 'S1', DAY, [reading(7, 3)])
        self.assertEqual(batches.get_batch('batch'), original)
        self.assertEqual(batches.get_receipt('batch'), receipt)

    def test_base_checks_arithmetic_and_provenance(self):
        _, batches, days, register = self.source()
        day = self.sample_day(batches, days)
        wire = export(days, register, day.day_id)
        ledger = self.ledger()
        bad_total = json.loads(wire)
        bad_total['data']['totals']['by_station']['S1'] = '21'
        with self.assertRaisesRegex(ValueError, 'totals disagree'):
            accept(ledger, json.dumps(bad_total))
        bad_source = json.loads(wire)
        bad_source['data']['members'][0]['rows'][1]['source']['position'] = 3
        with self.assertRaisesRegex(ValueError, 'position disagrees'):
            accept(ledger, json.dumps(bad_source))
        self.assertIsNone(read(ledger, day_id=day.day_id))
        self.assertIsNone(read(ledger, package_id=day.day_id))
        self.assertEqual(accept(ledger, wire).outcome, 'base_accepted')

    def test_failed_base_publication_can_retry(self):
        _, batches, days, register = self.source()
        day = self.sample_day(batches, days)
        wire = export(days, register, day.day_id)
        ledger = self.ledger()

        def fail(receipt):
            self.assertEqual(receipt.outcome, 'base_accepted')
            raise RuntimeError('commit checkpoint')

        with self.assertRaisesRegex(RuntimeError, 'commit checkpoint'):
            accept(ledger, wire, before_commit=fail)
        ledger.connection.close()
        ledger = self.ledger()
        self.assertIsNone(read(ledger, day_id=day.day_id))
        self.assertIsNone(read(ledger, package_id=day.day_id))
        self.assertEqual(accept(ledger, wire).outcome, 'base_accepted')
