from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import wave
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from cet_sprint import service
from cet_sprint.scheduling import next_review
from cet_sprint.storage import CoachError, db_path, local_date


class EngineFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "student"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_cet4_source_drill_review_checkpoint_export(self) -> None:
        profile = service.init_profile(
            self.workspace,
            level=4,
            weak_section="reading",
            weekday_minutes=80,
            weekend_minutes=120,
        )
        self.assertTrue(profile["created"])
        paper = self.root / "practice.txt"
        paper.write_text(
            "Passage A: A student visits the library.\nQuestion 1: Where does she go?",
            encoding="utf-8",
        )
        before = paper.read_bytes()
        material = service.add_material(
            self.workspace,
            str(paper),
            section="reading",
            subtype="careful_reading",
            confidence="high",
        )
        duplicate = service.add_material(self.workspace, str(paper))
        self.assertTrue(material["added"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(paper.read_bytes(), before)
        item = service.add_item(
            self.workspace,
            material_id=material["material_id"],
            section="reading",
            subtype="careful_reading",
            prompt="Where does she go? A. Library B. Café C. Station D. Office",
            answer="A",
            answer_status="verified",
            evidence="Passage A, sentence 1",
            locator="line:1",
        )
        question = service.drill(self.workspace, section="reading")
        self.assertEqual(question["item_id"], item["item_id"])
        self.assertNotIn("answer", question)
        self.assertNotIn("transcript", question)
        first = service.attempt(
            self.workspace,
            item_id=item["item_id"],
            response="B",
            minutes=2,
            error_tag="location_failed",
            event_id="attempt-1",
        )
        self.assertEqual(first["result"], "wrong")
        self.assertEqual(first["answer_reveal"], "A")
        self.assertEqual(first["judgment_basis"], "verified_key")
        retry = service.attempt(
            self.workspace,
            item_id=item["item_id"],
            response="B",
            minutes=2,
            error_tag="location_failed",
            event_id="attempt-1",
        )
        self.assertTrue(retry["duplicate"])
        another = service.add_item(
            self.workspace,
            section="reading",
            subtype="careful_reading",
            prompt="What does she borrow? A. Book B. Pen C. Map D. Phone",
        )
        with self.assertRaises(CoachError):
            service.attempt(
                self.workspace,
                item_id=another["item_id"],
                response="A",
                event_id="attempt-1",
            )
        self.assertEqual(service.status(self.workspace)["counts"]["attempts"], 1)
        self.assertEqual(service.review(self.workspace)["total_due"], 0)
        checkpoint = service.add_checkpoint(
            self.workspace,
            kind="mock",
            taken_on=local_date().isoformat(),
            source="learner practice",
            section="reading",
            correct=6,
            question_total=10,
            minutes=40,
        )
        self.assertTrue(checkpoint["replan_required"])
        weekly = service.plan(self.workspace)
        self.assertEqual(weekly["focus"], "reading")
        daily = service.today(self.workspace)
        self.assertLessEqual(len(daily["tasks"]), 3)
        self.assertEqual(
            daily["training_budget_minutes"], int(daily["available_minutes"] * 0.85)
        )
        task_id = daily["tasks"][0]["id"]
        service.log_task(
            self.workspace,
            task_id=task_id,
            minutes=20,
            evidence="限时阅读 6/10，错因已标注",
        )
        summary = service.report(self.workspace)
        self.assertEqual(summary["execution"]["completed_tasks"], 1)
        self.assertEqual(summary["independent_attempts"]["reading"]["wrong"], 1)
        exported = service.export_state(self.workspace, format_name="json")
        data = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(data["materials"]), 1)
        self.assertIsNone(data["attempts"][0]["response"])
        markdown = service.export_state(self.workspace, format_name="md")
        self.assertIn("原始正确", Path(markdown["path"]).read_text(encoding="utf-8"))

    def test_cet6_audio_and_fsrs_fallback(self) -> None:
        service.init_profile(self.workspace, level=6)
        audio = self.root / "clip.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\0\0" * 800)
        material = service.add_material(
            self.workspace,
            str(audio),
            section="listening",
            subtype="lecture",
            confidence="high",
        )
        item = service.add_item(
            self.workspace,
            material_id=material["material_id"],
            section="listening",
            subtype="lecture",
            prompt="What is the main point? A. Cost B. Access C. Weather D. Time",
            answer="B",
            answer_status="verified",
            evidence="user-supplied transcript, sentence 1",
            audio_path=str(audio),
            transcript="Access matters most.",
        )
        shown = service.drill(self.workspace, section="listening")
        self.assertEqual(Path(shown["audio_path"]), audio.resolve())
        self.assertNotIn("Access matters most.", json.dumps(shown))
        result = service.attempt(
            self.workspace, item_id=item["item_id"], response="B", minutes=1
        )
        self.assertEqual(result["judgment_basis"], "verified_key")
        service.add_checkpoint(
            self.workspace,
            kind="mock",
            taken_on=local_date().isoformat(),
            source="timed listening sample",
            section="listening",
            correct=1,
            question_total=1,
            minutes=2,
        )
        self.assertIn("focus", service.plan(self.workspace))
        self.assertLessEqual(len(service.today(self.workspace)["tasks"]), 3)
        vocab = service.add_item(
            self.workspace,
            section="vocab",
            subtype="vocab",
            source_type="original",
            prompt="Give the meaning of resilient.",
            answer="able to recover",
            answer_status="generated",
        )
        first = service.attempt(
            self.workspace,
            item_id=vocab["item_id"],
            response="able to recover",
            result="correct",
            minutes=1,
        )
        self.assertEqual(first["review"]["scheduler"], "fsrs")
        self.assertTrue(first["review"]["card_json"])
        exported = service.export_state(self.workspace, format_name="json")
        self.assertTrue(Path(exported["path"]).is_file())
        with patch.dict(sys.modules, {"fsrs": None}):
            fallback = next_review(
                section="vocab",
                result="wrong",
                previous=first["review"],
                exam_date=None,
            )
        self.assertEqual(fallback["scheduler"], "fixed_fallback")
        self.assertEqual(
            fallback["due_at"], (local_date() + timedelta(days=1)).isoformat()
        )
        with self.assertRaises(CoachError):
            service.add_item(
                self.workspace,
                section="listening",
                subtype="short_news",
                prompt="wrong level",
            )

    def test_provenance_and_score_boundaries(self) -> None:
        service.init_profile(self.workspace, level=4)
        with self.assertRaises(CoachError):
            service.add_item(
                self.workspace,
                section="reading",
                subtype="careful_reading",
                source_type="official",
                prompt="Claim without source",
            )
        with self.assertRaises(CoachError):
            service.add_item(
                self.workspace,
                section="reading",
                subtype="careful_reading",
                source_type="original",
                prompt="Question?",
                answer="A",
                answer_status="verified",
                evidence="AI",
            )
        unverified = service.add_item(
            self.workspace,
            section="reading",
            subtype="matching",
            prompt="Which paragraph?",
            answer_status="unverified",
        )
        question = service.drill(self.workspace, section="reading")
        self.assertEqual(question["item_id"], unverified["item_id"])
        answer = service.attempt(
            self.workspace,
            item_id=unverified["item_id"],
            response="C",
            result="unverified",
        )
        self.assertIsNone(answer["answer_reveal"])
        with self.assertRaises(CoachError):
            service.add_checkpoint(
                self.workspace,
                kind="mock",
                taken_on=local_date().isoformat(),
                source="test",
                section="reading",
                correct=8,
                question_total=10,
                minutes=40,
                score_total=500,
            )
        with self.assertRaises(CoachError):
            service.add_checkpoint(
                self.workspace,
                kind="official",
                taken_on=local_date().isoformat(),
                source="score report",
                score_total=500,
                score_listening=200,
                score_reading=200,
                score_writing_translation=200,
            )
        official = service.add_checkpoint(
            self.workspace,
            kind="official",
            taken_on=local_date().isoformat(),
            source="score report",
            score_total=500,
            score_listening=160,
            score_reading=180,
            score_writing_translation=160,
        )
        self.assertTrue(official["saved"])
        self.assertEqual(
            service.report(self.workspace)["checkpoints"][0]["score_total"], 500
        )

    def test_scanned_pdf_and_migration_backup(self) -> None:
        state = db_path(self.workspace)
        state.parent.mkdir()
        sqlite3.connect(state).close()
        service.init_profile(self.workspace, level=4)
        self.assertTrue(list(state.parent.glob("state.sqlite3.v0.*.bak")))
        from pypdf import PdfWriter

        pdf = self.root / "scan.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        with pdf.open("wb") as handle:
            writer.write(handle)
        imported = service.add_material(self.workspace, str(pdf))
        self.assertIn("扫描件", "".join(imported["warnings"]))
        with self.assertRaises(CoachError):
            service.drill(self.workspace, section="listening")
        foreign_workspace = self.root / "foreign"
        foreign_state = db_path(foreign_workspace)
        foreign_state.parent.mkdir(parents=True)
        with sqlite3.connect(foreign_state) as conn:
            conn.execute("CREATE TABLE unrelated (id INTEGER)")
        with self.assertRaises(CoachError) as error:
            service.init_profile(foreign_workspace, level=4)
        self.assertEqual(error.exception.code, "unknown_schema")
        self.assertTrue(list(foreign_state.parent.glob("state.sqlite3.v0.*.bak")))

    def test_json_cli_smoke(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "cet.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--workspace",
                str(self.workspace),
                "init",
                "--level",
                "4",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["ok"])
        missing = subprocess.run(
            [
                sys.executable,
                str(script),
                "--workspace",
                str(self.workspace),
                "drill",
                "--section",
                "reading",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        payload = json.loads(missing.stdout)
        self.assertEqual(payload["code"], "no_usable_item")
        self.assertNotEqual(missing.returncode, 0)
        invalid = subprocess.run(
            [sys.executable, str(script), "--workspace", str(self.workspace), "init"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(json.loads(invalid.stdout)["code"], "invalid_arguments")

    def test_batch_import_is_atomic_and_targeted_review_hides_answer(self) -> None:
        service.init_profile(self.workspace, level=4)
        source = self.root / "reading.txt"
        source.write_text("[Page 1] The library opens at eight.", encoding="utf-8")
        material = service.add_material(
            self.workspace,
            str(source),
            section="reading",
            subtype="careful_reading",
            confidence="high",
        )
        first = {
            "section": "reading",
            "subtype": "careful_reading",
            "prompt": "When does it open? A. Seven B. Eight C. Nine D. Ten",
            "answer": "B",
            "answer_status": "verified",
            "evidence": "page 1, sentence 1",
            "locator": "page:1",
        }
        second = {
            "section": "reading",
            "subtype": "careful_reading",
            "prompt": "Why does it open? A. Staff B. Visitors C. Weather D. Unknown",
        }
        with self.assertRaises(CoachError):
            service.add_items_batch(
                self.workspace,
                [first, {**second, "subtype": "lecture"}],
                material_id=material["material_id"],
            )
        self.assertEqual(service.list_items(self.workspace)["items"], [])
        manifest = {
            "schema_version": 1,
            "material_id": material["material_id"],
            "items": [first, second],
        }
        manifest_path = self.root / "items.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "cet.py"
        imported = subprocess.run(
            [
                sys.executable,
                str(script),
                "--workspace",
                str(self.workspace),
                "material",
                "batch-add",
                "--input",
                str(manifest_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(imported.returncode, 0, imported.stdout)
        payload = json.loads(imported.stdout)["data"]
        self.assertEqual(payload["added_count"], 2)
        self.assertEqual(
            service.add_items_batch(
                self.workspace, [first, second], material_id=material["material_id"]
            )["skipped_count"],
            2,
        )
        exact = subprocess.run(
            [
                sys.executable,
                str(script),
                "--workspace",
                str(self.workspace),
                "drill",
                "--item-id",
                payload["added_item_ids"][0],
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        shown = json.loads(exact.stdout)["data"]
        self.assertEqual(shown["item_id"], payload["added_item_ids"][0])
        self.assertNotIn("answer", shown)
        self.assertNotIn("page 1, sentence 1", json.dumps(shown))
        with sqlite3.connect(db_path(self.workspace)) as conn:
            conn.execute(
                "INSERT INTO review_cards(item_id,due_at,stage,scheduler,updated_at) VALUES(?,?,?,?,?)",
                (
                    shown["item_id"],
                    local_date().isoformat(),
                    0,
                    "fixed_interval",
                    local_date().isoformat(),
                ),
            )
        due_id = service.review(self.workspace)["items"][0]["item_id"]
        self.assertEqual(
            service.drill(self.workspace, item_id=due_id)["item_id"], due_id
        )
        with self.assertRaises(CoachError):
            service.drill(self.workspace, item_id=due_id, section="listening")

    def test_replan_discards_obsolete_pending_task_and_exam_day(self) -> None:
        service.init_profile(self.workspace, level=4, weak_section="reading")
        initial = service.today(self.workspace)
        old_ids = {task["id"] for task in initial["tasks"]}
        service.set_profile(self.workspace, {"weak_section": "listening"})
        updated = service.today(self.workspace)
        new_ids = {task["id"] for task in updated["tasks"]}
        self.assertNotEqual(old_ids, new_ids)
        self.assertEqual(
            service.report(self.workspace)["execution"]["planned_tasks"],
            len(updated["tasks"]),
        )
        service.set_profile(self.workspace, {"exam_date": local_date().isoformat()})
        exam_day = service.today(self.workspace)
        self.assertEqual(exam_day["tasks"], [])
        self.assertIn("考试日", exam_day["reason"])

    def test_original_listening_tts_when_available(self) -> None:
        if not shutil.which("say") or not shutil.which("ffmpeg"):
            self.skipTest("macOS say and ffmpeg are required for the optional TTS path")
        service.init_profile(self.workspace, level=6)
        item = service.add_item(
            self.workspace,
            section="listening",
            subtype="lecture",
            source_type="original",
            prompt="What is the topic? A. Books B. Time C. Money D. Travel",
            answer="B",
            answer_status="generated",
            transcript="Time is limited.",
        )
        with self.assertRaises(CoachError):
            service.drill(self.workspace, section="listening")
        result = service.synthesize_item(self.workspace, item["item_id"])
        self.assertTrue(Path(result["audio_path"]).is_file())
        self.assertEqual(
            service.drill(self.workspace, section="listening")["audio_label"],
            "原创辅助音频，非官方考试材料",
        )


if __name__ == "__main__":
    unittest.main()
