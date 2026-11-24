import sqlite3
import tempfile
import unittest
from pathlib import Path

from cet_sprint import service
from cet_sprint.storage import CoachError, db_path


class RevisionHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        service.init_profile(self.workspace, level=6)
        self.item = service.add_item(
            self.workspace,
            section="writing",
            subtype="essay",
            prompt="Write an essay about responsible use of AI.",
        )["item_id"]
        self.criterion = "说明独立起草、检查反馈和自行改写的过程。"

    def reviewed(self, response, **args):
        return service.attempt(
            self.workspace,
            item_id=self.item,
            response=response,
            result="partial",
            feedback="说清核实证据的步骤。",
            evidence="I would look up the timetable.",
            **args,
        )

    def test_rewrite_targets_first_draft_and_keeps_evidence_separate(self) -> None:
        initial = self.reviewed(
            "first only", criterion=self.criterion, event_id="first"
        )
        self.reviewed("second only", criterion=self.criterion, event_id="second")
        rewrite = self.reviewed(
            "first revised", revises=initial["attempt_id"], event_id="rewrite"
        )
        self.assertEqual(rewrite["practice_mode"], "assisted")
        self.assertEqual(rewrite["revises_attempt_id"], "first")
        self.assertEqual(rewrite["revision_criterion"], self.criterion)
        history = service.revision_history(self.workspace, item_id=self.item)
        self.assertEqual(
            (history["independent_drafts"], history["guided_rewrites"]), (2, 1)
        )
        self.assertTrue(history["attempts"][-1]["matches_parent_criterion"])
        self.assertTrue(
            all(
                not row["response_saved"] and "response" not in row
                for row in history["attempts"]
            )
        )
        stats = service.efficiency(self.workspace)["effective_evidence"]
        self.assertEqual(stats["supported_writing_translation_reviews"], 2)
        self.assertEqual(stats["guided_writing_translation_rewrites"], 1)
        self.assertTrue(
            self.reviewed("first revised", revises="first", event_id="rewrite")[
                "duplicate"
            ]
        )
        with self.assertRaises(CoachError) as changed:
            self.reviewed("again", revises="first", criterion="换成检查时态")
        self.assertEqual(changed.exception.code, "revision_criterion_mismatch")
        self.assertEqual(
            len(
                service.revision_history(self.workspace, item_id=self.item)["attempts"]
            ),
            3,
        )

    def test_v3_feedback_migrates_and_can_be_used_without_inventing_a_saved_criterion(
        self,
    ) -> None:
        first = self.reviewed("older draft")
        path = db_path(self.workspace)
        old = sqlite3.connect(path)
        with old:
            old.execute("ALTER TABLE attempts DROP COLUMN translation_alignment_json")
            old.execute("ALTER TABLE attempts DROP COLUMN revises_attempt_id")
            old.execute("ALTER TABLE attempts DROP COLUMN revision_criterion")
            old.execute("PRAGMA user_version = 3")
        old.close()
        with self.assertRaises(CoachError) as missing:
            self.reviewed("revision", revises=first["attempt_id"])
        self.assertEqual(missing.exception.code, "revision_criterion_missing")
        self.reviewed("revision", revises=first["attempt_id"], criterion=self.criterion)
        history = service.revision_history(self.workspace, item_id=self.item)
        self.assertIsNone(history["attempts"][-1]["matches_parent_criterion"])
        self.assertEqual(history["attempts"][0]["feedback"], "说清核实证据的步骤。")
        self.assertEqual(len(list(path.parent.glob("state.sqlite3.v3.*.bak"))), 1)
