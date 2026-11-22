from contextlib import contextmanager

from intake.provenance import SourceReference
from intake.receipts import ImportReceipt
from intake.rows import ReadingRow, StationBatch, litres_text, text_litres


class BatchStore:
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
                CREATE TABLE IF NOT EXISTS station_batches (
                    batch_id TEXT PRIMARY KEY, station TEXT NOT NULL, day TEXT NOT NULL
                )
            ''')
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS reading_rows (
                    batch_id TEXT NOT NULL, position INTEGER NOT NULL,
                    document TEXT NOT NULL, source_line INTEGER NOT NULL, litres TEXT,
                    PRIMARY KEY (batch_id, position)
                )
            ''')
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS import_receipts (
                    batch_id TEXT PRIMARY KEY, row_count INTEGER NOT NULL,
                    missing_count INTEGER NOT NULL
                )
            ''')

    def get_batch(self, batch_id):
        header = self.connection.execute(
            'SELECT station, day FROM station_batches WHERE batch_id = ?', (batch_id,)
        ).fetchone()
        if header is None:
            return None
        records = self.connection.execute('''
            SELECT position, document, source_line, litres FROM reading_rows
            WHERE batch_id = ? ORDER BY position
        ''', (batch_id,))
        rows = tuple(
            ReadingRow(
                None if quantity is None else text_litres(quantity),
                SourceReference(document, line, batch_id, position),
            )
            for position, document, line, quantity in records
        )
        return StationBatch(batch_id, header[0], header[1], rows)

    def get_receipt(self, batch_id):
        record = self.connection.execute('''
            SELECT row_count, missing_count FROM import_receipts WHERE batch_id = ?
        ''', (batch_id,)).fetchone()
        return None if record is None else ImportReceipt(batch_id, *record)

    def save_batch(self, batch, receipt):
        if not self.connection.in_transaction:
            raise RuntimeError('saving a batch requires a transaction')
        self.connection.execute(
            'INSERT INTO station_batches (batch_id, station, day) VALUES (?, ?, ?)',
            (batch.batch_id, batch.station, batch.day),
        )
        self.connection.executemany('''
            INSERT INTO reading_rows (batch_id, position, document, source_line, litres)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            (batch.batch_id, row.source.position, row.source.document, row.source.line,
             None if row.litres is None else litres_text(row.litres))
            for row in batch.rows
        ))
        self.connection.execute('''
            INSERT INTO import_receipts (batch_id, row_count, missing_count) VALUES (?, ?, ?)
        ''', (receipt.batch_id, receipt.row_count, receipt.missing_count))
