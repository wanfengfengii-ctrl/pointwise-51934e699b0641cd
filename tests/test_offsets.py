import json
import sqlite3
import unittest

from audit.daybook import read, read_offset
from audit.handover import prepare_handover
from freezing.days import freeze
from intake.batches import submit
from issue.register import export, issue_offset
from issue.packages import decode_package
from issue.withdrawals import withdraw
from receive.intake import accept, post_offset
from tests.test_base import DAY, WarehouseCase, reading

STATIONS = ('甲', '乙', '丙', '丁')


class OffsetCase(WarehouseCase):
    def four_stations(self, batches, days, day=DAY):
        for index, station in enumerate(STATIONS):
            submit(batches, f'b-{station}', station, day,
                   [reading(5, 2 + index, f'{station}.csv')])
        return freeze(batches, days, day, [f'b-{station}' for station in STATIONS])

    def issue(self, orders, path=None):
        """Issue a four-station frozen day plus the given offset orders."""
        source, batches, days, register = self.source(path)
        day = self.four_stations(batches, days)
        base = export(days, register, day.day_id)
        wires = {
            offset_id: issue_offset(days, register, offset_id, day.day_id, stations, litres)
            for offset_id, stations, litres in orders
        }
        source.close()
        return day.day_id, base, wires

    def fresh_ledger(self, name='ledger.sqlite'):
        return self.ledger(self.root / name)

    def by_station(self, ledger, day_id):
        return {station: balance.current_litres
                for station, balance in read(ledger, day_id=day_id).totals.by_station.items()}


class OffsetIssueTests(OffsetCase):
    def test_wire_is_version_two_with_sorted_stations(self):
        source, batches, days, register = self.source()
        day = self.four_stations(batches, days)
        wire = issue_offset(days, register, 'P', day.day_id, list(reversed(STATIONS)), 5)
        document = json.loads(wire)
        self.assertEqual(document['version'], 2)
        self.assertEqual(document['kind'], 'offset')
        self.assertEqual(document['data'], {'stations': sorted(STATIONS), 'litres': '5'})
        self.assertEqual(issue_offset(days, register, 'P', day.day_id, STATIONS, 5), wire)
        self.assertEqual(issue_offset(days, register, 'P', day.day_id, tuple(STATIONS), 5), wire)
        self.assertEqual(export(days, register, 'P'), wire)
        with self.assertRaisesRegex(ValueError, 'conflicting issue'):
            issue_offset(days, register, 'P', day.day_id, ('甲', '乙'), 5)
        with self.assertRaisesRegex(ValueError, 'conflicting issue'):
            issue_offset(days, register, 'P', day.day_id, STATIONS, 6)

    def test_invalid_parameters(self):
        source, batches, days, register = self.source()
        day = self.four_stations(batches, days)
        for stations, litres in (([], 5), (('甲',), 0), (('甲',), -1), (('甲',), True),
                                 ((5,), 5), (('甲', '甲'), 5), (('甲', '外站'), 5)):
            with self.assertRaises(ValueError):
                issue_offset(days, register, 'B', day.day_id, stations, litres)
        self.assertIsNone(register.find('B'))
        with self.assertRaisesRegex(LookupError, 'frozen day not found'):
            issue_offset(days, register, 'B', 'day:' + '0' * 64, ('甲',), 5)
        with self.assertRaisesRegex(ValueError, 'reserved day prefix'):
            issue_offset(days, register, 'day:bad', day.day_id, ('甲',), 5)

    def test_share_operation_number_with_corrections(self):
        source, batches, days, register = self.source()
        day = self.four_stations(batches, days)
        from issue.corrections import amend
        amend(days, register, 'C', day.day_id, '甲', 5)
        with self.assertRaisesRegex(ValueError, 'conflicting issue'):
            issue_offset(days, register, 'C', day.day_id, ('甲',), 5)
        wire = issue_offset(days, register, 'P', day.day_id, ('甲',), 5)
        with self.assertRaisesRegex(ValueError, 'conflicting issue'):
            amend(days, register, 'P', day.day_id, '甲', 5)
        self.assertEqual(decode_package(wire).kind, 'offset')
        self.assertEqual(export(days, register, 'P'), wire)


