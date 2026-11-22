import json
from dataclasses import dataclass

from intake.rows import litres_text, text_litres


@dataclass(frozen=True)
class AcceptanceRecord:
    package_id: str
    wire: str
    target_id: str | None
    receipt: dict


@dataclass(frozen=True)
class JournalEntry:
    sequence: int
    day_id: str
    package_id: str
    correction_id: str
    station: str
    delta: int


class AcceptanceStore:
    def __init__(self, connection):
        self.connection = connection

    def initialize(self):
        self.connection.execute('BEGIN IMMEDIATE')
        with self.connection:
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS accepted_packages (
                    package_id TEXT PRIMARY KEY, wire TEXT NOT NULL,
                    target_id TEXT UNIQUE, receipt TEXT NOT NULL
                )
            ''')
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS accepted_bases (
                    day_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL
                )
            ''')
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS journal_entries (
                    sequence INTEGER PRIMARY KEY, day_id TEXT NOT NULL,
                    package_id TEXT NOT NULL UNIQUE, correction_id TEXT NOT NULL,
                    station TEXT NOT NULL, delta_litres TEXT NOT NULL
                )
            ''')
            self.connection.execute('''
                CREATE INDEX IF NOT EXISTS journal_day_sequence
                ON journal_entries (day_id, sequence)
            ''')

    @staticmethod
    def _acceptance(row):
        if row is None:
            return None
        package_id, wire, target_id, receipt = row
        return AcceptanceRecord(package_id, wire, target_id, json.loads(receipt))

    @staticmethod
    def _entry(row):
        sequence, day_id, package_id, correction_id, station, delta = row
        return JournalEntry(sequence, day_id, package_id, correction_id, station, text_litres(delta))

    def get_acceptance(self, package_id):
        row = self.connection.execute('''
            SELECT package_id, wire, target_id, receipt FROM accepted_packages WHERE package_id = ?
        ''', (package_id,)).fetchone()
        return self._acceptance(row)

    def get_base(self, day_id):
        row = self.connection.execute(
            'SELECT snapshot FROM accepted_bases WHERE day_id = ?', (day_id,)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def find_correction_entry(self, correction_id):
        row = self.connection.execute('''
            SELECT sequence, day_id, package_id, correction_id, station, delta_litres
            FROM journal_entries WHERE package_id = ? AND correction_id = ?
        ''', (correction_id, correction_id)).fetchone()
        return None if row is None else self._entry(row)

    def find_accepted_withdrawal(self, correction_id):
        row = self.connection.execute('''
            SELECT package_id, wire, target_id, receipt FROM accepted_packages WHERE target_id = ?
        ''', (correction_id,)).fetchone()
        return self._acceptance(row)

    def list_journal(self, day_id):
        rows = self.connection.execute('''
            SELECT sequence, day_id, package_id, correction_id, station, delta_litres
            FROM journal_entries WHERE day_id = ? ORDER BY sequence
        ''', (day_id,))
        return tuple(self._entry(row) for row in rows)

    def _require_transaction(self):
        if not self.connection.in_transaction:
            raise RuntimeError('publishing acceptance requires a transaction')

    def insert_acceptance(self, record: AcceptanceRecord):
        self._require_transaction()
        receipt = json.dumps(record.receipt, sort_keys=True, separators=(',', ':'))
        self.connection.execute('''
            INSERT INTO accepted_packages (package_id, wire, target_id, receipt) VALUES (?, ?, ?, ?)
        ''', (record.package_id, record.wire, record.target_id, receipt))

    def insert_base(self, day_id, payload):
        self._require_transaction()
        snapshot = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        self.connection.execute(
            'INSERT INTO accepted_bases (day_id, snapshot) VALUES (?, ?)', (day_id, snapshot)
        )

    def append_journal(self, day_id, package_id, correction_id, station, delta):
        self._require_transaction()
        cursor = self.connection.execute('''
            INSERT INTO journal_entries (day_id, package_id, correction_id, station, delta_litres)
            VALUES (?, ?, ?, ?, ?)
        ''', (day_id, package_id, correction_id, station, litres_text(delta)))
        return cursor.lastrowid
