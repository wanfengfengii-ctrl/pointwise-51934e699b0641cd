import json

from audit.daybook import read
from audit.handover import prepare_handover
from issue.corrections import amend
from issue.packages import decode_package
from issue.register import export
from issue.withdrawals import withdraw
from receive.intake import accept
from tests.test_base import WarehouseCase


class WithdrawalTests(WarehouseCase):
    def release(self, path=None, identities=('C', 'D', 'R'), target='C'):
        source, batches, days, register = self.source(path)
        day = self.sample_day(batches, days)
        correction, independent, withdrawal = identities
        amend(days, register, correction, day.day_id, 'S1', 5)
        amend(days, register, independent, day.day_id, 'S1', 4)
        target_id = correction if target == 'C' else independent
        withdraw(register, withdrawal, target_id)
        packages = {
            role: export(days, register, package_id)
            for role, package_id in zip(('C', 'D', 'R'), identities)
        }
        packages['base'] = export(days, register, day.day_id)
        source.close()
        return day.day_id, packages

    def release_equal(self, filename, target_id):
        path = self.root / filename
        source, batches, days, register = self.source(path)
        day = self.sample_day(batches, days)
        for correction_id in ('C1', 'C2'):
            amend(days, register, correction_id, day.day_id, 'S1', 5)
        withdraw(register, 'R', target_id)
        packages = {package_id: export(days, register, package_id) for package_id in ('C1', 'C2', 'R')}
        packages['base'] = export(days, register, day.day_id)
        source.close()
        return path, day.day_id, packages

    def book(self, ledger, day_id):
        return read(ledger, day_id=day_id)

    def journal(self, ledger, day_id):
        return [(line.package_id, line.correction_id, line.delta) for line in self.book(ledger, day_id).journal]

    def test_withdraw_before_correction(self):
        day_id, packages = self.release()
        ledger = self.ledger()
        accept(ledger, packages['base'])
        withdrawal = accept(ledger, packages['R'])
        self.assertEqual(self.book(ledger, day_id).totals.current_litres, 20)
        self.assertEqual(self.journal(ledger, day_id), [])
        independent = accept(ledger, packages['D'])
        self.assertEqual(self.book(ledger, day_id).totals.current_litres, 24)
        late = accept(ledger, packages['C'])
        self.assertEqual(self.book(ledger, day_id).totals.current_litres, 24, 'LATE_CORRECTION_POSTED')
        self.assertEqual(self.journal(ledger, day_id), [('D', 'D', 4)])
        self.assertEqual((withdrawal.outcome, withdrawal.correction_id, withdrawal.journal_sequence),
                         ('withdrawal_without_entry', 'C', None))
        self.assertEqual((late.outcome, late.correction_id, late.journal_sequence),
                         ('correction_withdrawn', 'C', None))
        ledger.connection.close()
        ledger = self.ledger()
        report = prepare_handover(ledger, day_id, ['R', 'D', 'C'])
        self.assertEqual(report.receipts, (withdrawal, independent, late))
        self.assertEqual(report.daybook.totals.by_station['S1'].current_litres, 24)

    def test_reopen_before_late(self):
        day_id, packages = self.release()
        ledger = self.ledger()
        accept(ledger, packages['base'])
        withdrawal = accept(ledger, packages['R'])
        ledger.connection.close()
        ledger = self.ledger()
        self.assertEqual(read(ledger, package_id='R'), withdrawal)
        self.assertIsNone(read(ledger, package_id='C'))
        self.assertEqual(self.book(ledger, day_id).totals.current_litres, 20)
        accept(ledger, packages['D'])
        correction = accept(ledger, packages['C'])
        self.assertEqual(correction.outcome, 'correction_withdrawn')
        self.assertEqual(accept(ledger, packages['C']), correction)
        self.assertEqual(self.journal(ledger, day_id), [('D', 'D', 4)])
        self.assertEqual(self.book(ledger, day_id).totals.current_litres, 24)

    def test_withdraw_after_correction(self):
        day_id, packages = self.release()
        ledger = self.ledger()
        accept(ledger, packages['base'])
        receipts = []
        totals = []
        for package_id in ('C', 'D', 'R'):
            receipts.append(accept(ledger, packages[package_id]))
            totals.append(self.book(ledger, day_id).totals.current_litres)
        self.assertEqual(totals, [25, 29, 24])
        self.assertEqual(self.journal(ledger, day_id), [('C', 'C', 5), ('D', 'D', 4), ('R', 'C', -5)])
        self.assertEqual(receipts[-1].outcome, 'withdrawal_reversed')
        self.assertEqual(self.book(ledger, day_id).base.totals.litres, 20)
        ledger.connection.close()
        report = prepare_handover(self.ledger(), day_id, ['C', 'D', 'R'])
        self.assertEqual(report.receipts, tuple(receipts))
        self.assertEqual(report.daybook.totals.current_litres, 24)

    def test_journal_order(self):
        day_id, packages = self.release(identities=('Z-C', 'M-D', 'A-R'))
        ledger = self.ledger()
        accept(ledger, packages['base'])
        for role in ('C', 'D', 'R'):
            accept(ledger, packages[role])
        ledger.connection.close()
        ledger = self.ledger()
        self.assertEqual(self.journal(ledger, day_id), [('Z-C', 'Z-C', 5), ('M-D', 'M-D', 4), ('A-R', 'Z-C', -5)])
        self.assertEqual(self.book(ledger, day_id).totals.current_litres, 24)
        lines = self.book(ledger, day_id).journal
        self.assertEqual([line.sequence for line in lines], sorted(line.sequence for line in lines))
        for line in lines:
            self.assertEqual(read(ledger, package_id=line.package_id).journal_sequence, line.sequence)

    def test_withdraw_rollback_and_retry(self):
        day_id, packages = self.release()
        ledger = self.ledger()
        accept(ledger, packages['base'])
        accept(ledger, packages['C'])

        def fail(receipt):
            self.assertEqual(receipt.outcome, 'withdrawal_reversed')
            self.assertIsNotNone(receipt.journal_sequence)
            raise RuntimeError('commit checkpoint')

        with self.assertRaisesRegex(RuntimeError, 'commit checkpoint'):
            accept(ledger, packages['R'], before_commit=fail)
        ledger.connection.close()
        ledger = self.ledger()
        self.assertEqual(self.book(ledger, day_id).totals.current_litres, 25)
        self.assertEqual(self.journal(ledger, day_id), [('C', 'C', 5)])
        self.assertIsNone(read(ledger, package_id='R'))
        self.assertIsNone(ledger.find_accepted_withdrawal('C'))
        receipt = accept(ledger, packages['R'])
        self.assertEqual(self.book(ledger, day_id).totals.current_litres, 20)
        self.assertEqual(self.journal(ledger, day_id), [('C', 'C', 5), ('R', 'C', -5)])
        self.assertEqual(read(ledger, package_id='R'), receipt)

    def test_commit_visibility_and_new_history_on_retry(self):
        day_id, packages = self.release()
        ledger = self.ledger()
        accept(ledger, packages['base'])
        observer = self.ledger()

        def fail(receipt):
            self.assertEqual(receipt.outcome, 'withdrawal_without_entry')
            self.assertIsNone(receipt.journal_sequence)
            self.assertIsNone(read(observer, package_id='R'))
            self.assertEqual(self.book(observer, day_id).totals.current_litres, 20)
            raise RuntimeError('commit checkpoint')

        with self.assertRaisesRegex(RuntimeError, 'commit checkpoint'):
            accept(ledger, packages['R'], before_commit=fail)
        ledger.connection.close()
        observer.connection.close()
        ledger = self.ledger()
        self.assertIsNone(read(ledger, package_id='R'))
        self.assertIsNone(ledger.find_accepted_withdrawal('C'))
        self.assertEqual(self.journal(ledger, day_id), [])
        self.assertEqual(self.book(ledger, day_id).totals.current_litres, 20)
        self.assertEqual(accept(ledger, packages['C']).outcome, 'correction_posted')
        self.assertEqual(accept(ledger, packages['R']).outcome, 'withdrawal_reversed')
        self.assertEqual(self.journal(ledger, day_id), [('C', 'C', 5), ('R', 'C', -5)])

    def test_retry_after_lost_reply(self):
        day_id, packages = self.release()
        ledger = self.ledger()
        accept(ledger, packages['base'])
        accept(ledger, packages['C'])
        accept(ledger, packages['R'])  # The caller loses the returned receipt after commit.
        ledger.connection.close()
        ledger = self.ledger()
        stored = read(ledger, package_id='R')
        self.assertIsNotNone(stored)
        self.assertEqual(stored.outcome, 'withdrawal_reversed')
        self.assertEqual(accept(ledger, packages['R']), stored)
        self.assertEqual(self.journal(ledger, day_id), [('C', 'C', 5), ('R', 'C', -5)])
        self.assertEqual(self.book(ledger, day_id).totals.current_litres, 20)

    def test_conflicting_redelivery(self):
        day_id, packages = self.release()
        alternate_day, alternate = self.release(self.root / 'alternate.sqlite', target='D')
        self.assertEqual(alternate_day, day_id)
        ledger = self.ledger()
        accept(ledger, packages['base'])
        accept(ledger, packages['C'])
        original = accept(ledger, packages['R'])
        before = self.book(ledger, day_id)
        with self.assertRaisesRegex(ValueError, 'conflicting delivery'):
            accept(ledger, alternate['R'])
        self.assertEqual(self.book(ledger, day_id), before)
        self.assertEqual(read(ledger, package_id='R'), original)

    def test_preserve_original_after_conflict_and_reopen(self):
        day_id, packages = self.release()
        _, alternate = self.release(self.root / 'alternate.sqlite', target='D')
        ledger = self.ledger()
        accept(ledger, packages['base'])
        accept(ledger, packages['D'])
        original = accept(ledger, packages['R'])
        with self.assertRaisesRegex(ValueError, 'conflicting delivery'):
            accept(ledger, alternate['R'])
        ledger.connection.close()
        ledger = self.ledger()
        self.assertEqual(read(ledger, package_id='R'), original)
        self.assertEqual(accept(ledger, packages['R']), original)
        self.assertEqual(self.journal(ledger, day_id), [('D', 'D', 4)])
        self.assertEqual(self.book(ledger, day_id).totals.current_litres, 24)

    def test_equal_amount_corrections(self):
        _, day_id, packages = self.release_equal('equal.sqlite', 'C1')
        ledger = self.ledger()
        accept(ledger, packages['base'])
        withdrawal = accept(ledger, packages['R'])
        kept = accept(ledger, packages['C2'])
        blocked = accept(ledger, packages['C1'])
        self.assertEqual(self.journal(ledger, day_id), [('C2', 'C2', 5)])
        self.assertEqual(self.book(ledger, day_id).totals.current_litres, 25)
        self.assertEqual(withdrawal.correction_id, 'C1')
        self.assertEqual(kept.outcome, 'correction_posted')
        self.assertEqual(blocked.outcome, 'correction_withdrawn')
        self.assertIsNone(blocked.journal_sequence)
        self.assertEqual(read(ledger, package_id='C1'), blocked)

    def test_frozen_target(self):
        observed = []
        for target in ('C1', 'C2'):
            path, day_id, packages = self.release_equal(f'issue-{target}.sqlite', target)
            reopened, _, days, register = self.source(path)
            self.assertEqual(export(days, register, 'R'), packages['R'])
            reopened.close()
            package = decode_package(packages['R'])
            self.assertEqual((package.day_id, package.station, package.delta, package.target_id),
                             (day_id, 'S1', -5, target))
            ledger = self.ledger(self.root / f'ledger-{target}.sqlite')
            accept(ledger, packages['base'])
            for package_id in ('R', 'C2', 'C1'):
                accept(ledger, packages[package_id])
            observed.append((packages['R'], self.journal(ledger, day_id)))
        self.assertNotEqual(observed[0][0], observed[1][0])
        self.assertEqual(observed[0][1], [('C2', 'C2', 5)])
        self.assertEqual(observed[1][1], [('C1', 'C1', 5)])

    def test_issue_conflict(self):
        _, batches, days, register = self.source()
        day = self.sample_day(batches, days)
        for correction_id in ('C1', 'C2'):
            amend(days, register, correction_id, day.day_id, 'S1', 5)
        with self.assertRaisesRegex(LookupError, 'issued correction not found'):
            withdraw(register, 'missing', 'not-issued')
        self.assertEqual(withdraw(register, 'R', 'C1'), 'R')
        self.assertEqual(withdraw(register, 'R', 'C1'), 'R')
        wire = export(days, register, 'R')
        with self.assertRaisesRegex(ValueError, 'conflicting issue'):
            withdraw(register, 'R', 'C2')
        with self.assertRaisesRegex(ValueError, 'already has a withdrawal'):
            withdraw(register, 'R2', 'C1')
        self.assertIsNone(register.find('R2'))
        self.assertIsNone(register.find('missing'))
        self.assertIsNone(register.find_withdrawal('C2'))
        self.assertEqual(export(days, register, 'R'), wire)

    def test_mismatched_late_correction_is_rejected(self):
        day_id, packages = self.release()
        ledger = self.ledger()
        accept(ledger, packages['base'])
        withdrawal = accept(ledger, packages['R'])
        wrong = json.loads(packages['C'])
        wrong['data']['delta_litres'] = '6'
        with self.assertRaisesRegex(ValueError, 'binding disagrees'):
            accept(ledger, json.dumps(wrong))
        self.assertIsNone(read(ledger, package_id='C'))
        self.assertEqual(read(ledger, package_id='R'), withdrawal)
        self.assertEqual(accept(ledger, packages['C']).outcome, 'correction_withdrawn')
        self.assertEqual(self.book(ledger, day_id).totals.current_litres, 20)
        self.assertEqual(self.journal(ledger, day_id), [])
