from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import wave
from datetime import date, timedelta
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
        self.assertEqual(result["transcript_reveal"], "Access matters most.")
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

    def test_cli_emits_utf8_json_on_ascii_console(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "cet.py"
        cmd = [sys.executable, "-B", str(script), "--workspace", str(self.workspace)]
        env = {**os.environ, "PYTHONIOENCODING": "ascii"}
        missing = subprocess.run(
            cmd + ["status"], env=env, capture_output=True, check=False
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("档案", json.loads(missing.stdout)["message"])
        subprocess.check_output(cmd + ["init", "--level", "4"], env=env)
        day = json.loads(subprocess.check_output(cmd + ["today"], env=env))
        self.assertTrue(day["ok"])
        self.assertIn("诊断", day["data"]["tasks"][0]["title"])

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

    def test_drill_ranks_due_recent_error_and_fresh_candidates(self) -> None:
        service.init_profile(self.workspace, level=4)

        def add(prompt: str, section: str = "reading") -> str:
            if section == "reading":
                prompt += "\nA. Library\nB. Café\nC. Station\nD. Office"
            return service.add_item(
                self.workspace,
                section=section,
                subtype="careful_reading" if section == "reading" else "essay",
                prompt=prompt,
            )["item_id"]

        corrected = add("前一次错误，最近一次做对的阅读题")
        wrong = add("最近一次做错的阅读题")
        fresh = add("没有作答记录的阅读题")
        other = add("历史写作题", "writing")
        stamp = local_date().isoformat()
        with sqlite3.connect(db_path(self.workspace)) as connection:
            connection.executemany(
                """INSERT INTO attempts(id,item_id,made_at,result,minutes,judgment_basis)
                   VALUES(?,?,?,?,1,'coach_review')""",
                [
                    ("first", corrected, stamp, "wrong"),
                    ("second", corrected, stamp, "correct"),
                    ("third", wrong, stamp, "wrong"),
                    ("other", other, stamp, "wrong"),
                ],
            )
        self.assertEqual(
            service.drill(self.workspace, section="reading")["item_id"], wrong
        )
        self.assertEqual(
            service.drill(self.workspace, section="reading", fresh=True)["item_id"],
            fresh,
        )
        with sqlite3.connect(db_path(self.workspace)) as connection:
            connection.execute(
                "INSERT INTO review_cards(item_id,due_at,scheduler,updated_at) VALUES(?,?,?,?)",
                (corrected, stamp, "fixed", stamp),
            )
        self.assertEqual(
            service.drill(self.workspace, section="reading")["item_id"], corrected
        )

    def test_reading_drill_only_loads_the_selected_question_body(self) -> None:
        service.init_profile(self.workspace, level=4)
        for index in range(5):
            service.add_item(
                self.workspace,
                section="reading",
                subtype="careful_reading",
                prompt=f"Q{index}: Choose the correct option. A. One B. Two C. Three D. Four",
            )
        fetched = []
        connect = service.connect

        def track(*args, **kwargs):
            conn = connect(*args, **kwargs)

            def inspect(cursor, values):
                if "prompt" in {column[0] for column in cursor.description}:
                    fetched.append(values)
                return sqlite3.Row(cursor, values)

            conn.row_factory = inspect
            return conn

        with patch.object(service, "connect", side_effect=track):
            selected = service.drill(self.workspace, section="reading", fresh=True)
        self.assertTrue(selected["question"])
        self.assertEqual(len(fetched), 1)

    def test_ranked_listening_drill_skips_changed_audio(self) -> None:
        service.init_profile(self.workspace, level=4)
        items = []
        for name in ("original", "fallback"):
            audio = self.root / f"{name}.wav"
            with wave.open(str(audio), "wb") as output:
                output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                output.writeframes(b"\x01\x00" * 800)
            items.append(
                service.add_item(
                    self.workspace,
                    section="listening",
                    subtype="short_news",
                    source_type="original",
                    prompt=f"{name} entrance? A. West B. North C. South D. East",
                    answer="C",
                    answer_status="generated",
                    evidence="Use the south entrance.",
                    transcript="Use the south entrance.",
                    audio_path=str(audio),
                )["item_id"]
            )
        stamp = local_date().isoformat()
        with sqlite3.connect(db_path(self.workspace)) as conn:
            conn.execute(
                "INSERT INTO review_cards(item_id,due_at,scheduler,updated_at) VALUES(?,?,?,?)",
                (items[0], stamp, "fixed", stamp),
            )
        (self.root / "original.wav").write_bytes(b"changed")
        result = service.drill(self.workspace, section="listening")
        self.assertEqual(result["item_id"], items[1])
        self.assertNotIn("transcript", result)
        (self.root / "fallback.wav").unlink()
        with self.assertRaises(CoachError) as exc:
            service.drill(self.workspace, section="listening")
        self.assertEqual(exc.exception.code, "audio_changed")

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

    def test_today_updates_tasks_atomically_on_one_connection(self) -> None:
        service.init_profile(self.workspace, level=4, weak_section="reading")
        initial = service.today(self.workspace)["tasks"]
        self.assertEqual(len(initial), 2)
        completed, pending = (item["id"] for item in initial)
        service.log_task(
            self.workspace, task_id=completed, minutes=20, evidence="独立完成"
        )
        service.set_profile(self.workspace, {"weak_section": "listening"})
        db = db_path(self.workspace)
        with sqlite3.connect(db) as conn:
            conn.execute(
                """CREATE TRIGGER reject_maintenance BEFORE INSERT ON task_logs
                   WHEN NEW.role='maintenance'
                   BEGIN SELECT RAISE(ABORT,'injected failure'); END"""
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected failure"):
            service.today(self.workspace)
        with sqlite3.connect(db) as conn:
            self.assertCountEqual(
                conn.execute("SELECT id FROM task_logs").fetchall(),
                [(completed,), (pending,)],
            )
            conn.execute("DROP TRIGGER reject_maintenance")
        with patch.object(service, "connect", wraps=service.connect) as connect:
            updated = service.today(self.workspace)
        connect.assert_called_once_with(self.workspace)
        current_ids = {task["id"] for task in updated["tasks"]}
        with sqlite3.connect(db) as conn:
            self.assertEqual(
                dict(conn.execute("SELECT id,completed FROM task_logs")),
                {completed: 1, **dict.fromkeys(current_ids, 0)},
            )

    def test_cached_plan_refreshes_days_and_phase_within_one_week(self) -> None:
        exam_day = date(2030, 11, 28)
        service.init_profile(self.workspace, level=4, exam_date=exam_day.isoformat())
        earlier = service.plan(self.workspace, current=exam_day - timedelta(days=2))
        later = service.plan(self.workspace, current=exam_day - timedelta(days=1))
        self.assertEqual(earlier["week_start"], later["week_start"])
        self.assertTrue(later["reused"])
        self.assertEqual(later["days_remaining"], 1)
        self.assertTrue(
            service.today(self.workspace, current=exam_day - timedelta(days=1))["tasks"]
        )
        on_exam = service.today(self.workspace, current=exam_day)
        self.assertEqual(on_exam["tasks"], [])
        self.assertIn("考试日", on_exam["reason"])
        after_exam = service.today(self.workspace, current=exam_day + timedelta(days=1))
        self.assertEqual(after_exam["tasks"], [])
        self.assertIn("已过", after_exam["reason"])

    def test_v1_database_migration_backs_up_and_preserves_attempts(self) -> None:
        service.init_profile(self.workspace, level=4)
        item = service.add_item(
            self.workspace,
            section="reading",
            subtype="careful_reading",
            prompt="When? A. Seven B. Eight C. Nine D. Ten",
            answer="B",
            answer_status="verified",
            evidence="user key",
        )
        service.attempt(self.workspace, item_id=item["item_id"], response="B")
        path = self.workspace / ".cet-sprint" / "state.sqlite3"
        with sqlite3.connect(path) as old:
            old.execute("ALTER TABLE attempts DROP COLUMN practice_mode")
            old.execute("ALTER TABLE checkpoints DROP COLUMN comparison_key")
            old.execute("ALTER TABLE items DROP COLUMN audio_sha256")
            old.execute("ALTER TABLE attempts DROP COLUMN translation_alignment_json")
            old.execute("ALTER TABLE attempts DROP COLUMN revises_attempt_id")
            old.execute("ALTER TABLE attempts DROP COLUMN revision_criterion")
            old.execute("PRAGMA user_version = 1")
        old.close()
        self.assertEqual(
            service.report(self.workspace)["independent_attempts"]["reading"][
                "correct"
            ],
            1,
        )
        backups = list(path.parent.glob("state.sqlite3.v1.*.bak"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                backup.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1
            )
        backup.close()
        with sqlite3.connect(path) as migrated:
            self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(
                migrated.execute("SELECT practice_mode FROM attempts").fetchone()[0],
                "independent",
            )
        migrated.close()

    def test_cet4_listening_blocks_replaced_audio_keeps_prior_attempt(self) -> None:
        service.init_profile(self.workspace, level=4)
        audio = self.root / "broadcast.wav"

        def write_wav(sample):
            with wave.open(str(audio), "wb") as handle:
                handle.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                handle.writeframes(sample * 800)

        write_wav(b"\x01\x00")
        first_bytes = audio.read_bytes()
        item_id = service.add_item(
            self.workspace,
            section="listening",
            subtype="short_news",
            source_type="original",
            prompt="Which entrance? A. West B. North C. South D. East",
            answer="C",
            answer_status="generated",
            evidence="Visitors should use the south entrance.",
            transcript="Visitors should use the south entrance.",
            audio_path=str(audio),
        )["item_id"]
        initial = service.drill(self.workspace, item_id=item_id)
        self.assertNotIn("transcript", initial)
        service.attempt(
            self.workspace, item_id=item_id, response="C", minutes=1, event_id="first"
        )
        original_due = (local_date() + timedelta(days=1)).isoformat()

        write_wav(b"\x20\x00")
        for operation in (
            lambda: service.drill(self.workspace, item_id=item_id),
            lambda: service.attempt(
                self.workspace, item_id=item_id, response="C", event_id="second"
            ),
        ):
            with self.assertRaises(CoachError) as changed:
                operation()
            self.assertEqual(changed.exception.code, "audio_changed")
        review = service.review(self.workspace, through=original_due)["items"][0]
        self.assertFalse(review["audio_ready"])
        self.assertEqual(review["audio_error"], "audio_changed")
        self.assertEqual(review["due_at"], original_due)
        self.assertEqual(service.status(self.workspace)["counts"]["attempts"], 1)
        self.assertTrue(
            service.attempt(
                self.workspace, item_id=item_id, response="C", event_id="first"
            )["duplicate"]
        )
        audio.write_bytes(first_bytes)
        self.assertEqual(
            service.drill(self.workspace, item_id=item_id)["item_id"], item_id
        )
        self.assertTrue(
            service.review(self.workspace, through=original_due)["items"][0][
                "audio_ready"
            ]
        )

    def test_v2_audio_uses_registered_fingerprint_or_requests_rebinding(self) -> None:
        service.init_profile(self.workspace, level=4)
        audio = self.root / "legacy.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            handle.writeframes(b"\x20\x00" * 800)
        material = service.add_material(self.workspace, str(audio))
        linked = service.add_item(
            self.workspace,
            section="listening",
            subtype="short_news",
            material_id=material["material_id"],
            prompt="Where is the event? A. West B. North C. South D. East",
            audio_path=str(audio),
        )["item_id"]
        loose = service.add_item(
            self.workspace,
            section="listening",
            subtype="short_news",
            prompt="When is the event? A. Monday B. Tuesday C. Friday D. Sunday",
            audio_path=str(audio),
        )["item_id"]
        path = db_path(self.workspace)
        old = sqlite3.connect(path)
        with old:
            old.execute("ALTER TABLE items DROP COLUMN audio_sha256")
            old.execute("ALTER TABLE attempts DROP COLUMN translation_alignment_json")
            old.execute("ALTER TABLE attempts DROP COLUMN revises_attempt_id")
            old.execute("ALTER TABLE attempts DROP COLUMN revision_criterion")
            old.execute("PRAGMA user_version = 2")
        old.close()
        self.assertEqual(
            service.drill(self.workspace, item_id=linked)["item_id"], linked
        )
        with self.assertRaises(CoachError) as unbound:
            service.drill(self.workspace, item_id=loose)
        self.assertEqual(unbound.exception.code, "audio_binding_unverified")
        self.assertTrue(
            service.update_item(self.workspace, loose, audio_path=str(audio))[
                "audio_ready"
            ]
        )
        self.assertEqual(service.drill(self.workspace, item_id=loose)["item_id"], loose)
        self.assertEqual(len(list(path.parent.glob("state.sqlite3.v2.*.bak"))), 1)

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
            evidence="Time is limited.",
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
