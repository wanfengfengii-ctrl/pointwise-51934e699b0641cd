from audit.daybook import read
from freezing.days import freeze
from intake.batches import submit
from issue.corrections import amend
from issue.packages import decode_package
from issue.register import export
from issue.withdrawals import withdraw
from receive.intake import accept
from tests.test_base import DAY, WarehouseCase, reading


class CorrectionTests(WarehouseCase):
    def correction_wires(self):
        source, batches, days, register = self.source()
        day = self.sample_day(batches, days)
        base = export(days, register, day.day_id)
        amend(days, register, 'C', day.day_id, 'S1', 5)
        correction = export(days, register, 'C')
        source.close()
        return day.day_id, base, correction

    def test_correction_once(self):
        day_id, base, correction = self.correction_wires()
        ledger = self.ledger()
        accept(ledger, base)
        first = accept(ledger, correction)
        self.assertEqual(first.outcome, 'correction_posted')
        self.assertEqual(accept(ledger, correction), first)
        ledger.connection.close()
        ledger = self.ledger()
        book = read(ledger, day_id=day_id)
        self.assertEqual(book.base.totals.litres, 20)
        self.assertEqual(book.totals.current_litres, 25)
        self.assertEqual([(line.package_id, line.delta) for line in book.journal], [('C', 5)])
        self.assertEqual(book.journal[0].sequence, first.journal_sequence)
        self.assertEqual(read(ledger, package_id='C'), first)

    def test_correction_before_base(self):
        day_id, base, correction = self.correction_wires()
        ledger = self.ledger()
        with self.assertRaisesRegex(LookupError, 'base not accepted'):
            accept(ledger, correction)
        self.assertIsNone(read(ledger, day_id=day_id))
        self.assertIsNone(read(ledger, package_id='C'))
        self.assertEqual(ledger.list_journal(day_id), ())
        accept(ledger, base)
        receipt = accept(ledger, correction)
        self.assertEqual(accept(ledger, correction), receipt)
        self.assertEqual(read(ledger, day_id=day_id).totals.current_litres, 25)
        self.assertEqual(len(ledger.list_journal(day_id)), 1)

    def test_source_issue(self):
        source, batches, days, register = self.source()
        day = self.sample_day(batches, days)
        self.assertEqual(amend(days, register, 'C', day.day_id, 'S1', 5), 'C')
        self.assertEqual(amend(days, register, 'C', day.day_id, 'S1', 5), 'C')
        issued = export(days, register, 'C')
        with self.assertRaisesRegex(ValueError, 'conflicting issue'):
            amend(days, register, 'C', day.day_id, 'S1', 6)
        with self.assertRaisesRegex(ValueError, 'not in frozen day'):
            amend(days, register, 'wrong-station', day.day_id, 'S2', 5)
        with self.assertRaisesRegex(LookupError, 'frozen day not found'):
            amend(days, register, 'unknown-day', 'day:' + '0' * 64, 'S1', 5)
        for unissued in ('wrong-station', 'unknown-day'):
            with self.assertRaisesRegex(LookupError, 'package not found'):
                export(days, register, unissued)
        source.close()
        _, _, days, register = self.source()
        self.assertEqual(export(days, register, 'C'), issued)
        package = decode_package(issued)
        self.assertEqual((package.day_id, package.station, package.delta), (day.day_id, 'S1', 5))

    def test_station_attribution(self):
        source, batches, days, register = self.source()
        submit(batches, 'A-batch', 'A', DAY, [reading(7, 2, 'a.csv')])
        submit(batches, 'B-batch', 'B', DAY, [reading(13, 3, 'b.csv')])
        day = freeze(batches, days, DAY, ['A-batch', 'B-batch'])
        base = export(days, register, day.day_id)
        amend(days, register, 'C', day.day_id, 'B', 5)
        correction = export(days, register, 'C')
        self.assertEqual(decode_package(correction).station, 'B')
        source.close()
        ledger = self.ledger()
        accept(ledger, base)
        first = accept(ledger, correction)
        self.assertEqual(accept(ledger, correction), first)
        ledger.connection.close()
        ledger = self.ledger()
        book = read(ledger, day_id=day.day_id)
        self.assertEqual({key: value.current_litres for key, value in book.totals.by_station.items()}, {'A': 7, 'B': 18})
        self.assertEqual(book.totals.current_litres, 25)
        self.assertEqual([(line.station, line.package_id, line.delta) for line in book.journal], [('B', 'C', 5)])
        self.assertEqual(read(ledger, package_id='C'), first)

    def test_exact_base(self):
        source, batches, days, register = self.source()
        target = self.sample_day(batches, days)
        submit(batches, 'other', 'S1', DAY, [reading(11, 5)])
        other = freeze(batches, days, DAY, ['other'])
        amend(days, register, 'C', target.day_id, 'S1', 5)
        withdraw(register, 'R', 'C')
        target_base = export(days, register, target.day_id)
        other_base = export(days, register, other.day_id)
        correction = export(days, register, 'C')
        withdrawal = export(days, register, 'R')
        source.close()
        ledger = self.ledger()
        accept(ledger, other_base)
        for package_id, wire in [('C', correction), ('R', withdrawal)]:
            with self.assertRaisesRegex(LookupError, 'base not accepted'):
                accept(ledger, wire)
            self.assertIsNone(read(ledger, package_id=package_id))
        self.assertEqual(read(ledger, day_id=other.day_id).totals.current_litres, 11)
        self.assertEqual(ledger.list_journal(target.day_id), ())
        accept(ledger, target_base)
        receipt = accept(ledger, correction)
        self.assertEqual(read(ledger, package_id='C'), receipt)
        target_book = read(ledger, day_id=target.day_id)
        self.assertEqual(target_book.totals.current_litres, 25)
        self.assertEqual([(line.package_id, line.delta) for line in target_book.journal], [('C', 5)])
        self.assertEqual(read(ledger, day_id=other.day_id).journal, ())
