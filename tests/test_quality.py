from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cet_sprint import service
from cet_sprint.storage import CoachError

PASSAGE = (
    "The library opens at eight and closes at six.\n"
    "When does it open? A. Seven B. Eight C. Nine D. Ten"
)


class GeneratedItemQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        service.init_profile(self.workspace, level=4)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_reading(self, **changes: str) -> dict:
        fields = {
            "section": "reading",
            "subtype": "careful_reading",
            "source_type": "original",
            "prompt": PASSAGE,
            "answer": "B",
            "answer_status": "generated",
            "evidence": "The library opens at eight",
        }
        fields.update(changes)
        return service.add_item(self.workspace, **fields)

    def assert_quality_error(self, code: str, **changes: str) -> None:
        with self.assertRaises(CoachError) as caught:
            self.add_reading(**changes)
        self.assertEqual(caught.exception.code, code)

    def test_grounded_original_can_train_without_leaking_key(self) -> None:
        item = self.add_reading()
        question = service.drill(self.workspace, item_id=item["item_id"])
        self.assertNotIn("answer", question)
        self.assertNotIn("evidence", question)
        outcome = service.attempt(
            self.workspace, item_id=item["item_id"], response="B", minutes=2
        )
        self.assertEqual(outcome["answer_reveal"], "B")
        self.assertEqual(outcome["judgment_basis"], "generated_key")

    def test_rejects_structural_and_evidence_failures(self) -> None:
        self.assert_quality_error(
            "invalid_choice_options",
            prompt="The library opens at eight. When? A. Seven B. Eight C. Nine",
        )
        self.assert_quality_error(
            "duplicate_choice_options",
            prompt="The library opens at eight. When? A. Seven B. Seven C. Nine D. Ten",
        )
        self.assert_quality_error("invalid_choice_answer", answer="E")
        self.assert_quality_error(
            "generated_evidence_mismatch", evidence="The library opens at noon"
        )
        self.assert_quality_error(
            "generated_evidence_mismatch", evidence="Seven B. Eight"
        )
        self.assert_quality_error(
            "answer_leak_in_prompt", prompt=PASSAGE + "\nAnswer: B"
        )

    def test_batch_and_update_use_same_quality_gate(self) -> None:
        valid = {
            "section": "reading",
            "subtype": "careful_reading",
            "source_type": "original",
            "prompt": PASSAGE,
            "answer": "B",
            "answer_status": "generated",
            "evidence": "The library opens at eight",
        }
        with self.assertRaises(CoachError):
            service.add_items_batch(
                self.workspace,
                [
                    valid,
                    {
                        **valid,
                        "prompt": PASSAGE.replace("D. Ten", ""),
                        "evidence": "bad evidence",
                    },
                ],
            )
        self.assertEqual(service.list_items(self.workspace)["items"], [])
        draft = service.add_item(
            self.workspace,
            section="reading",
            subtype="careful_reading",
            source_type="original",
            prompt=PASSAGE,
        )
        with self.assertRaises(CoachError) as caught:
            service.update_item(
                self.workspace,
                draft["item_id"],
                answer="B",
                answer_status="generated",
                evidence="The library opens at noon",
            )
        self.assertEqual(caught.exception.code, "generated_evidence_mismatch")
        result = service.update_item(
            self.workspace,
            draft["item_id"],
            answer="B",
            answer_status="generated",
            evidence="The library opens at eight",
        )
        self.assertEqual(result["answer_status"], "generated")

    def test_original_listening_needs_transcript(self) -> None:
        with self.assertRaises(CoachError) as caught:
            service.add_item(
                self.workspace,
                section="listening",
                subtype="short_news",
                source_type="original",
                prompt="What happened? A. Rain B. Snow C. Wind D. Fog",
                answer="A",
                answer_status="generated",
                evidence="It rained all night",
            )
        self.assertEqual(caught.exception.code, "missing_original_transcript")


if __name__ == "__main__":
    unittest.main()
