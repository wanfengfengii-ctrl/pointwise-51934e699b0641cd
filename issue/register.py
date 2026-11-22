from contextlib import contextmanager
from dataclasses import dataclass

from freezing.history import load_frozen_day
from freezing.store import FrozenDayStore
from intake.provenance import require_name
from issue.packages import Package, decode_package, encode_package, make_base_package


@dataclass(frozen=True)
class IssueRecord:
    package: Package
    wire: str


class IssueRegister:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def transaction(self):
        self.connection.execute('BEGIN IMMEDIATE')
        with self.connection:
            yield

    def initialize(self):
        with self.transaction():
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS issued_packages (
                    package_id TEXT PRIMARY KEY, target_id TEXT UNIQUE, wire TEXT NOT NULL
                )
            ''')

    @staticmethod
    def _record(row):
        if row is None:
            return None
        return IssueRecord(decode_package(row[0]), row[0])

    def find(self, package_id):
        row = self.connection.execute(
            'SELECT wire FROM issued_packages WHERE package_id = ?', (package_id,)
        ).fetchone()
        return self._record(row)

    def find_withdrawal(self, correction_id):
        row = self.connection.execute(
            'SELECT wire FROM issued_packages WHERE target_id = ?', (correction_id,)
        ).fetchone()
        return self._record(row)

    def save(self, package: Package) -> IssueRecord:
        if not self.connection.in_transaction:
            raise RuntimeError('issuing a package requires a transaction')
        wire = encode_package(package)
        self.connection.execute('''
            INSERT INTO issued_packages (package_id, target_id, wire) VALUES (?, ?, ?)
        ''', (package.package_id, package.target_id, wire))
        return IssueRecord(package, wire)


def export(days: FrozenDayStore, register: IssueRegister, package_id) -> str:
    require_name(package_id, 'package id')
    with register.transaction():
        record = register.find(package_id)
        if record is not None:
            return record.wire
        day = load_frozen_day(days, package_id)
        if day is None:
            raise LookupError(f'package not found: {package_id}')
        return register.save(make_base_package(day)).wire
