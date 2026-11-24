from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from cet_sprint import service
from cet_sprint.storage import CoachError, db_path, local_date


class EfficiencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        service.init_profile(self.workspace, level=4)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_no_data_is_not_a_fake_efficiency_score(self) -> None:
        result = service.efficiency(self.workspace)
        self.assertEqual(result["diagnosis"]["code"], "evidence_insufficient")
        self.assertIsNone(service.plan(self.workspace, force=True)["focus"])
        self.assertIsNone(result["execution"]["completion_rate"])
        self.assertNotIn("score", result)

    def test_verified_answer_gap_is_separate_from_original_practice(self) -> None:
        self.assertIsNone(service.plan(self.workspace)["focus"])
        material = self.workspace / "reading.txt"
        material.write_text("The library opens at eight.", encoding="utf-8")
        source = service.add_material(
            self.workspace,
            str(material),
            section="reading",
            subtype="careful_reading",
            confidence="high",
        )
        for number in range(5):
            item = service.add_item(
                self.workspace,
                material_id=source["material_id"],
                section="reading",
                subtype="careful_reading",
                prompt=f"Q{number}: When does it open? A. Seven B. Eight C. Nine D. Ten",
                answer="B",
                answer_status="verified",
                evidence="line 1",
            )
            service.attempt(
                self.workspace,
                item_id=item["item_id"],
                response="B" if number < 2 else "A",
                minutes=2,
                error_tag="location_failed" if number >= 2 else None,
            )
        result = service.efficiency(self.workspace)
        self.assertEqual(result["effective_evidence"]["verified_key_attempts"], 5)
        self.assertEqual(
            result["independent_answer_quality"]["verified_source_questions"][
                "reading"
            ]["raw_accuracy"],
            0.4,
        )
        self.assertEqual(result["diagnosis"]["code"], "accuracy_gap")
        self.assertEqual(service.plan(self.workspace)["focus"], "reading")
        self.assertEqual(result["repeated_error_tags"]["location_failed"], 3)

    def test_recent_evidence_keeps_older_and_same_time_first_attempts(self) -> None:
        item_ids = [
            service.add_item(
                self.workspace,
                section="reading",
                subtype="careful_reading",
                prompt=f"Q{index}. A. One B. Two C. Three D. Four",
            )["item_id"]
            for index in range(3)
        ]
        today = local_date()
        recent = today.isoformat()
        older = (today - timedelta(days=30)).isoformat()
        with sqlite3.connect(db_path(self.workspace)) as conn:
            conn.executemany(
                """INSERT INTO attempts(id,item_id,made_at,result,minutes,judgment_basis,practice_mode)
                   VALUES(?,?,?, ?,1,'verified_key',?)""",
                [
                    ("old", item_ids[0], older, "wrong", "independent"),
                    ("retest", item_ids[0], recent, "correct", "independent"),
                    ("assisted", item_ids[1], recent, "wrong", "assisted"),
                    ("same-time", item_ids[1], recent, "correct", "independent"),
                    ("new", item_ids[2], recent, "wrong", "independent"),
                ],
            )
        plan = service.plan(self.workspace, force=True)
        self.assertEqual(plan["sample_evidence"]["reading"]["new_items"], 1)
        self.assertEqual(plan["sample_evidence"]["reading"]["attempts"], 3)
        result = service.efficiency(self.workspace)
        verified = result["independent_answer_quality"]["verified_source_questions"]
        self.assertEqual(verified["reading"]["total"], 1)
        self.assertEqual(verified["reading"]["correct"], 0)
        self.assertEqual(result["effective_evidence"]["review_attempts"], 2)

    def test_capacity_and_input_diagnoses_use_distinct_evidence(self) -> None:
        for offset in (2, 1, 0):
            service.today(self.workspace, current=local_date() - timedelta(days=offset))
        mismatch = service.efficiency(self.workspace)
        self.assertEqual(mismatch["diagnosis"]["code"], "capacity_mismatch")
        self.assertEqual(mismatch["execution"]["completion_rate"], 0)
        for offset in (2, 1, 0):
            daily = service.today(
                self.workspace, current=local_date() - timedelta(days=offset)
            )
            for task in daily["tasks"]:
                service.log_task(
                    self.workspace,
                    task_id=task["id"],
                    minutes=10,
                    evidence="只读了材料",
                )
        input_heavy = service.efficiency(self.workspace)
        self.assertEqual(input_heavy["diagnosis"]["code"], "input_heavy")
        self.assertEqual(
            input_heavy["effective_evidence"]["completed_tasks_with_evidence"], 3
        )

    def test_comparable_mock_is_raw_only_and_cli_is_json(self) -> None:
        for offset, correct, minutes in ((1, 5, 40), (0, 7, 42)):
            service.add_checkpoint(
                self.workspace,
                kind="mock",
                taken_on=(local_date() - timedelta(days=offset)).isoformat(),
                source=f"practice-{offset}",
                section="reading",
                correct=correct,
                question_total=10,
                minutes=minutes,
                comparison_key="仔细阅读-同难度-限时",
            )
        script = Path(__file__).resolve().parents[1] / "scripts" / "cet.py"
        done = subprocess.run(
            [
                sys.executable,
                str(script),
                "--workspace",
                str(self.workspace),
                "efficiency",
                "--days",
                "7",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stdout)
        result = json.loads(done.stdout)["data"]
        pair = result["comparable_mock_pairs"][0]
        self.assertEqual(
            (pair["earlier_raw_accuracy"], pair["later_raw_accuracy"]), (0.5, 0.7)
        )
        self.assertNotIn("reported_score_estimate", result)

    def test_repeating_one_item_does_not_become_five_independent_questions(
        self,
    ) -> None:
        item = service.add_item(
            self.workspace,
            section="reading",
            subtype="careful_reading",
            prompt="When? A. Seven B. Eight C. Nine D. Ten",
            answer="B",
            answer_status="verified",
            evidence="user-supplied answer key",
        )
        for index in range(5):
            service.attempt(
                self.workspace,
                item_id=item["item_id"],
                response="A",
                minutes=1,
                event_id=f"repeat-{index}",
            )
        result = service.efficiency(self.workspace)
        self.assertEqual(result["effective_evidence"]["verified_key_attempts"], 5)
        self.assertEqual(result["effective_evidence"]["new_verified_questions"], 1)
        self.assertEqual(result["effective_evidence"]["review_attempts"], 4)
        self.assertEqual(
            result["independent_answer_quality"]["verified_source_questions"][
                "reading"
            ]["total"],
            1,
        )
        self.assertEqual(result["diagnosis"]["code"], "evidence_insufficient")
        self.assertIsNone(service.plan(self.workspace, force=True)["focus"])

    def test_reused_paper_or_unknown_conditions_stay_out_of_mock_pairs(self) -> None:
        for offset, source, key in (
            (3, "卷 A", "仔细阅读-限时"),
            (2, "卷 A", "仔细阅读-限时"),
            (1, "卷 B", "长篇匹配-限时"),
            (0, "卷 C", None),
        ):
            service.add_checkpoint(
                self.workspace,
                kind="mock",
                taken_on=(local_date() - timedelta(days=offset)).isoformat(),
                source=source,
                section="reading",
                question_total=10,
                correct=6,
                minutes=30,
                comparison_key=key,
            )
        result = service.efficiency(self.workspace)
        self.assertEqual(result["effective_evidence"]["timed_mock_checkpoints"], 4)
        self.assertEqual(result["comparable_mock_pairs"], [])
        self.assertIn(
            "timed_mock", service.plan(self.workspace)["sample_evidence"]["reading"]
        )

    def test_assisted_answer_does_not_create_fresh_evidence_or_review_card(
        self,
    ) -> None:
        item = service.add_item(
            self.workspace,
            section="reading",
            subtype="careful_reading",
            prompt="When? A. Seven B. Eight C. Nine D. Ten",
            answer="B",
            answer_status="verified",
            evidence="user-supplied key",
        )
        first = service.attempt(
            self.workspace,
            item_id=item["item_id"],
            response="B",
            practice_mode="assisted",
        )
        self.assertIsNone(first["review"])
        self.assertEqual(first["practice_mode"], "assisted")
        after_assisted = service.report(self.workspace)
        self.assertEqual(after_assisted["assisted_attempts"], 1)
        self.assertEqual(after_assisted["independent_attempts"], {})
        service.attempt(self.workspace, item_id=item["item_id"], response="B")
        result = service.efficiency(self.workspace)["effective_evidence"]
        self.assertEqual(result["assisted_attempts"], 1)
        self.assertEqual(result["new_verified_questions"], 0)
        self.assertEqual(result["review_attempts"], 1)

    def test_focus_targets_subtype_and_picks_only_a_fresh_item(self) -> None:
        def add(subtype, number):
            return service.add_item(
                self.workspace,
                section="reading",
                subtype=subtype,
                prompt=f"Q{number}: What is the main idea? A. One B. Two C. Three D. Four",
                answer="C",
                answer_status="verified",
                evidence="user supplied",
            )["item_id"]

        for number in range(3):
            matching = add("matching", number)
            service.attempt(self.workspace, item_id=matching, response="C")
            reading = add("careful_reading", number + 3)
            failure = service.attempt(
                self.workspace,
                item_id=reading,
                response="B",
                error_tag="把例子当主旨",
                minutes=2,
            )
        self.assertEqual(failure["same_error_streak_distinct_items"], 3)
        self.assertIn("暂停加题", failure["next_action"])

        fresh_id = add("careful_reading", 6)
        add("matching", 7)
        plan = service.plan(self.workspace, force=True)
        self.assertEqual(plan["focus"], "reading")
        detail = plan["focus_detail"]
        self.assertEqual(detail["subtype"], "careful_reading")
        self.assertEqual(detail["top_error_tag"], "把例子当主旨")
        self.assertEqual(detail["repeated_errors"], {"把例子当主旨": 3})
        self.assertEqual(
            service.today(self.workspace)["tasks"][0]["subtype"], "careful_reading"
        )
        selected = service.drill(self.workspace, use_focus=True, fresh=True)
        self.assertEqual(selected["item_id"], fresh_id)
        self.assertNotIn("answer", selected)
        self.assertNotIn(
            fresh_id,
            {
                row["item_id"]
                for row in service.review(
                    self.workspace,
                    through=(local_date() + timedelta(days=1)).isoformat(),
                )["items"]
            },
        )

        service.attempt(
            self.workspace, item_id=fresh_id, response="B", error_tag="把例子当主旨"
        )
        service.attempt(
            self.workspace, item_id=fresh_id, response="B", error_tag="把例子当主旨"
        )
        report = service.efficiency(self.workspace)["independent_answer_quality"]
        careful = next(
            row
            for row in report["verified_subtype_evidence"]
            if row["subtype"] == "careful_reading"
        )
        self.assertEqual(careful["repeated_errors"], {"把例子当主旨": 4})
        with self.assertRaisesRegex(CoachError, "没有尚未作答"):
            service.drill(self.workspace, use_focus=True, fresh=True)


if __name__ == "__main__":
    unittest.main()
