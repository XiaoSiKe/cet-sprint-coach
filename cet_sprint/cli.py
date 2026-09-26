"""JSON CLI for agent use. Learners normally use the Skill through conversation."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import service
from .storage import CoachError


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "invalid_arguments",
                    "message": message,
                    "recovery": "运行 --help 查看命令和参数。",
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)


def _text_arg(literal: str | None, filename: str | None, label: str) -> str | None:
    if literal is not None and filename is not None:
        raise CoachError(
            "ambiguous_text",
            f"{label} 只能使用文本或文件中的一种。",
            "去掉其中一个参数。",
        )
    if filename is not None:
        path = Path(filename).expanduser().resolve()
        if not path.is_file():
            raise CoachError(
                "text_file_not_found", f"找不到 {label} 文件：{path}", "核对路径。"
            )
        return path.read_text(encoding="utf-8")
    return literal


def make_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="cet.py",
        description="Local CET-4/CET-6 coaching engine; responses are JSON.",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="Student workspace; state lives in WORKSPACE/.cet-sprint/",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor", help="Check runtime and local state")
    commands.add_parser("status", help="Read profile and concise state")
    init = commands.add_parser("init", help="Create a learner profile")
    init.add_argument("--level", type=int, choices=(4, 6), required=True)
    init.add_argument("--exam-date")
    init.add_argument("--target-score", type=int)
    init.add_argument("--weekday-minutes", type=int, default=60)
    init.add_argument("--weekend-minutes", type=int, default=90)
    init.add_argument(
        "--weak-section",
        choices=("listening", "reading", "writing", "translation", "vocab"),
    )

    profile = commands.add_parser(
        "profile", help="Update learner reality; triggers replan"
    )
    profile_sub = profile.add_subparsers(dest="action", required=True)
    set_profile = profile_sub.add_parser("set")
    set_profile.add_argument(
        "--level", type=int, choices=(4, 6), default=argparse.SUPPRESS
    )
    set_profile.add_argument(
        "--exam-date", default=argparse.SUPPRESS, help="YYYY-MM-DD or none to clear"
    )
    set_profile.add_argument(
        "--target-score", default=argparse.SUPPRESS, help="0-710 or none to clear"
    )
    set_profile.add_argument("--weekday-minutes", type=int, default=argparse.SUPPRESS)
    set_profile.add_argument("--weekend-minutes", type=int, default=argparse.SUPPRESS)
    set_profile.add_argument(
        "--weak-section", default=argparse.SUPPRESS, help="section or none to clear"
    )

    material = commands.add_parser(
        "material", help="Import and manage source-grounded materials/items"
    )
    material_sub = material.add_subparsers(dest="action", required=True)
    add = material_sub.add_parser(
        "add", help="Index one user-selected local file without editing it"
    )
    add.add_argument("path")
    add.add_argument(
        "--source-type",
        choices=("official", "past_paper", "user_material", "original"),
        default="user_material",
    )
    add.add_argument(
        "--section", choices=("listening", "reading", "writing", "translation", "vocab")
    )
    add.add_argument("--subtype")
    add.add_argument("--year", type=int)
    add.add_argument("--locator")
    add.add_argument("--source-url")
    add.add_argument("--verified-at")
    add.add_argument(
        "--confidence", choices=("high", "medium", "low"), default="medium"
    )
    material_sub.add_parser("list", help="List material metadata")
    item_add = material_sub.add_parser(
        "item-add", help="Add one question or writing/translation prompt"
    )
    item_add.add_argument("--material-id")
    item_add.add_argument(
        "--source-type",
        choices=("official", "past_paper", "user_material", "original"),
        default="user_material",
    )
    item_add.add_argument(
        "--section",
        choices=("listening", "reading", "writing", "translation", "vocab"),
        required=True,
    )
    item_add.add_argument("--subtype", required=True)
    item_add.add_argument("--prompt")
    item_add.add_argument("--prompt-file")
    item_add.add_argument("--answer")
    item_add.add_argument("--answer-file")
    item_add.add_argument(
        "--answer-status",
        choices=("unverified", "verified", "generated"),
        default="unverified",
    )
    item_add.add_argument("--evidence")
    item_add.add_argument("--locator")
    item_add.add_argument("--audio-path")
    item_add.add_argument("--transcript-file")
    batch_add = material_sub.add_parser(
        "batch-add", help="Atomically import 1-500 structured items from JSON"
    )
    batch_add.add_argument("--input", required=True, help="JSON manifest path")
    item_update = material_sub.add_parser(
        "item-update", help="Verify answer, attach audio or deactivate item"
    )
    item_update.add_argument("--item-id", required=True)
    item_update.add_argument("--answer")
    item_update.add_argument("--answer-file")
    item_update.add_argument(
        "--answer-status", choices=("unverified", "verified", "generated")
    )
    item_update.add_argument("--evidence")
    item_update.add_argument("--locator")
    item_update.add_argument("--audio-path")
    item_update.add_argument("--transcript-file")
    item_update.add_argument("--deactivate", action="store_true")
    item_list = material_sub.add_parser(
        "items", help="List item metadata, without answers"
    )
    item_list.add_argument(
        "--section", choices=("listening", "reading", "writing", "translation", "vocab")
    )
    synth = material_sub.add_parser(
        "synth", help="Generate macOS auxiliary audio for an original listening item"
    )
    synth.add_argument("--item-id", required=True)

    plan = commands.add_parser("plan", help="Create or read the current week plan")
    plan.add_argument("--force", action="store_true")
    today = commands.add_parser("today", help="List or log up to three daily tasks")
    today_sub = today.add_subparsers(dest="action")
    task_log = today_sub.add_parser(
        "log", help="Mark a daily task complete with evidence"
    )
    task_log.add_argument("--task-id", required=True)
    task_log.add_argument("--minutes", type=float, required=True)
    task_log.add_argument("--evidence", required=True)
    drill = commands.add_parser(
        "drill", help="Select one usable item, hiding answer and transcript"
    )
    drill.add_argument(
        "--section",
        choices=("listening", "reading", "writing", "translation", "vocab"),
    )
    drill.add_argument("--subtype")
    drill.add_argument("--item-id", help="Select an exact due-review item")
    attempt = commands.add_parser(
        "attempt", help="Record actual response; only then reveal source answer"
    )
    attempt.add_argument("--item-id", required=True)
    attempt.add_argument("--response")
    attempt.add_argument("--response-file")
    attempt.add_argument(
        "--result", choices=("correct", "partial", "wrong", "unverified")
    )
    attempt.add_argument("--minutes", type=float, default=0)
    attempt.add_argument("--error-tag")
    attempt.add_argument("--feedback")
    attempt.add_argument("--feedback-file")
    attempt.add_argument("--evidence")
    attempt.add_argument("--save-response", action="store_true")
    attempt.add_argument("--rating", choices=("easy",))
    attempt.add_argument("--event-id")
    checkpoint = commands.add_parser(
        "checkpoint", help="Keep official score separate from mock raw accuracy"
    )
    checkpoint.add_argument("--kind", choices=("official", "mock"), required=True)
    checkpoint.add_argument("--date", required=True)
    checkpoint.add_argument("--source", required=True)
    checkpoint.add_argument(
        "--section", choices=("listening", "reading", "writing", "translation")
    )
    checkpoint.add_argument("--correct", type=int)
    checkpoint.add_argument("--question-total", type=int)
    checkpoint.add_argument("--minutes", type=float)
    checkpoint.add_argument("--score-total", type=int)
    checkpoint.add_argument("--score-listening", type=int)
    checkpoint.add_argument("--score-reading", type=int)
    checkpoint.add_argument("--score-writing-translation", type=int)
    checkpoint.add_argument("--event-id")
    review = commands.add_parser("review", help="Read due review metadata")
    review.add_argument("--limit", type=int, default=20)
    report = commands.add_parser(
        "report", help="Separate execution, answers, errors and checkpoints"
    )
    report.add_argument("--days", type=int, default=7)
    efficiency = commands.add_parser(
        "efficiency",
        help="Interpret execution and independent evidence without a composite score",
    )
    efficiency.add_argument("--days", type=int, default=7)
    export = commands.add_parser(
        "export", help="Write a readable JSON or Markdown export"
    )
    export.add_argument("--format", choices=("json", "md"), required=True)
    export.add_argument("--output")
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Path(args.workspace)
    if args.command == "doctor":
        return service.doctor(workspace)
    if args.command == "status":
        return service.status(workspace)
    if args.command == "init":
        return service.init_profile(
            workspace,
            level=args.level,
            exam_date=args.exam_date,
            target_score=args.target_score,
            weekday_minutes=args.weekday_minutes,
            weekend_minutes=args.weekend_minutes,
            weak_section=args.weak_section,
        )
    if args.command == "profile":
        changes = {
            key: value
            for key, value in vars(args).items()
            if key
            in {
                "level",
                "exam_date",
                "target_score",
                "weekday_minutes",
                "weekend_minutes",
                "weak_section",
            }
        }
        for key in ("exam_date", "target_score", "weak_section"):
            if changes.get(key) == "none":
                changes[key] = None
        if isinstance(changes.get("target_score"), str):
            try:
                changes["target_score"] = int(changes["target_score"])
            except ValueError as exc:
                raise CoachError(
                    "invalid_target", "目标分需要整数或 none。", "核对 --target-score。"
                ) from exc
        if not changes:
            raise CoachError(
                "no_profile_change",
                "没有提供要更新的档案字段。",
                "使用 profile set --help。",
            )
        return service.set_profile(workspace, changes)
    if args.command == "material":
        if args.action == "add":
            return service.add_material(
                workspace,
                args.path,
                source_type=args.source_type,
                section=args.section,
                subtype=args.subtype,
                year=args.year,
                locator=args.locator,
                source_url=args.source_url,
                verified_at=args.verified_at,
                confidence=args.confidence,
            )
        if args.action == "list":
            return service.list_materials(workspace)
        if args.action == "items":
            return service.list_items(workspace, args.section)
        if args.action == "synth":
            return service.synthesize_item(workspace, args.item_id)
        if args.action == "item-add":
            prompt = _text_arg(args.prompt, args.prompt_file, "题目")
            answer = _text_arg(args.answer, args.answer_file, "答案")
            transcript = _text_arg(None, args.transcript_file, "听力脚本")
            return service.add_item(
                workspace,
                material_id=args.material_id,
                source_type=args.source_type,
                section=args.section,
                subtype=args.subtype,
                prompt=prompt or "",
                answer=answer,
                answer_status=args.answer_status,
                evidence=args.evidence,
                locator=args.locator,
                audio_path=args.audio_path,
                transcript=transcript,
            )
        if args.action == "batch-add":
            path = Path(args.input).expanduser().resolve()
            if not path.is_file():
                raise CoachError(
                    "batch_not_found",
                    f"找不到批量题目文件：{path}",
                    "核对 --input 路径。",
                )
            if path.stat().st_size > 5 * 1024 * 1024:
                raise CoachError(
                    "batch_too_large",
                    "批量题目文件超过 5 MiB。",
                    "按材料拆成较小批次。",
                )
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CoachError(
                    "invalid_batch_json",
                    "批量题目文件不是有效 UTF-8 JSON。",
                    "修正 JSON 后重试。",
                ) from exc
            if (
                not isinstance(manifest, dict)
                or set(manifest) - {"schema_version", "material_id", "items"}
                or manifest.get("schema_version") != 1
            ):
                raise CoachError(
                    "invalid_batch",
                    "批量清单需要 schema_version=1、items 和可选 material_id。",
                    "核对 references/materials.md 中的格式。",
                )
            return service.add_items_batch(
                workspace,
                manifest.get("items"),
                material_id=manifest.get("material_id"),
            )
        if args.action == "item-update":
            answer = _text_arg(args.answer, args.answer_file, "答案")
            transcript = _text_arg(None, args.transcript_file, "听力脚本")
            return service.update_item(
                workspace,
                args.item_id,
                answer=answer,
                answer_status=args.answer_status,
                evidence=args.evidence,
                locator=args.locator,
                audio_path=args.audio_path,
                transcript=transcript,
                active=False if args.deactivate else None,
            )
    if args.command == "plan":
        return service.plan(workspace, force=args.force)
    if args.command == "today":
        return (
            service.log_task(
                workspace,
                task_id=args.task_id,
                minutes=args.minutes,
                evidence=args.evidence,
            )
            if args.action == "log"
            else service.today(workspace)
        )
    if args.command == "drill":
        return service.drill(
            workspace, section=args.section, subtype=args.subtype, item_id=args.item_id
        )
    if args.command == "attempt":
        response = _text_arg(args.response, args.response_file, "作答")
        feedback = _text_arg(args.feedback, args.feedback_file, "反馈")
        return service.attempt(
            workspace,
            item_id=args.item_id,
            response=response or "",
            result=args.result,
            minutes=args.minutes,
            error_tag=args.error_tag,
            feedback=feedback,
            evidence=args.evidence,
            save_response=args.save_response,
            rating=args.rating,
            event_id=args.event_id,
        )
    if args.command == "checkpoint":
        return service.add_checkpoint(
            workspace,
            kind=args.kind,
            taken_on=args.date,
            source=args.source,
            section=args.section,
            correct=args.correct,
            question_total=args.question_total,
            minutes=args.minutes,
            score_total=args.score_total,
            score_listening=args.score_listening,
            score_reading=args.score_reading,
            score_writing_translation=args.score_writing_translation,
            event_id=args.event_id,
        )
    if args.command == "review":
        return service.review(workspace, limit=args.limit)
    if args.command == "report":
        return service.report(workspace, days=args.days)
    if args.command == "efficiency":
        return service.efficiency(workspace, days=args.days)
    if args.command == "export":
        return service.export_state(
            workspace, format_name=args.format, output=args.output
        )
    raise CoachError("unknown_command", "未知命令。", "运行 --help。")


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        result = dispatch(args)
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))
        return 0
    except CoachError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": exc.code,
                    "message": exc.message,
                    "recovery": exc.recovery,
                },
                ensure_ascii=False,
            )
        )
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "storage_error",
                    "message": str(exc),
                    "recovery": "检查学习目录权限与磁盘状态；不要删除原档案，可先从备份恢复。",
                },
                ensure_ascii=False,
            )
        )
        return 3
    except Exception:  # noqa: BLE001 - keep the JSON protocol intact on unexpected failures
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "internal_error",
                    "message": "引擎出现未预期错误，本次操作不能视为已保存。",
                    "recovery": "保留 .cet-sprint/ 与原材料，记录操作命令并检查版本；不要删除数据库。",
                },
                ensure_ascii=False,
            )
        )
        return 4


if __name__ == "__main__":
    sys.exit(main())