class OffsetAcceptTests(OffsetCase):
    def test_accept_is_pending_and_does_not_deduct(self):
        day_id, base, wires = self.issue([('P', STATIONS, 5), ('Q', ('甲', '乙'), 5),
                                          ('R', ('甲', '乙'), 5)])
        ledger = self.fresh_ledger()
        accept(ledger, base)
        receipts = [accept(ledger, wires[key]) for key in ('P', 'Q', 'R')]
        for receipt in receipts:
            self.assertEqual((receipt.kind, receipt.outcome, receipt.correction_id,
                              receipt.journal_sequence), ('offset', 'offset_pending', None, None))
        book = read(ledger, day_id=day_id)
        self.assertEqual(book.totals.current_litres, 20)
        self.assertEqual(book.offsets, ())
        self.assertEqual(book.journal, ())
        self.assertEqual(read(ledger, package_id='P'), receipts[0])
        self.assertEqual(read_offset(ledger, 'P'), (receipts[0], None))
        ledger.connection.close()
        ledger = self.fresh_ledger()
        self.assertEqual(accept(ledger, wires['P']), receipts[0])
        self.assertEqual(accept(ledger, json.dumps(json.loads(wires['P']), indent=2)), receipts[0])
        permuted = json.loads(wires['P'])
        permuted['data']['stations'] = list(reversed(permuted['data']['stations']))
        self.assertEqual(accept(ledger, json.dumps(permuted)), receipts[0])

    def test_conflicting_redelivery(self):
        day_id, base, wires = self.issue([('P', STATIONS, 5)])
        alternate_day, _, alternate = self.issue(
            [('P', ('甲', '乙'), 5)], path=self.root / 'alternate.sqlite')
        self.assertEqual(alternate_day, day_id)
        ledger = self.fresh_ledger()
        accept(ledger, base)
        original = accept(ledger, wires['P'])
        before = read(ledger, day_id=day_id)
        with self.assertRaisesRegex(ValueError, 'conflicting delivery'):
            accept(ledger, alternate['P'])
        self.assertEqual(read(ledger, day_id=day_id), before)
        self.assertEqual(read(ledger, package_id='P'), original)

    def test_reject_bad_wire(self):
        day_id, base, wires = self.issue([('P', STATIONS, 5)])
        ledger = self.fresh_ledger()
        accept(ledger, base)
        document = json.loads(wires['P'])
        bad_version = dict(document, version=1)
        with self.assertRaisesRegex(ValueError, 'unsupported wire version'):
            accept(ledger, json.dumps(bad_version))
        duplicate = wires['P'].replace('"stations":', '"stations":"dup","stations":', 1)
        with self.assertRaisesRegex(ValueError, 'duplicate JSON field'):
            accept(ledger, duplicate)
        repeated = json.loads(wires['P'])
        repeated['data']['stations'] = ['甲', '甲']
        with self.assertRaisesRegex(ValueError, 'duplicate offset station'):
            accept(ledger, json.dumps(repeated))
        wrong_type = json.loads(wires['P'])
        wrong_type['data']['litres'] = 5
        with self.assertRaises(ValueError):
            accept(ledger, json.dumps(wrong_type))
        off_day = json.loads(wires['P'])
        off_day['day_id'] = 'day:' + '0' * 64
        with self.assertRaisesRegex(LookupError, 'base not accepted'):
            accept(ledger, json.dumps(off_day))
        self.assertIsNone(read(ledger, package_id='P'))
        self.assertEqual(read(ledger, day_id=day_id).totals.current_litres, 20)

    def test_requires_local_base(self):
        day_id, base, wires = self.issue([('P', STATIONS, 5)])
        ledger = self.fresh_ledger()
        with self.assertRaisesRegex(LookupError, 'base not accepted'):
            accept(ledger, wires['P'])
        self.assertIsNone(read(ledger, package_id='P'))
        self.assertIsNone(read(ledger, day_id=day_id))

    def test_joint_feasibility_rejects_third_order(self):
        day_id, base, wires = self.issue([
            ('P', ('甲', '乙'), 5), ('Q', ('甲', '乙'), 5), ('R', ('甲', '乙'), 5)])
        ledger = self.fresh_ledger()
        accept(ledger, base)
        accept(ledger, wires['P'])
        accept(ledger, wires['Q'])
        with self.assertRaisesRegex(ValueError, 'jointly fulfilled'):
            accept(ledger, wires['R'])
        self.assertIsNone(read(ledger, package_id='R'))
        self.assertEqual(read(ledger, day_id=day_id).offsets, ())
        self.assertEqual(read(ledger, day_id=day_id).totals.current_litres, 20)

    def test_accept_rollback_on_callback_failure(self):
        day_id, base, wires = self.issue([('P', STATIONS, 5)])

        def fail(receipt):
            self.assertEqual(receipt.outcome, 'offset_pending')
            raise RuntimeError('commit checkpoint')

        ledger = self.fresh_ledger()
        accept(ledger, base)
        with self.assertRaisesRegex(RuntimeError, 'commit checkpoint'):
            accept(ledger, wires['P'], before_commit=fail)
        ledger.connection.close()
        ledger = self.fresh_ledger()
        self.assertIsNone(read(ledger, package_id='P'))
        self.assertIsNone(read_offset(ledger, 'P'))
        self.assertEqual(accept(ledger, wires['P']).outcome, 'offset_pending')


