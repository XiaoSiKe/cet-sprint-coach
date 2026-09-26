from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from cet_sprint import service
from cet_sprint.storage import local_date


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
        self.assertEqual(result["repeated_error_tags"]["location_failed"], 3)

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


if __name__ == "__main__":
    unittest.main()
