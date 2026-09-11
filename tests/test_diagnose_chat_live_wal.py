import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('intake_diagnosis', Path(__file__).parents[1] / 'scripts/diagnose_chat_intake.py')
diagnosis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnosis)


class LiveWalDiagnosisTests(unittest.TestCase):
    def test_reads_committed_wal_events_without_allowing_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'jobs.db'
            with sqlite3.connect(db) as writer:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('CREATE TABLE chat_event_queue(event_id TEXT)')
                writer.commit()
                writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                writer.execute("INSERT INTO chat_event_queue VALUES ('new-message')")
                writer.commit()
                reader = diagnosis._open_readonly(db)
                self.assertEqual(reader.execute('SELECT event_id FROM chat_event_queue').fetchone()[0], 'new-message')
                with self.assertRaises(sqlite3.OperationalError):
                    reader.execute('DELETE FROM chat_event_queue')
                reader.close()