class OffsetPostTests(OffsetCase):
    def test_post_p_first_uses_province_stations(self):
        day_id, base, wires = self.issue([('P', STATIONS, 5), ('Q', ('甲', '乙'), 5),
                                          ('R', ('甲', '乙'), 5)])
        ledger = self.fresh_ledger()
        accept(ledger, base)
        for key in ('P', 'Q', 'R'):
            accept(ledger, wires[key])
        result = post_offset(ledger, 'P')
        self.assertEqual(result.package_id, 'P')
        self.assertEqual(result.day_id, day_id)
        # Q and R each need 5 L from 甲乙, so P must fit entirely in 丙丁.
        self.assertTrue(set(result.by_station) <= {'丙', '丁'})
        self.assertEqual(sum(result.by_station.values()), 5)
        book = read(ledger, day_id=day_id)
        self.assertEqual(book.offsets, (result,))
        self.assertEqual(book.totals.current_litres, 15)
        self.assertEqual(book.totals.by_station['甲'].current_litres, 5)
        self.assertEqual(book.totals.by_station['乙'].current_litres, 5)
        self.assertEqual(sum(book.totals.by_station[s].current_litres for s in ('丙', '丁')), 5)
        for key in ('Q', 'R'):
            post_offset(ledger, key)
        final = self.by_station(ledger, day_id)
        self.assertEqual((final['甲'], final['乙']), (0, 0))
        self.assertEqual(final['丙'] + final['丁'], 5)
        self.assertEqual(read_offset(ledger, 'P'), (read(ledger, package_id='P'), result))

    def test_multistation_split(self):
        day_id, base, wires = self.issue([('S', ('丙', '丁'), 7)])
        ledger = self.fresh_ledger()
        accept(ledger, base)
        accept(ledger, wires['S'])
        result = post_offset(ledger, 'S')
        self.assertEqual(sum(result.by_station.values()), 7)
        self.assertTrue(set(result.by_station) <= {'丙', '丁'})
        self.assertGreater(len(result.by_station), 1)
        self.assertEqual(read(ledger, day_id=day_id).totals.current_litres, 13)

    def test_post_rejects_exhausted_station_and_keeps_posting(self):
        day_id, base, wires = self.issue([
            ('P', STATIONS, 5), ('X', ('丁',), 5), ('Y', ('丁',), 2)])
        ledger = self.fresh_ledger()
        accept(ledger, base)
        accept(ledger, wires['P'])
        result = post_offset(ledger, 'P')
        self.assertIn('丁', result.by_station)
        residual = 5 - result.by_station['丁']
        # A 5 L order confined to 丁 cannot fit once P has touched it.
        self.assertGreater(residual, 0)
        self.assertLess(residual, 5)
        with self.assertRaisesRegex(ValueError, 'jointly fulfilled'):
            accept(ledger, wires['X'])
        self.assertIsNone(read(ledger, package_id='X'))
        # A dedicated order for 丁's actual residual is accepted on the same ledger.
        self.assertEqual(accept(ledger, wires['Y']).outcome, 'offset_pending')
        # P's first receipt and split are unchanged.
        self.assertEqual(read_offset(ledger, 'P'), (read(ledger, package_id='P'), result))
        self.assertEqual(read(ledger, day_id=day_id).offsets, (result,))
        self.assertEqual(read(ledger, day_id=day_id).totals.current_litres, 15)

    def test_p_restricted_to_jia_yi_leaves_bing_ding_unusable(self):
        day_id, base, wires = self.issue([
            ('P', ('甲', '乙'), 5), ('Q', ('甲', '乙'), 5), ('R', ('甲', '乙'), 5)])
        ledger = self.fresh_ledger()
        accept(ledger, base)
        accept(ledger, wires['P'])
        accept(ledger, wires['Q'])
        # 丙丁 still hold 10 L, but none of it can serve the 甲乙-only orders.
        with self.assertRaisesRegex(ValueError, 'jointly fulfilled'):
            accept(ledger, wires['R'])
        self.assertIsNone(read(ledger, package_id='R'))
        self.assertEqual(read(ledger, day_id=day_id).totals.current_litres, 20)
        post_offset(ledger, 'P')
        post_offset(ledger, 'Q')
        self.assertEqual(self.by_station(ledger, day_id), {'甲': 0, '乙': 0, '丙': 5, '丁': 5})

    def test_post_rollback_and_retry(self):
        day_id, base, wires = self.issue([('S', ('丙', '丁'), 7)])
        ledger = self.fresh_ledger()
        receipt = accept(ledger, base)
        offset_receipt = accept(ledger, wires['S'])
        observed = []

        def fail(result):
            observed.append(result)
            self.assertEqual(read(ledger, day_id=day_id).totals.current_litres, 13)
            raise RuntimeError('lost reply')

        with self.assertRaisesRegex(RuntimeError, 'lost reply'):
            post_offset(ledger, 'S', before_commit=fail)
        ledger.connection.close()
        ledger = self.fresh_ledger()
        self.assertIsNone(read_offset(ledger, 'S')[1])
        self.assertEqual(read(ledger, day_id=day_id).totals.current_litres, 20)
        self.assertEqual(read(ledger, package_id='S'), offset_receipt)
        result = post_offset(ledger, 'S')
        self.assertEqual(sum(result.by_station.values()), 7)
        self.assertEqual(read(ledger, day_id=day_id).totals.current_litres, 13)
        # Simulate a lost reply: reopen and query by id.
        ledger.connection.close()
        ledger = self.fresh_ledger()
        self.assertEqual(read_offset(ledger, 'S'), (offset_receipt, result))
        self.assertEqual(post_offset(ledger, 'S'), result)
        self.assertEqual(read(ledger, day_id=day_id).totals.current_litres, 13)

    def test_missing_offset_lookup(self):
        day_id, base, wires = self.issue([('P', STATIONS, 5)])
        ledger = self.fresh_ledger()
        accept(ledger, base)
        with self.assertRaisesRegex(LookupError, 'offset not accepted'):
            post_offset(ledger, 'P')
        with self.assertRaisesRegex(LookupError, 'offset not accepted'):
            post_offset(ledger, 'absent')
        self.assertEqual(read(ledger, day_id=day_id).totals.current_litres, 20)

    def test_reopen_keeps_first_posting(self):
        day_id, base, wires = self.issue([('P', STATIONS, 5)])
        ledger = self.fresh_ledger()
        accept(ledger, base)
        accept(ledger, wires['P'])
        first = post_offset(ledger, 'P')
        ledger.connection.close()
        ledger = self.fresh_ledger()
        self.assertEqual(post_offset(ledger, 'P'), first)
        self.assertEqual(read_offset(ledger, 'P')[1], first)
        self.assertEqual(read(ledger, day_id=day_id).totals.current_litres, 15)


