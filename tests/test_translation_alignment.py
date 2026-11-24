import sqlite3
import tempfile
import unittest
from pathlib import Path

from cet_sprint import service
from cet_sprint.storage import CoachError, db_path


class TranslationAlignmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        service.init_profile(self.workspace, level=6)
        self.item = service.add_item(
            self.workspace,
            section="translation",
            subtype="paragraph",
            prompt="只在周末延长开放时间。入馆无须预约。",
        )["item_id"]
        self.initial = [
            {
                "key": "范围",
                "source_quote": "只在周末",
                "translation_quote": "only on weekends",
                "status": "preserved",
            },
            {
                "key": "预约",
                "source_quote": "入馆无须预约",
                "translation_quote": "a reservation is required",
                "status": "distorted",
            },
        ]
        self.changed = [
            {
                **self.initial[0],
                "translation_quote": "every day",
                "status": "distorted",
            },
            {
                **self.initial[1],
                "translation_quote": "no reservation is required",
                "status": "preserved",
            },
        ]

    def record(self, text, **options):
        return service.attempt(
            self.workspace,
            item_id=self.item,
            response=text,
            feedback="保留原文限定。",
            evidence="核对周末与预约。",
            **options,
        )

    def test_paired_comparison_preserves_regression_and_short_evidence(self):
        a = self.record(
            "The hours are extended only on weekends; a reservation is required.",
            result="partial",
            criterion="核对范围和预约",
            alignment=self.initial,
        )
        b = self.record(
            "The hours are extended every day; no reservation is required.",
            result="partial",
            revises=a["attempt_id"],
            alignment=self.changed,
        )
        self.assertEqual(b["alignment_delta"]["repaired"], ["预约"])
        self.assertEqual(b["alignment_delta"]["regressed"], ["范围"])
        self.assertIn("范围", b["next_action"])
        history = service.revision_history(self.workspace, item_id=self.item)[
            "attempts"
        ]
        self.assertFalse(any(row["response_saved"] for row in history))
        self.assertEqual(history[1]["alignment_delta"], b["alignment_delta"])
        self.assertEqual(history[1]["revises_attempt_id"], history[0]["id"])

    def test_reference_quotes_and_point_scope_enforced_before_writes(self):
        draft = (
            "The library extends its hours only on weekends; a reservation is required."
        )
        cases = (
            (
                [{**self.initial[0], "source_quote": "每天"}, self.initial[1]],
                "source_quote_not_found",
            ),
            (
                [
                    {**self.initial[0], "translation_quote": "every day"},
                    self.initial[1],
                ],
                "translation_quote_not_found",
            ),
            (
                [{**self.initial[0], "status": "omitted"}, self.initial[1]],
                "omission_has_quote",
            ),
        )
        for points, code in cases:
            with self.subTest(code=code), self.assertRaises(CoachError) as raised:
                self.record(draft, result="partial", alignment=points)
            self.assertEqual(raised.exception.code, code)
        first = self.record(
            draft, result="partial", criterion="核对两个原文点", alignment=self.initial
        )
        updated = "The library extends its hours every day; no reservation is required."
        for data, result, code in (
            (None, "partial", "alignment_required"),
            (self.changed[:1], "partial", "alignment_scope_changed"),
            (self.changed, "correct", "alignment_result_mismatch"),
        ):
            with self.subTest(code=code), self.assertRaises(CoachError) as raised:
                self.record(
                    updated, result=result, revises=first["attempt_id"], alignment=data
                )
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(
            len(
                service.revision_history(self.workspace, item_id=self.item)["attempts"]
            ),
            1,
        )

    def test_v4_note_retained_without_inventing_typed_evidence(self):
        self.record("An earlier draft.", result="partial", criterion="核对限定")
        path = db_path(self.workspace)
        old = sqlite3.connect(path)
        with old:
            old.execute("ALTER TABLE attempts DROP COLUMN translation_alignment_json")
            old.execute("PRAGMA user_version = 4")
        old.close()
        history = service.revision_history(self.workspace, item_id=self.item)
        self.assertIsNone(history["attempts"][0]["translation_alignment"])
        self.assertEqual(history["attempts"][0]["evidence"], "核对周末与预约。")
        self.assertEqual(len(list(path.parent.glob("state.sqlite3.v4.*.bak"))), 1)
