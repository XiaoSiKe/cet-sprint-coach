import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from cet_sprint import service
from cet_sprint.recovery import restore_database
from cet_sprint.storage import CoachError, db_path


class DatabaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workspace = self.root / "study"
        service.init_profile(self.workspace, level=6)
        self.item = service.add_item(
            self.workspace,
            section="translation",
            subtype="paragraph",
            prompt="入馆无须预约。",
        )["item_id"]
        service.attempt(
            self.workspace,
            item_id=self.item,
            result="correct",
            response="No reservation is required.",
            evidence="No reservation is required.",
            feedback="保留了否定条件。",
            criterion="核对预约条件",
            alignment=[
                {
                    "key": "预约",
                    "source_quote": "入馆无须预约",
                    "translation_quote": "No reservation is required",
                    "status": "preserved",
                }
            ],
        )
        self.target = db_path(self.workspace)
        self.source = self.root / "verified.sqlite3"
        with (
            closing(sqlite3.connect(self.target)) as source,
            closing(sqlite3.connect(self.source)) as dest,
        ):
            source.backup(dest)

    def test_restore_keeps_exact_damaged_bytes_and_restores_links(self):
        original = self.source.read_bytes()
        damaged = b"SQLite format 3\0 truncated"
        self.target.write_bytes(damaged)
        done = restore_database(self.workspace, str(self.source))
        self.assertTrue(done["restored"])
        self.assertEqual(done["attempts"], 1)
        self.assertEqual(Path(done["preserved_damaged_database"]).read_bytes(), damaged)
        self.assertEqual(self.target.read_bytes(), original)
        self.assertTrue(
            restore_database(self.workspace, str(self.source))["already_matches"]
        )
        history = service.revision_history(self.workspace, item_id=self.item)
        self.assertEqual(
            history["attempts"][0]["translation_alignment"][0]["status"], "preserved"
        )

    def test_wrong_backup_or_active_sidecar_leaves_damaged_target_untouched(self):
        self.target.write_bytes(b"damaged")
        wrong = self.root / "unknown.sqlite3"
        with closing(sqlite3.connect(wrong)) as conn:
            conn.execute("CREATE TABLE unrelated (id INTEGER)")
        with self.assertRaises(CoachError) as invalid:
            restore_database(self.workspace, str(wrong))
        self.assertEqual(invalid.exception.code, "invalid_database_backup")
        Path(str(self.target) + "-wal").write_bytes(b"live")
        with self.assertRaises(CoachError) as sidecar:
            restore_database(self.workspace, str(self.source))
        self.assertEqual(sidecar.exception.code, "recovery_sidecars_present")
        self.assertEqual(self.target.read_bytes(), b"damaged")

    def test_older_backup_does_not_overwrite_active_progress(self):
        service.attempt(
            self.workspace,
            item_id=self.item,
            response="Visitors do not need a booking.",
            result="correct",
            evidence="do not need a booking",
            feedback="准确。",
        )
        data = self.target.read_bytes()
        with self.assertRaises(CoachError) as healthy:
            restore_database(self.workspace, str(self.source))
        self.assertEqual(healthy.exception.code, "recovery_target_healthy")
        self.assertEqual(self.target.read_bytes(), data)
        self.assertEqual(service.status(self.workspace)["counts"]["attempts"], 2)