class OffsetAuditTests(OffsetCase):
    def test_read_offset_none_for_v1_and_unknown(self):
        day_id, base, wires = self.issue([('P', STATIONS, 5)])
        ledger = self.fresh_ledger()
        accept(ledger, base)
        self.assertIsNone(read_offset(ledger, day_id))
        self.assertIsNone(read_offset(ledger, 'missing'))
        accept(ledger, wires['P'])
        self.assertIsNone(read_offset(ledger, day_id))
        self.assertEqual(read_offset(ledger, 'P')[0].kind, 'offset')

    def test_full_day_reconciliation(self):
        day_id, base, wires = self.issue([('P', STATIONS, 5), ('Q', ('甲', '乙'), 5),
                                          ('R', ('甲', '乙'), 5)])
        ledger = self.fresh_ledger()
        accept(ledger, base)
        for key in ('P', 'Q', 'R'):
            accept(ledger, wires[key])
        for key in ('P', 'Q', 'R'):
            post_offset(ledger, key)
        book = read(ledger, day_id=day_id)
        self.assertEqual([result.package_id for result in book.offsets], ['P', 'Q', 'R'])
        deducted = {'甲': 0, '乙': 0, '丙': 0, '丁': 0}
        for result in book.offsets:
            for station, quantity in result.by_station.items():
                deducted[station] += quantity
        self.assertEqual(deducted, {
            station: 5 - book.totals.by_station[station].current_litres for station in STATIONS})
        for station in STATIONS:
            balance = book.totals.by_station[station]
            self.assertEqual(balance.base_litres, 5)
            self.assertEqual(balance.offset_litres, deducted[station])
            self.assertEqual(balance.current_litres, 5 - deducted[station])
        # The frozen basis is retained verbatim.
        self.assertEqual(book.base.totals.by_station, {station: 5 for station in STATIONS})
        self.assertEqual(book.base.totals.litres, 20)

    def test_correction_does_not_enlarge_offset_allowance(self):
        source, batches, days, register = self.source()
        day = self.four_stations(batches, days)
        base = export(days, register, day.day_id)
        from issue.corrections import amend
        amend(days, register, 'C', day.day_id, '甲', 5)
        correction = export(days, register, 'C')
        # Formal 甲 is 5 + 5 = 10 L, but the offset allowance stays at 5 L.
        over = issue_offset(days, register, 'X', day.day_id, ('甲',), 6)
        exact = issue_offset(days, register, 'Y', day.day_id, ('甲',), 5)
        source.close()
        ledger = self.fresh_ledger()
        accept(ledger, base)
        accept(ledger, correction)
        with self.assertRaisesRegex(ValueError, 'jointly fulfilled'):
            accept(ledger, over)
        self.assertEqual(accept(ledger, exact).outcome, 'offset_pending')
        result = post_offset(ledger, 'Y')
        self.assertEqual(result.by_station, {'甲': 5})
        book = read(ledger, day_id=day.day_id)
        self.assertEqual(book.totals.by_station['甲'].delta_litres, 5)
        self.assertEqual(book.totals.by_station['甲'].offset_litres, 5)
        # Formal book still carries the correction: 5 base + 5 correction - 5 offset.
        self.assertEqual(book.totals.by_station['甲'].current_litres, 5)
        self.assertEqual(book.totals.current_litres, 20)

    def test_offset_cannot_take_withdrawal_bound_correction_id(self):
        source, batches, days, register = self.source()
        day = self.four_stations(batches, days)
        base = export(days, register, day.day_id)
        from issue.corrections import amend
        amend(days, register, 'C', day.day_id, '甲', 5)
        withdraw(register, 'R', 'C')
        withdrawal = export(days, register, 'R')
        # A source-issued offset that reuses the original correction number.
        forged = json.dumps({
            'version': 2, 'id': 'C', 'day_id': day.day_id, 'kind': 'offset',
            'data': {'stations': ['甲'], 'litres': '5'},
        })
        source.close()
        ledger = self.fresh_ledger()
        accept(ledger, base)
        receipt = accept(ledger, withdrawal)
        self.assertEqual(receipt.outcome, 'withdrawal_without_entry')
        self.assertEqual(receipt.correction_id, 'C')
        with self.assertRaisesRegex(ValueError, 'accepted withdrawal'):
            accept(ledger, forged)
        self.assertIsNone(read(ledger, package_id='C'))
        # A withdrawal wire cannot target an accepted offset.
        ledger2 = self.fresh_ledger('offset-ledger.sqlite')
        source, batches, days, register = self.source()
        day = self.four_stations(batches, days)
        base2 = export(days, register, day.day_id)
        offset = issue_offset(days, register, 'P', day.day_id, STATIONS, 5)
        source.close()
        accept(ledger2, base2)
        accept(ledger2, offset)
        bogus = json.dumps({
            'version': 1, 'id': 'W', 'day_id': day.day_id, 'kind': 'withdrawal',
            'data': {'station': '甲', 'delta_litres': '-5', 'target_id': 'P'},
        })
        with self.assertRaisesRegex(ValueError, 'without an original correction entry'):
            accept(ledger2, bogus)
        self.assertIsNone(read(ledger2, package_id='W'))


    def test_prepare_handover_includes_offsets(self):
        day_id, base, wires = self.issue([('P', STATIONS, 5)])
        ledger = self.fresh_ledger()
        base_receipt = accept(ledger, base)
        offset_receipt = accept(ledger, wires['P'])
        posting = post_offset(ledger, 'P')
        report = prepare_handover(ledger, day_id, [day_id, 'P'])
        self.assertEqual(report.receipts, (base_receipt, offset_receipt))
        self.assertEqual(report.daybook.offsets, (posting,))
        self.assertEqual(report.daybook.totals.current_litres, 15)


if __name__ == '__main__':
    unittest.main()
