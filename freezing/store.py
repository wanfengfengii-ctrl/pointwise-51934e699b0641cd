import json
from contextlib import contextmanager


class FrozenDayStore:
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
                CREATE TABLE IF NOT EXISTS frozen_days (
                    day_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL
                )
            ''')

    def save_snapshot(self, day_id, payload):
        snapshot = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        with self.transaction():
            result = self.connection.execute('''
                INSERT INTO frozen_days (day_id, snapshot) VALUES (?, ?)
                ON CONFLICT(day_id) DO NOTHING
            ''', (day_id, snapshot))
            if result.rowcount == 0:
                existing = self.connection.execute(
                    'SELECT snapshot FROM frozen_days WHERE day_id = ?', (day_id,)
                ).fetchone()[0]
                if existing != snapshot:
                    raise ValueError(f'conflicting frozen day: {day_id}')

    def load_snapshot(self, day_id):
        record = self.connection.execute(
            'SELECT snapshot FROM frozen_days WHERE day_id = ?', (day_id,)
        ).fetchone()
        return None if record is None else json.loads(record[0])
