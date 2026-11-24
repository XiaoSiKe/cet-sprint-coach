"""Application operations for the local CET coaching engine."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from itertools import chain
from pathlib import Path
from typing import Any

from . import __version__
from .alignment import alignment_delta, validate_alignment
from .materials import (
    extract,
    sha256_file,
    synthesize_mac_audio,
    validate_audio,
    validate_file,
)
from .quality import validate_item_quality
from .scheduling import fsrs_status, next_review
from .storage import (
    SCHEMA_VERSION,
    SECTIONS,
    CoachError,
    check_section,
    connect,
    db_path,
    json_text,
    local_date,
    now_iso,
    parse_date,
    require_profile,
    row_dict,
    stable_id,
    state_root,
    today_iso,
    validate_material_source,
)


def doctor(workspace: Path) -> dict[str, Any]:
    try:
        import pypdf

        pdf = {"available": True, "version": getattr(pypdf, "__version__", "installed")}
    except ImportError:
        pdf = {"available": False, "version": None}
    return {
        "python": sys.version.split()[0],
        "engine_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "state_exists": db_path(workspace).is_file(),
        "state_path": str(db_path(workspace)),
        "fsrs": fsrs_status(),
        "pypdf": pdf,
        "ffprobe_available": shutil.which("ffprobe") is not None,
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "macos_say_available": shutil.which("say") is not None,
        "official_sources_checked_at": "2026-09-26",
        "official_refresh_needed": (local_date() - date(2026, 9, 26)).days > 30,
    }


def init_profile(
    workspace: Path,
    *,
    level: int,
    exam_date: str | None = None,
    target_score: int | None = None,
    weekday_minutes: int = 60,
    weekend_minutes: int = 90,
    weak_section: str | None = None,
) -> dict[str, Any]:
    if level not in (4, 6):
        raise CoachError(
            "invalid_level", "只支持 CET-4 或 CET-6。", "使用 --level 4 或 6。"
        )
    exam_date = parse_date(exam_date, "exam_date")
    if target_score is not None and not 0 <= target_score <= 710:
        raise CoachError(
            "invalid_target", "目标报道分需在 0–710 之间。", "调整 --target-score。"
        )
    if not 0 <= weekday_minutes <= 720 or not 0 <= weekend_minutes <= 720:
        raise CoachError(
            "invalid_capacity", "每日可用分钟需在 0–720 之间。", "按现实时间重新填写。"
        )
    if weak_section is not None:
        check_section(level, weak_section)
    conn = connect(workspace, create=True)
    try:
        old = row_dict(conn.execute("SELECT * FROM profile WHERE id=1").fetchone())
        if old:
            if old["level"] != level:
                raise CoachError(
                    "level_conflict",
                    f"已有 CET-{old['level']} 档案，不能静默改成 CET-{level}。",
                    "为另一等级选择新学习目录，或使用 profile set --level 明确切换。",
                )
            return {
                "created": False,
                "profile": old,
                "state_path": str(db_path(workspace)),
            }
        with conn:
            conn.execute(
                "INSERT INTO profile VALUES (1,?,?,?,?,?,?,?)",
                (
                    level,
                    exam_date,
                    target_score,
                    weekday_minutes,
                    weekend_minutes,
                    weak_section,
                    now_iso(),
                ),
            )
        return {
            "created": True,
            "profile": require_profile(conn),
            "state_path": str(db_path(workspace)),
        }
    finally:
        conn.close()


def set_profile(workspace: Path, changes: dict[str, Any]) -> dict[str, Any]:
    conn = connect(workspace)
    try:
        old = require_profile(conn)
        allowed = {
            "level",
            "exam_date",
            "target_score",
            "weekday_minutes",
            "weekend_minutes",
            "weak_section",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise CoachError(
                "invalid_profile_field",
                f"不支持的档案字段：{', '.join(sorted(unknown))}",
                "使用 profile set --help 核对。",
            )
        updated = {**old, **changes}
        level = int(updated["level"])
        if level not in (4, 6):
            raise CoachError(
                "invalid_level", "只支持 CET-4 或 CET-6。", "使用 --level 4 或 6。"
            )
        if level != old["level"]:
            existing = (
                conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                + conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
            )
            if existing:
                raise CoachError(
                    "level_has_history",
                    "已有题目或成绩记录，不能在同一档案切换级别。",
                    "为新等级建立独立学习目录。",
                )
        exam_date = parse_date(updated["exam_date"], "exam_date")
        score = updated["target_score"]
        if score is not None and not 0 <= int(score) <= 710:
            raise CoachError(
                "invalid_target", "目标报道分需在 0–710 之间。", "调整 --target-score。"
            )
        for field in ("weekday_minutes", "weekend_minutes"):
            if not 0 <= int(updated[field]) <= 720:
                raise CoachError(
                    "invalid_capacity",
                    f"{field} 需在 0–720 分钟之间。",
                    "按现实时间重新填写。",
                )
        if updated["weak_section"] is not None:
            check_section(level, updated["weak_section"])
        with conn:
            conn.execute(
                "UPDATE profile SET level=?, exam_date=?, target_score=?, weekday_minutes=?, weekend_minutes=?, weak_section=?, updated_at=? WHERE id=1",
                (
                    level,
                    exam_date,
                    score,
                    updated["weekday_minutes"],
                    updated["weekend_minutes"],
                    updated["weak_section"],
                    now_iso(),
                ),
            )
            conn.execute("DELETE FROM plans")
        return {"profile": require_profile(conn), "replan_required": True}
    finally:
        conn.close()


def status(workspace: Path) -> dict[str, Any]:
    conn = connect(workspace)
    try:
        profile = require_profile(conn)
        counts = {
            "materials": conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0],
            "active_items": conn.execute(
                "SELECT COUNT(*) FROM items WHERE active=1"
            ).fetchone()[0],
            "attempts": conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
            "due_reviews": conn.execute(
                "SELECT COUNT(*) FROM review_cards WHERE due_at<=?", (today_iso(),)
            ).fetchone()[0],
        }
        latest_official = row_dict(
            conn.execute(
                "SELECT * FROM checkpoints WHERE kind='official' ORDER BY taken_on DESC, created_at DESC LIMIT 1"
            ).fetchone()
        )
        latest_mock = row_dict(
            conn.execute(
                "SELECT * FROM checkpoints WHERE kind='mock' ORDER BY taken_on DESC, created_at DESC LIMIT 1"
            ).fetchone()
        )
        current = local_date()
        week = (current - timedelta(days=current.weekday())).isoformat()
        plan_row = row_dict(
            conn.execute(
                "SELECT focus,basis,generated_at FROM plans WHERE week_start=?", (week,)
            ).fetchone()
        )
        return {
            "profile": profile,
            "counts": counts,
            "latest_official": latest_official,
            "latest_mock": latest_mock,
            "week_plan": plan_row,
        }
    finally:
        conn.close()


def add_material(
    workspace: Path,
    path_text: str,
    *,
    source_type: str = "user_material",
    section: str | None = None,
    subtype: str | None = None,
    year: int | None = None,
    locator: str | None = None,
    source_url: str | None = None,
    verified_at: str | None = None,
    confidence: str = "medium",
) -> dict[str, Any]:
    conn = connect(workspace)
    try:
        profile = require_profile(conn)
        if confidence not in {"high", "medium", "low"}:
            raise CoachError(
                "invalid_confidence",
                "归类置信度需为 high、medium 或 low。",
                "核对 --confidence。",
            )
        validate_material_source(
            source_type, year, source_url, parse_date(verified_at, "verified_at")
        )
        if source_type == "past_paper" and not (locator or source_url):
            raise CoachError(
                "past_paper_evidence_missing",
                "真题材料还缺卷次或可核验位置。",
                "补充 --locator（例如卷次及封面位置）或 --source-url；否则改为 user_material。",
            )
        if section is not None:
            check_section(profile["level"], section, subtype)
        if subtype is not None and section is None:
            raise CoachError(
                "subtype_without_section", "题型需要对应模块。", "同时提供 --section。"
            )
        path = validate_file(path_text)
        digest = sha256_file(path)
        existing = row_dict(
            conn.execute("SELECT * FROM materials WHERE sha256=?", (digest,)).fetchone()
        )
        if existing:
            existing.pop("extracted_text", None)
            return {"added": False, "duplicate": True, "material": existing}
        extracted, warnings = extract(path)
        material_id = stable_id("material", digest)
        # A low-confidence classification stays unbound; medium confidence never binds a subtype.
        bound_section = section if confidence != "low" else None
        bound_subtype = subtype if confidence == "high" else None
        with conn:
            conn.execute(
                "INSERT INTO materials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    material_id,
                    str(path),
                    digest,
                    profile["level"],
                    bound_section,
                    bound_subtype,
                    source_type,
                    year,
                    locator,
                    source_url,
                    verified_at,
                    confidence,
                    extracted,
                    json_text(warnings),
                    now_iso(),
                ),
            )
        return {
            "added": True,
            "duplicate": False,
            "material_id": material_id,
            "path": str(path),
            "sha256": digest,
            "section": bound_section,
            "subtype": bound_subtype,
            "characters_extracted": len(extracted),
            "warnings": warnings,
            "candidate_items": 0,
        }
    finally:
        conn.close()


def list_materials(workspace: Path) -> dict[str, Any]:
    conn = connect(workspace)
    try:
        require_profile(conn)
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT id,path,sha256,level,section,subtype,source_type,year,locator,source_url,verified_at,confidence,warnings_json,created_at FROM materials ORDER BY created_at DESC"
            )
        ]
        for row in rows:
            row["warnings"] = json.loads(row.pop("warnings_json"))
        return {"materials": rows}
    finally:
        conn.close()


def _audio_fingerprint(path: Path) -> str:
    try:
        return sha256_file(path)
    except OSError as exc:
        raise CoachError(
            "audio_missing",
            "无法读取绑定的音频文件。",
            "恢复原文件或核对后重新绑定。",
        ) from exc


def _bind_audio(raw: str, material: dict[str, Any] | None) -> tuple[str, str]:
    path = validate_audio(raw)
    fingerprint = _audio_fingerprint(path)
    if material and material["path"] == str(path) and material["sha256"] != fingerprint:
        raise CoachError(
            "audio_changed",
            "当前音频与已登记来源的文件不一致。",
            "恢复匹配的原音频；替代音频需单独核对来源和脚本。",
        )
    return str(path), fingerprint


def _check_bound_audio(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    fingerprints: dict[str, str] | None = None,
) -> None:
    raw = item.get("audio_path")
    if not raw:
        raise CoachError(
            "audio_missing", "听力题尚未绑定音频。", "核对脚本后绑定可播放音频。"
        )
    actual = fingerprints.get(raw) if fingerprints is not None else None
    if actual is None:
        actual = _audio_fingerprint(Path(raw))
        if fingerprints is not None:
            fingerprints[raw] = actual
    expected = item.get("audio_sha256")
    if not expected and item.get("material_id"):
        material = conn.execute(
            "SELECT path,sha256 FROM materials WHERE id=?", (item["material_id"],)
        ).fetchone()
        if material and material["path"] == raw:
            expected = material["sha256"]
    if not expected:
        raise CoachError(
            "audio_binding_unverified",
            "旧音频绑定缺少可核对的历史指纹。",
            "核对音频、脚本和题目后，用 material item-update --audio-path 显式重绑。",
        )
    if actual != expected:
        raise CoachError(
            "audio_changed",
            "音频同路径文件已变化，无法确认与原题匹配。",
            "恢复校验一致的原音频，或核对来源和脚本后重新绑定。",
        )


def _add_item_conn(
    conn: sqlite3.Connection,
    profile: dict[str, Any],
    *,
    section: str,
    subtype: str,
    prompt: str,
    material_id: str | None = None,
    source_type: str = "user_material",
    answer: str | None = None,
    answer_status: str = "unverified",
    evidence: str | None = None,
    locator: str | None = None,
    audio_path: str | None = None,
    transcript: str | None = None,
) -> dict[str, Any]:
    if not all(isinstance(value, str) for value in (section, subtype, prompt)):
        raise CoachError(
            "invalid_item", "题目模块、题型和题干都必须是文本。", "核对题目字段。"
        )
    for field, value in {
        "material_id": material_id,
        "source_type": source_type,
        "answer": answer,
        "answer_status": answer_status,
        "evidence": evidence,
        "locator": locator,
        "audio_path": audio_path,
        "transcript": transcript,
    }.items():
        if value is not None and not isinstance(value, str):
            raise CoachError(
                "invalid_item", f"{field} 必须是文本或空值。", "核对题目字段。"
            )
    check_section(profile["level"], section, subtype)
    prompt = prompt.strip()
    if not prompt:
        raise CoachError("empty_prompt", "题目内容为空。", "提供题干或写译提示。")
    if answer_status not in {"unverified", "verified", "generated"}:
        raise CoachError(
            "invalid_answer_status",
            "答案状态无效。",
            "使用 unverified、verified 或 generated。",
        )
    material = None
    if material_id:
        material = row_dict(
            conn.execute(
                "SELECT * FROM materials WHERE id=?", (material_id,)
            ).fetchone()
        )
        if material is None:
            raise CoachError(
                "material_not_found",
                "找不到关联材料。",
                "先运行 material list 核对 ID。",
            )
        source_type = material["source_type"]
        if material["level"] != profile["level"]:
            raise CoachError(
                "level_conflict", "材料级别与档案不一致。", "改用正确级别的材料。"
            )
        if material["section"] and material["section"] != section:
            raise CoachError(
                "material_section_conflict",
                "题目模块与材料归类冲突。",
                "核对材料与题型；不要静默改写来源。",
            )
        if material["subtype"] and material["subtype"] != subtype:
            raise CoachError(
                "material_subtype_conflict", "题型与材料归类冲突。", "核对具体题型。"
            )
    if source_type not in {"official", "past_paper", "user_material", "original"}:
        raise CoachError(
            "invalid_source_type",
            "题目来源类型无效。",
            "选择 user_material 或 original，或关联已导入材料。",
        )
    if material is None and source_type in {"official", "past_paper"}:
        raise CoachError(
            "unlinked_claim",
            "官方题或真题必须关联已核验材料。",
            "先用 material add 建来源记录，再提供 --material-id。",
        )
    if answer_status == "verified" and (not answer or not (evidence or locator)):
        raise CoachError(
            "unverified_answer",
            "已核验答案需同时有答案和依据位置。",
            "补充 --answer 及 --evidence 或 --locator；否则保持 unverified。",
        )
    if answer_status == "verified" and source_type == "original":
        raise CoachError(
            "original_not_official",
            "原创题不能标为来源核验答案。",
            "使用 --answer-status generated。",
        )
    if answer_status == "generated" and (source_type != "original" or not answer):
        raise CoachError(
            "invalid_generated_answer",
            "生成答案只用于有答案的原创题。",
            "使用 --source-type original 并提供 --answer。",
        )
    if section in {"writing", "translation"} and answer_status == "verified":
        raise CoachError(
            "subjective_unique_answer",
            "写作和翻译没有单一标准答案。",
            "将参考文本标为 generated 或保持 unverified，批改时说明依据。",
        )
    validate_item_quality(
        section=section,
        subtype=subtype,
        prompt=prompt,
        answer=answer,
        answer_status=answer_status,
        evidence=evidence,
        transcript=transcript,
        source_type=source_type,
        material_text=material["extracted_text"] if material else "",
    )
    if section != "listening" and (audio_path or transcript):
        raise CoachError(
            "audio_wrong_section",
            "音频和脚本只能绑定听力题。",
            "改用 --section listening。",
        )
    audio, fingerprint = (
        _bind_audio(audio_path, material) if audio_path else (None, None)
    )
    item_id = stable_id(
        "item",
        material_id or source_type,
        str(profile["level"]),
        section,
        subtype,
        prompt,
    )
    existing = row_dict(
        conn.execute("SELECT id,active FROM items WHERE id=?", (item_id,)).fetchone()
    )
    if existing:
        return {"added": False, "item_id": item_id, "duplicate": True}
    conn.execute(
        """INSERT INTO items(
            id,material_id,level,section,subtype,prompt,answer,answer_status,evidence,locator,
            audio_path,transcript,source_type,active,created_at,audio_sha256
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item_id,
            material_id,
            profile["level"],
            section,
            subtype,
            prompt,
            answer,
            answer_status,
            evidence,
            locator,
            audio,
            transcript,
            source_type,
            1,
            now_iso(),
            fingerprint,
        ),
    )
    return {
        "added": True,
        "item_id": item_id,
        "source_type": source_type,
        "answer_status": answer_status,
        "audio_ready": bool(audio),
    }


def add_item(
    workspace: Path,
    *,
    section: str,
    subtype: str,
    prompt: str,
    material_id: str | None = None,
    source_type: str = "user_material",
    answer: str | None = None,
    answer_status: str = "unverified",
    evidence: str | None = None,
    locator: str | None = None,
    audio_path: str | None = None,
    transcript: str | None = None,
) -> dict[str, Any]:
    conn = connect(workspace)
    try:
        profile = require_profile(conn)
        with conn:
            return _add_item_conn(
                conn,
                profile,
                section=section,
                subtype=subtype,
                prompt=prompt,
                material_id=material_id,
                source_type=source_type,
                answer=answer,
                answer_status=answer_status,
                evidence=evidence,
                locator=locator,
                audio_path=audio_path,
                transcript=transcript,
            )
    finally:
        conn.close()


def add_items_batch(
    workspace: Path, items: list[dict[str, Any]], *, material_id: str | None = None
) -> dict[str, Any]:
    if not isinstance(items, list) or not 1 <= len(items) <= 500:
        raise CoachError(
            "invalid_batch", "题目批次必须包含 1–500 条。", "按材料拆成较小批次。"
        )
    allowed = {
        "material_id",
        "source_type",
        "section",
        "subtype",
        "prompt",
        "answer",
        "answer_status",
        "evidence",
        "locator",
        "audio_path",
        "transcript",
    }
    conn = connect(workspace)
    try:
        profile = require_profile(conn)
        added: list[str] = []
        skipped: list[str] = []
        with conn:
            for index, item in enumerate(items, 1):
                if not isinstance(item, dict):
                    raise CoachError(
                        "invalid_batch",
                        f"第 {index} 条题目不是对象。",
                        "每条题目使用 JSON 对象。",
                    )
                unknown = set(item) - allowed
                if unknown:
                    raise CoachError(
                        "invalid_batch",
                        f"第 {index} 条有未知字段：{', '.join(sorted(unknown))}。",
                        "核对批量导入字段。",
                    )
                try:
                    result = _add_item_conn(
                        conn,
                        profile,
                        section=item.get("section"),
                        subtype=item.get("subtype"),
                        prompt=item.get("prompt"),
                        material_id=item.get("material_id", material_id),
                        source_type=item.get("source_type", "user_material"),
                        answer=item.get("answer"),
                        answer_status=item.get("answer_status", "unverified"),
                        evidence=item.get("evidence"),
                        locator=item.get("locator"),
                        audio_path=item.get("audio_path"),
                        transcript=item.get("transcript"),
                    )
                except CoachError as exc:
                    raise CoachError(
                        exc.code, f"第 {index} 条：{exc.message}", exc.recovery
                    ) from exc
                (added if result["added"] else skipped).append(result["item_id"])
        return {
            "added_count": len(added),
            "skipped_count": len(skipped),
            "added_item_ids": added,
            "skipped_item_ids": skipped,
        }
    finally:
        conn.close()


def update_item(
    workspace: Path,
    item_id: str,
    *,
    answer: str | None = None,
    answer_status: str | None = None,
    evidence: str | None = None,
    locator: str | None = None,
    audio_path: str | None = None,
    transcript: str | None = None,
    active: bool | None = None,
) -> dict[str, Any]:
    conn = connect(workspace)
    try:
        item = row_dict(
            conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        )
        if item is None:
            raise CoachError(
                "item_not_found", "找不到题目。", "用 material items 查看 ID。"
            )
        material = (
            row_dict(
                conn.execute(
                    "SELECT path,sha256,extracted_text FROM materials WHERE id=?",
                    (item["material_id"],),
                ).fetchone()
            )
            if item["material_id"]
            else None
        )
        if audio_path and item["section"] != "listening":
            raise CoachError(
                "audio_wrong_section", "音频只能绑定听力题。", "核对题目模块。"
            )
        audio, fingerprint = (
            _bind_audio(audio_path, material)
            if audio_path
            else (item["audio_path"], item["audio_sha256"])
        )
        changes = {
            "answer": answer if answer is not None else item["answer"],
            "answer_status": answer_status
            if answer_status is not None
            else item["answer_status"],
            "evidence": evidence if evidence is not None else item["evidence"],
            "locator": locator if locator is not None else item["locator"],
            "audio_path": audio,
            "transcript": transcript if transcript is not None else item["transcript"],
            "active": int(active) if active is not None else item["active"],
            "audio_sha256": fingerprint,
        }
        if changes["answer_status"] == "verified" and (
            not changes["answer"] or not (changes["evidence"] or changes["locator"])
        ):
            raise CoachError(
                "unverified_answer",
                "已核验答案需同时有答案和依据位置。",
                "补充答案和证据。",
            )
        if changes["answer_status"] == "verified" and item["source_type"] == "original":
            raise CoachError(
                "original_not_official",
                "原创题不能标为来源核验答案。",
                "使用 generated。",
            )
        if (
            item["section"] in {"writing", "translation"}
            and changes["answer_status"] == "verified"
        ):
            raise CoachError(
                "subjective_unique_answer",
                "写作和翻译没有单一标准答案。",
                "将参考文本标为 generated 或保持 unverified。",
            )
        if changes["answer_status"] == "generated" and (
            item["source_type"] != "original" or not changes["answer"]
        ):
            raise CoachError(
                "invalid_generated_answer",
                "生成答案只用于有答案的原创题。",
                "核对来源和答案。",
            )
        if item["section"] != "listening" and (
            changes["audio_path"] or changes["transcript"]
        ):
            raise CoachError(
                "audio_wrong_section", "音频和脚本只能绑定听力题。", "核对题目模块。"
            )
        material_text = material["extracted_text"] if material else ""
        validate_item_quality(
            section=item["section"],
            subtype=item["subtype"],
            prompt=item["prompt"],
            answer=changes["answer"],
            answer_status=changes["answer_status"],
            evidence=changes["evidence"],
            transcript=changes["transcript"],
            source_type=item["source_type"],
            material_text=material_text,
        )
        with conn:
            conn.execute(
                "UPDATE items SET answer=?,answer_status=?,evidence=?,locator=?,audio_path=?,transcript=?,active=?,audio_sha256=? WHERE id=?",
                (*changes.values(), item_id),
            )
        audio_issue = None
        if item["section"] == "listening":
            try:
                _check_bound_audio(conn, {**item, **changes})
            except CoachError as exc:
                audio_issue = exc.code
        return {
            "item_id": item_id,
            "updated": True,
            "answer_status": changes["answer_status"],
            "audio_ready": item["section"] == "listening" and audio_issue is None,
            "audio_error": audio_issue,
        }
    finally:
        conn.close()


def list_items(workspace: Path, section: str | None = None) -> dict[str, Any]:
    conn = connect(workspace)
    try:
        profile = require_profile(conn)
        if section:
            check_section(profile["level"], section)
        sql = "SELECT id,material_id,level,section,subtype,answer_status,locator,audio_path,source_type,active,created_at FROM items"
        params: tuple[Any, ...] = ()
        if section:
            sql += " WHERE section=?"
            params = (section,)
        sql += " ORDER BY created_at DESC, id"
        return {"items": [dict(row) for row in conn.execute(sql, params)]}
    finally:
        conn.close()


def synthesize_item(workspace: Path, item_id: str) -> dict[str, Any]:
    conn = connect(workspace)
    try:
        item = row_dict(
            conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        )
        if item is None:
            raise CoachError(
                "item_not_found", "找不到题目。", "用 material items 查看 ID。"
            )
        if item["section"] != "listening" or item["source_type"] != "original":
            raise CoachError(
                "tts_original_only",
                "只为原创听力题合成辅助音频。",
                "用户材料请引用原音频。",
            )
        if item["audio_path"] and Path(item["audio_path"]).is_file():
            _check_bound_audio(conn, item)
            return {
                "item_id": item_id,
                "audio_path": item["audio_path"],
                "created": False,
                "label": "合成音频，非官方考试材料",
            }
        path = state_root(workspace) / "audio" / f"{item_id}.aiff"
        synthesize_mac_audio(item["transcript"] or "", path)
        with conn:
            conn.execute(
                "UPDATE items SET audio_path=?,audio_sha256=? WHERE id=?",
                (str(path), _audio_fingerprint(path), item_id),
            )
        return {
            "item_id": item_id,
            "audio_path": str(path),
            "created": True,
            "label": "合成音频，非官方考试材料",
        }
    finally:
        conn.close()


def drill(
    workspace: Path,
    *,
    section: str | None = None,
    subtype: str | None = None,
    item_id: str | None = None,
    fresh: bool = False,
    use_focus: bool = False,
) -> dict[str, Any]:
    if use_focus:
        if any((section, subtype, item_id)):
            raise CoachError(
                "focus_filter_conflict",
                "主攻抽题不可与手动筛选同时使用。",
                "使用 --focus，或单独提供 --section/--subtype/--item-id。",
            )
        current_plan = plan(workspace)
        if not current_plan["focus"]:
            raise CoachError(
                "no_planned_focus",
                "目前没有可沿用的主攻范围。",
                "先根据 plan 的诊断或考试阶段说明决定下一步。",
            )
        section = current_plan["focus"]
        subtype = (current_plan["focus_detail"] or {}).get("subtype")
    conn = connect(workspace)
    try:
        # Rank candidates and read the chosen item from one database snapshot.
        conn.execute("BEGIN")
        profile = require_profile(conn)
        if item_id:
            selected = row_dict(
                conn.execute(
                    "SELECT level,section,subtype,active FROM items WHERE id=?",
                    (item_id,),
                ).fetchone()
            )
            if selected is None or not selected["active"]:
                raise CoachError(
                    "item_not_found",
                    "指定题目不存在或已停用。",
                    "运行 material items 核对 ID。",
                )
            if selected["level"] != profile["level"]:
                raise CoachError(
                    "level_conflict",
                    "指定题目级别与当前档案不符。",
                    "切换到相应等级的学习目录。",
                )
            if (section and section != selected["section"]) or (
                subtype and subtype != selected["subtype"]
            ):
                raise CoachError(
                    "item_filter_conflict",
                    "题目 ID 与模块或题型筛选不一致。",
                    "只保留 --item-id，或核对筛选条件。",
                )
            section = selected["section"]
        if section is None:
            raise CoachError(
                "missing_section",
                "需要指定模块或题目 ID。",
                "提供 --section 或 --item-id。",
            )
        check_section(profile["level"], section, subtype)
        clauses = ["i.level=?", "i.section=?", "i.active=1"]
        params: list[Any] = [profile["level"], section]
        if item_id:
            clauses.append("i.id=?")
            params.append(item_id)
        if subtype:
            clauses.append("i.subtype=?")
            params.append(subtype)
        scope = " AND ".join(clauses)
        candidate_filter = scope
        history_columns = """
            (SELECT COUNT(*) FROM attempts a WHERE a.item_id=i.id) AS attempt_count,
            (SELECT a.result FROM attempts a WHERE a.item_id=i.id
                ORDER BY a.made_at DESC,a.rowid DESC LIMIT 1) AS last_result
        """
        if fresh:
            candidate_filter += (
                " AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.item_id=i.id)"
            )
            history_columns = "0 AS attempt_count, NULL AS last_result"
        limit = "" if section == "listening" else "LIMIT 1"
        candidates = conn.execute(
            f"""WITH candidates AS (
                SELECT i.id,i.created_at,r.due_at,{history_columns}
                FROM items i LEFT JOIN review_cards r ON r.item_id=i.id
                WHERE {candidate_filter}
            )
            SELECT id FROM candidates
            ORDER BY CASE
                WHEN due_at<>'' AND due_at<=? THEN 0
                WHEN last_result IN ('wrong','partial') THEN 1
                WHEN attempt_count=0 THEN 2
                ELSE 3
            END, COALESCE(NULLIF(due_at,''),'9999-12-31'),attempt_count,created_at,id
            {limit}""",
            [*params, today_iso()],
        )
        first = candidates.fetchone()
        if first is None:
            if (
                fresh
                and conn.execute(
                    f"SELECT 1 FROM items i WHERE {scope} LIMIT 1", params
                ).fetchone()
            ):
                raise CoachError(
                    "no_fresh_item",
                    "该范围内没有尚未作答的可训练题。",
                    "导入同题型新题，或经学生同意重做旧题。",
                )
            recovery = (
                "先用 material item-add 建一题。"
                if section != "listening"
                else "先导入或合成可播放音频并绑定听力题。"
            )
            raise CoachError("no_usable_item", f"{section} 暂无可训练题。", recovery)

        fingerprints: dict[str, str] = {}
        first_failure = None
        for candidate in chain((first,), candidates):
            item = dict(
                conn.execute(
                    """SELECT i.id,i.level,i.section,i.subtype,i.prompt,i.answer_status,i.locator,
                          i.source_type,i.audio_path,i.audio_sha256,i.material_id,
                          m.path AS material_path,m.year AS material_year,
                          m.source_url AS material_url,m.confidence AS material_confidence
                   FROM items i LEFT JOIN materials m ON m.id=i.material_id WHERE i.id=?""",
                    (candidate["id"],),
                ).fetchone()
            )
            if section == "listening":
                try:
                    _check_bound_audio(conn, item, fingerprints)
                except CoachError as exc:
                    if first_failure is None:
                        first_failure = exc
                    continue
            break
        else:
            assert first_failure is not None
            raise first_failure
        source = {
            "type": item["source_type"],
            "material_path": item["material_path"],
            "year": item["material_year"],
            "url": item["material_url"],
            "locator": item["locator"],
            "confidence": item["material_confidence"],
        }
        return {
            "item_id": item["id"],
            "level": item["level"],
            "section": item["section"],
            "subtype": item["subtype"],
            "question": item["prompt"],
            "audio_path": item["audio_path"],
            "audio_label": "原创辅助音频，非官方考试材料"
            if item["source_type"] == "original" and item["audio_path"]
            else None,
            "source": source,
            "answer_status": item["answer_status"],
            "instruction": "先独立作答；提交后才核对答案与依据。",
        }
    finally:
        conn.close()


# Filter the reporting window first, then use the item/date index to find the
# first attempt across all history. Rowid breaks ties in insertion order.
FIRST_ATTEMPT_SQL = """a.rowid = (
    SELECT first.rowid FROM attempts first WHERE first.item_id=a.item_id
    ORDER BY first.made_at,first.rowid LIMIT 1
)"""


def _same_error_streak(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    attempt_id: str,
    basis: str,
    error_tag: str,
) -> int:
    since = (local_date() - timedelta(days=13)).isoformat()
    recent = conn.execute(
        f"""SELECT a.id,a.result,a.error_tag
        FROM attempts a JOIN items i ON i.id=a.item_id
        WHERE i.level=? AND i.section=? AND i.subtype=?
          AND a.practice_mode='independent' AND a.judgment_basis=? AND a.made_at>=?
          AND {FIRST_ATTEMPT_SQL}
        ORDER BY a.made_at DESC,a.rowid DESC LIMIT 3""",
        (item["level"], item["section"], item["subtype"], basis, since),
    ).fetchall()
    if not recent or recent[0]["id"] != attempt_id:
        return 0
    streak = 0
    for row in recent:
        if row["result"] not in {"wrong", "partial"} or row["error_tag"] != error_tag:
            break
        streak += 1
    return streak


def _resolve_revision_criterion(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    criterion: str | None,
    revises: str | None,
) -> str | None:
    if criterion is None and revises is None:
        return None
    if item["section"] not in {"writing", "translation"}:
        raise CoachError(
            "revision_section",
            "稿件判据和重写关联用于写作或翻译。",
            "核对题型与记录 ID。",
        )
    if criterion is not None:
        criterion = criterion.strip()
        if not 0 < len(criterion) <= 240:
            raise CoachError(
                "invalid_revision_criterion",
                "稿件判据须为 1–240 个字符。",
                "保留一条可检验判据。",
            )
    if revises is None:
        return criterion

    parent = row_dict(
        conn.execute(
            "SELECT item_id,judgment_basis,revision_criterion FROM attempts WHERE id=?",
            (revises,),
        ).fetchone()
    )
    if parent is None or parent["item_id"] != item["id"]:
        raise CoachError(
            "revision_parent_mismatch",
            "找不到同一题下的原稿记录。",
            "用 revisions 核对原稿 ID。",
        )
    if parent["judgment_basis"] != "coach_review":
        raise CoachError(
            "revision_parent_unreviewed",
            "该原稿缺少教练批改。",
            "选择已有批改和证据的稿件。",
        )
    original = parent["revision_criterion"]
    if original and criterion is not None and criterion != original:
        raise CoachError(
            "revision_criterion_mismatch",
            "重写判据与原稿登记的判据不同。",
            "省略 --criterion 沿用原稿判据，或保存为单独的练习。",
        )
    if not (original or criterion):
        raise CoachError(
            "revision_criterion_missing",
            "旧稿没有结构化判据。",
            "核对旧稿反馈后用 --criterion 提供本轮判据。",
        )
    return original or criterion


def _translation_alignment(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    response: str,
    alignment: Any,
    revises: str | None,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    parent = None
    if item["section"] == "translation" and revises:
        row = conn.execute(
            "SELECT translation_alignment_json FROM attempts WHERE id=?", (revises,)
        ).fetchone()
        if row and row[0]:
            parent = json.loads(row[0])
    if alignment is None:
        if parent:
            raise CoachError(
                "alignment_required",
                "该重译需要沿用原有的逐点证据。",
                "通过 --alignment-file 提交同一组信息点。",
            )
        return None, None
    if item["section"] != "translation":
        raise CoachError(
            "alignment_section", "逐点原译对照用于翻译。", "核对题目模块。"
        )
    points = validate_alignment(
        alignment, source=item["prompt"], draft=response, parent_points=parent
    )
    return points, alignment_delta(parent, points)


def attempt(
    workspace: Path,
    *,
    item_id: str,
    response: str,
    result: str | None = None,
    minutes: float = 0,
    error_tag: str | None = None,
    feedback: str | None = None,
    evidence: str | None = None,
    save_response: bool = False,
    rating: str | None = None,
    event_id: str | None = None,
    practice_mode: str = "independent",
    criterion: str | None = None,
    revises: str | None = None,
    alignment: Any = None,
) -> dict[str, Any]:
    if practice_mode not in {"independent", "assisted"}:
        raise CoachError(
            "invalid_practice_mode", "训练模式无效。", "使用 independent 或 assisted。"
        )
    if revises is not None:
        practice_mode = "assisted"
    if not response.strip():
        raise CoachError(
            "empty_response", "必须先有学生实际作答才能提交尝试。", "让学生作答后重试。"
        )
    if not 0 <= minutes <= 720:
        raise CoachError(
            "invalid_minutes", "作答分钟数需在 0–720 之间。", "核对 --minutes。"
        )
    if result is not None and result not in {
        "correct",
        "partial",
        "wrong",
        "unverified",
    }:
        raise CoachError(
            "invalid_result",
            "作答结果无效。",
            "使用 correct、partial、wrong 或 unverified。",
        )
    if rating not in (None, "easy"):
        raise CoachError(
            "invalid_rating", "只支持显式 easy 评级。", "省略 --rating，或使用 easy。"
        )
    conn = connect(workspace)
    try:
        conn.execute("BEGIN IMMEDIATE")
        profile = require_profile(conn)
        item = row_dict(
            conn.execute(
                "SELECT * FROM items WHERE id=? AND active=1", (item_id,)
            ).fetchone()
        )
        if item is None:
            raise CoachError(
                "item_not_found", "题目不存在或已停用。", "用 material items 核对 ID。"
            )
        criterion = _resolve_revision_criterion(conn, item, criterion, revises)
        points, differences = _translation_alignment(
            conn, item, response, alignment, revises
        )
        alignment_json = json_text(points) if points is not None else None
        made_at = now_iso()
        attempt_id = event_id or stable_id("attempt", item_id, made_at, response)
        existing = row_dict(
            conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        )
        if existing:
            if existing["item_id"] != item_id:
                raise CoachError(
                    "idempotency_conflict",
                    "event ID 已用于另一道题。",
                    "为新的作答使用新的 --event-id。",
                )
            if (
                existing["practice_mode"] != practice_mode
                or existing["revises_attempt_id"] != revises
                or existing["revision_criterion"] != criterion
                or existing["translation_alignment_json"] != alignment_json
            ):
                raise CoachError(
                    "idempotency_conflict",
                    "event ID 已用于另一种作答模式、稿件关联或判据。",
                    "核对原记录；新作答使用新的 --event-id。",
                )
            return {"saved": False, "duplicate": True, "attempt": existing}
        if item["section"] == "listening":
            _check_bound_audio(conn, item)
        answer = (item["answer"] or "").strip()
        normalized_answer = answer.upper()
        choice_match = re.fullmatch(r"\s*([A-Da-d])\s*[.)]?\s*", response)
        normalized_response = (
            choice_match.group(1).upper() if choice_match else response.strip().upper()
        )
        deterministic = bool(
            item["answer_status"] in {"verified", "generated"}
            and len(normalized_answer) == 1
            and normalized_answer in "ABCD"
            and len(normalized_response) == 1
            and normalized_response in "ABCD"
        )
        if deterministic:
            computed = (
                "correct" if normalized_answer == normalized_response else "wrong"
            )
            if result not in (None, computed):
                raise CoachError(
                    "result_mismatch",
                    "所给结果与已记录的单选答案不一致。",
                    "核对学生选项和答案依据。",
                )
            result = computed
            judgment_basis = (
                "verified_key"
                if item["answer_status"] == "verified"
                else "generated_key"
            )
        else:
            result = result or "unverified"
            judgment_basis = "coach_review" if result != "unverified" else "unverified"
        if item["answer_status"] == "unverified" and judgment_basis == "verified_key":
            raise CoachError(
                "unverified_answer",
                "这道题没有核实的答案。",
                "保留教练判断或先补核验依据。",
            )
        if (
            item["section"] in {"writing", "translation"}
            and result != "unverified"
            and not (feedback and evidence)
        ):
            raise CoachError(
                "missing_feedback_evidence",
                "写作和翻译批改需有具体反馈和作品证据。",
                "补充 --feedback 与 --evidence（学生原句或位置）。",
            )
        if (
            points is not None
            and result == "correct"
            and any(point["status"] != "preserved" for point in points)
        ):
            raise CoachError(
                "alignment_result_mismatch",
                "逐点核对仍有失真、遗漏或待核信息。",
                "按当前稿件的证据调整结果。",
            )
        if criterion is not None and judgment_basis != "coach_review":
            raise CoachError(
                "revision_feedback_missing",
                "稿件判据需配合实际批改结果与证据。",
                "补充 --result、--feedback 和 --evidence。",
            )
        previous = row_dict(
            conn.execute(
                "SELECT * FROM review_cards WHERE item_id=?", (item_id,)
            ).fetchone()
        )
        scheduled = next_review(
            section=item["section"],
            result=result if practice_mode == "independent" else "unverified",
            previous=previous,
            exam_date=profile["exam_date"],
            rating=rating,
        )
        with conn:
            conn.execute(
                """INSERT INTO attempts(
                       id,item_id,made_at,response,result,minutes,error_tag,judgment_basis,feedback,evidence,practice_mode,
                       revision_criterion,revises_attempt_id,translation_alignment_json
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    item_id,
                    made_at,
                    response if save_response else None,
                    result,
                    minutes,
                    error_tag,
                    judgment_basis,
                    feedback,
                    evidence,
                    practice_mode,
                    criterion,
                    revises,
                    alignment_json,
                ),
            )
            if practice_mode == "independent" and judgment_basis in {
                "verified_key",
                "coach_review",
            }:
                conn.execute(
                    """DELETE FROM plans WHERE week_start=? AND (
                        focus IS NULL OR (focus=? AND json_extract(payload_json, '$.focus_detail.subtype') IS NULL)
                    )""",
                    (_week_start(date.fromisoformat(made_at[:10])), item["section"]),
                )
            if scheduled:
                conn.execute(
                    """INSERT INTO review_cards(item_id,due_at,stage,scheduler,card_json,updated_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(item_id) DO UPDATE SET due_at=excluded.due_at,stage=excluded.stage,
                       scheduler=excluded.scheduler,card_json=excluded.card_json,updated_at=excluded.updated_at""",
                    (
                        item_id,
                        scheduled["due_at"],
                        scheduled["stage"],
                        scheduled["scheduler"],
                        scheduled["card_json"],
                        scheduled["updated_at"],
                    ),
                )
        recent = [
            row[0]
            for row in conn.execute(
                """SELECT result FROM attempts WHERE item_id=? AND practice_mode='independent'
                   ORDER BY made_at DESC,rowid DESC LIMIT 3""",
                (item_id,),
            )
        ]
        failures = 0
        for past_result in recent:
            if past_result in {"wrong", "partial"}:
                failures += 1
            else:
                break
        same_error_streak = 0
        if (
            practice_mode == "independent"
            and result in {"wrong", "partial"}
            and error_tag
        ):
            same_error_streak = _same_error_streak(
                conn, item, attempt_id, judgment_basis, error_tag
            )
        highest_streak = max(failures, same_error_streak)
        next_action = (
            "暂停加题；看一个反例，学生能复述判据后再换新题。"
            if highest_streak >= 3
            else "缩小训练步长，指出第一个卡点后复测。"
            if highest_streak >= 2
            else "按错因订正或继续下一题。"
        )
        if differences and differences["regressed"]:
            next_action = "先核对新增失真或遗漏的信息点：" + "、".join(
                differences["regressed"]
            )
        return {
            "saved": True,
            "attempt_id": attempt_id,
            "item_id": item_id,
            "result": result,
            "judgment_basis": judgment_basis,
            "practice_mode": practice_mode,
            "revision_criterion": criterion,
            "revises_attempt_id": revises,
            "translation_alignment": points,
            "alignment_delta": differences,
            "transcript_reveal": item["transcript"]
            if item["section"] == "listening"
            else None,
            "answer_reveal": answer
            if item["answer_status"] in {"verified", "generated"}
            else None,
            "answer_label": "来源答案"
            if item["answer_status"] == "verified"
            else "原创题参考答案"
            if item["answer_status"] == "generated"
            else "答案未核验",
            "source_evidence": item["evidence"]
            if item["answer_status"] == "verified"
            else None,
            "review": scheduled,
            "consecutive_failures": failures,
            "same_error_streak_distinct_items": same_error_streak,
            "next_action": next_action,
            "response_saved": save_response,
        }
    finally:
        conn.close()


def revision_history(workspace: Path, *, item_id: str) -> dict[str, Any]:
    """Read scoped writing feedback and explicit revision links without draft text."""
    conn = connect(workspace)
    try:
        profile = require_profile(conn)
        item = conn.execute(
            "SELECT section,subtype FROM items WHERE id=? AND level=?",
            (item_id, profile["level"]),
        ).fetchone()
        if not item or item["section"] not in {"writing", "translation"}:
            raise CoachError(
                "revision_item_missing", "找不到当前级别的写译题。", "核对题目 ID。"
            )
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT id,made_at,result,minutes,judgment_basis,practice_mode,
                          revision_criterion,revises_attempt_id,feedback,evidence,translation_alignment_json,
                          response IS NOT NULL AS response_saved
                     FROM attempts WHERE item_id=? ORDER BY made_at,rowid""",
                (item_id,),
            )
        ]
        for row in rows:
            stored = row.pop("translation_alignment_json")
            row["translation_alignment"] = json.loads(stored) if stored else None
        by_id = {row["id"]: row for row in rows}
        for row in rows:
            parent = by_id.get(row["revises_attempt_id"])
            parent_criterion = parent["revision_criterion"] if parent else None
            row["matches_parent_criterion"] = (
                row["revision_criterion"] == parent_criterion
                if parent_criterion
                else None
            )
            row["alignment_delta"] = alignment_delta(
                parent["translation_alignment"] if parent else None,
                row["translation_alignment"],
            )
            row["response_saved"] = bool(row["response_saved"])
        return {
            "item_id": item_id,
            "section": item["section"],
            "subtype": item["subtype"],
            "independent_drafts": sum(
                row["practice_mode"] == "independent" for row in rows
            ),
            "guided_rewrites": sum(
                row["revises_attempt_id"] is not None for row in rows
            ),
            "attempts": rows,
            "basis": "同题重写用于批改回访；独立迁移需另做新题。",
        }
    finally:
        conn.close()


def add_checkpoint(
    workspace: Path,
    *,
    kind: str,
    taken_on: str,
    source: str,
    section: str | None = None,
    correct: int | None = None,
    question_total: int | None = None,
    minutes: float | None = None,
    score_total: int | None = None,
    score_listening: int | None = None,
    score_reading: int | None = None,
    score_writing_translation: int | None = None,
    event_id: str | None = None,
    comparison_key: str | None = None,
) -> dict[str, Any]:
    if comparison_key is not None:
        comparison_key = comparison_key.strip()
        if not 0 < len(comparison_key) <= 120:
            raise CoachError(
                "invalid_comparison_key",
                "模考口径名需包含 1–120 个字符。",
                "核对 --comparison-key。",
            )
    taken_on = parse_date(taken_on, "taken_on") or today_iso()
    if date.fromisoformat(taken_on) > local_date():
        raise CoachError(
            "future_checkpoint", "检查点不能晚于今天。", "核对考试或模考日期。"
        )
    if not source.strip():
        raise CoachError(
            "missing_source", "检查点必须标明来源。", "提供试卷、学校或成绩单来源。"
        )
    conn = connect(workspace)
    try:
        profile = require_profile(conn)
        if kind == "official":
            if score_total is None or not 0 <= score_total <= 710:
                raise CoachError(
                    "invalid_official_score",
                    "官方报道总分需在 0–710 之间。",
                    "录入成绩单总分。",
                )
            maxima = {
                "score_listening": 249,
                "score_reading": 249,
                "score_writing_translation": 212,
            }
            values = {
                "score_listening": score_listening,
                "score_reading": score_reading,
                "score_writing_translation": score_writing_translation,
            }
            for name, value in values.items():
                if value is not None and not 0 <= value <= maxima[name]:
                    raise CoachError(
                        "invalid_section_score",
                        f"{name} 超出官方单项范围。",
                        "核对成绩报告单。",
                    )
            known = [value for value in values.values() if value is not None]
            if sum(known) > score_total or (
                len(known) == 3 and sum(known) != score_total
            ):
                raise CoachError(
                    "score_sum_mismatch",
                    "单项报道分与总分不一致。",
                    "核对成绩单；写作和翻译合并录入。",
                )
            if any(
                value is not None
                for value in (section, correct, question_total, minutes, comparison_key)
            ):
                raise CoachError(
                    "official_raw_mix",
                    "官方报道分不能和模考原始正确数混记。",
                    "分开创建 official 与 mock 检查点。",
                )
        elif kind == "mock":
            if section not in {"listening", "reading"}:
                raise CoachError(
                    "invalid_mock_section",
                    "客观题模考只记录听力或阅读原始正确率。",
                    "写作与翻译用 attempt 保存有证据的反馈。",
                )
            if (
                correct is None
                or question_total is None
                or not 1 <= question_total <= 200
                or not 0 <= correct <= question_total
            ):
                raise CoachError(
                    "invalid_mock_result",
                    "模考需要正确数和题数，且正确数不得超过题数。",
                    "提供 --correct 与 --question-total。",
                )
            if minutes is None or not 0 < minutes <= 720:
                raise CoachError(
                    "invalid_minutes", "模考需要合理的限时分钟数。", "提供 --minutes。"
                )
            if any(
                value is not None
                for value in (
                    score_total,
                    score_listening,
                    score_reading,
                    score_writing_translation,
                )
            ):
                raise CoachError(
                    "mock_score_mix",
                    "模考原始正确率不能写成官方报道分。",
                    "去掉 --score-* 参数。",
                )
        else:
            raise CoachError(
                "invalid_checkpoint_kind", "检查点类型无效。", "使用 official 或 mock。"
            )
        checkpoint_id = event_id or stable_id(
            "checkpoint",
            kind,
            taken_on,
            source,
            section or "",
            str(score_total),
            str(correct),
            str(question_total),
        )
        conn.execute("BEGIN IMMEDIATE")
        existing = row_dict(
            conn.execute(
                "SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)
            ).fetchone()
        )
        if existing:
            expected = {
                "kind": kind,
                "taken_on": taken_on,
                "source": source.strip(),
                "level": profile["level"],
                "section": section,
                "correct": correct,
                "question_total": question_total,
                "minutes": minutes,
                "score_total": score_total,
                "score_listening": score_listening,
                "score_reading": score_reading,
                "score_writing_translation": score_writing_translation,
                "comparison_key": comparison_key,
            }
            if any(existing[key] != value for key, value in expected.items()):
                raise CoachError(
                    "idempotency_conflict",
                    "event ID 已用于另一条检查点。",
                    "为新的检查点使用新的 --event-id。",
                )
            return {"saved": False, "duplicate": True, "checkpoint": existing}
        with conn:
            conn.execute(
                """INSERT INTO checkpoints(
                       id,kind,level,taken_on,section,correct,question_total,minutes,score_total,
                       score_listening,score_reading,score_writing_translation,source,created_at,comparison_key
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    checkpoint_id,
                    kind,
                    profile["level"],
                    taken_on,
                    section,
                    correct,
                    question_total,
                    minutes,
                    score_total,
                    score_listening,
                    score_reading,
                    score_writing_translation,
                    source.strip(),
                    now_iso(),
                    comparison_key,
                ),
            )
            conn.execute("DELETE FROM plans")
        return {
            "saved": True,
            "checkpoint_id": checkpoint_id,
            "kind": kind,
            "replan_required": True,
        }
    finally:
        conn.close()


def _week_start(day: date) -> str:
    return (day - timedelta(days=day.weekday())).isoformat()


def _latest_checkpoint_marker(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT id FROM checkpoints ORDER BY created_at DESC,id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


SUBTYPE_CHECKS = {
    "short_news": "写出报道的核心事件，并指出支持判断的听到的信息。",
    "long_conversation": "写出说话者的意图或关键变化，并指出一处音频依据。",
    "listening_passage": "先抓关键信息，再用一处音频依据排除干扰项。",
    "lecture": "写出观点或转折后的结论，并指出一句音频依据。",
    "banked_cloze": "标出词性、搭配或上下文线索，再选择对应词。",
    "matching": "圈出段落中的同义定位词，并说明匹配原因。",
    "careful_reading": "先说题干问什么，指向支撑答案的原文，并说出最像的干扰项为何错。",
    "essay": "围绕同一条判据二改，标出原句和修改后消除了什么问题。",
    "paragraph": "对照原文信息点核对二稿，指出补回或修正的一处关系。",
}


def _subtype_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["section"], row["subtype"])].append(row)
    result = []
    for (section, subtype), events in sorted(groups.items()):
        outcomes = Counter(event["result"] for event in events)
        tags = Counter(
            event["error_tag"]
            for event in events
            if event["result"] in {"wrong", "partial"} and event["error_tag"]
        )
        repeats = {tag: count for tag, count in tags.items() if count >= 2}
        top_tag = min(repeats, key=lambda tag: (-repeats[tag], tag), default=None)
        result.append(
            {
                "section": section,
                "subtype": subtype,
                "sample_count": len(events),
                "correct": outcomes["correct"],
                "wrong": outcomes["wrong"],
                "partial": outcomes["partial"],
                "error_rate": round(
                    (outcomes["wrong"] + outcomes["partial"] * 0.5) / len(events), 4
                ),
                "repeated_errors": repeats,
                "top_error_tag": top_tag,
                "verification": SUBTYPE_CHECKS[subtype],
            }
        )
    return result


def _select_focus_detail(
    section: str | None, evidence: list[dict[str, Any]]
) -> dict[str, Any] | None:
    candidates = [
        item
        for item in evidence
        if item["section"] == section and item["error_rate"] > 0
    ]
    minimum = 3 if section in {"reading", "listening"} else 1
    supported = [item for item in candidates if item["sample_count"] >= minimum]
    provisional = [item for item in candidates if item["top_error_tag"]]
    selected = min(
        supported or provisional,
        key=lambda item: (
            -item["error_rate"],
            -max(item["repeated_errors"].values(), default=0),
            -item["sample_count"],
            item["subtype"],
        ),
        default=None,
    )
    if selected is None:
        return None
    return {
        **selected,
        "basis": "independent-first-attempts"
        if supported
        else "provisional-repeated-error",
    }


def _focus_from_evidence(
    conn: sqlite3.Connection, profile: dict[str, Any], current: date
) -> tuple[str | None, str, dict[str, Any], dict[str, Any] | None]:
    since = (current - timedelta(days=13)).isoformat()
    next_day = (current + timedelta(days=1)).isoformat()
    rows = [
        dict(row)
        for row in conn.execute(
            f"""SELECT i.section,i.subtype,a.result,a.error_tag,a.practice_mode,a.judgment_basis,
                       {FIRST_ATTEMPT_SQL} AS first_attempt
                FROM attempts a JOIN items i ON i.id=a.item_id
                WHERE i.level=? AND a.made_at>=? AND a.made_at<?""",
            (profile["level"], since, next_day),
        )
    ]
    by_section: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if (
            row["section"] in {"listening", "reading", "writing", "translation"}
            and row["practice_mode"] == "independent"
            and row["judgment_basis"]
            in (
                {"verified_key"}
                if row["section"] in {"listening", "reading"}
                else {"coach_review"}
            )
            and row["first_attempt"]
        ):
            by_section[row["section"]].append(row)
    subtype_evidence = _subtype_evidence(
        [event for events in by_section.values() for event in events]
    )
    candidates = []
    weights = {"listening": 0.35, "reading": 0.35, "writing": 0.15, "translation": 0.15}
    samples: dict[str, Any] = {}
    for section, events in by_section.items():
        all_section_rows = [
            row
            for row in rows
            if row["section"] == section and row["practice_mode"] == "independent"
        ]
        minimum = 3 if section in {"listening", "reading"} else 1
        wrong = sum(event["result"] == "wrong" for event in events)
        partial = sum(event["result"] == "partial" for event in events)
        tags = Counter(
            row["error_tag"]
            for row in events
            if row["error_tag"] and row["result"] in {"wrong", "partial"}
        )
        samples[section] = {
            "attempts": len(all_section_rows),
            "new_items": len(events),
            "wrong": wrong,
            "partial": partial,
            "repeated_errors": {k: v for k, v in tags.items() if v >= 2},
        }
        if len(events) >= minimum:
            error_rate = (wrong + partial * 0.5) / len(events)
            repeated = sum(value - 1 for value in tags.values() if value >= 2)
            candidates.append((weights[section] * error_rate, repeated, section))
    mock_rows = conn.execute(
        """SELECT section,correct,question_total,source FROM checkpoints
           WHERE kind='mock' AND level=? AND taken_on BETWEEN ? AND ?
                 AND comparison_key IS NOT NULL AND question_total>=5
           ORDER BY taken_on DESC, created_at DESC, id DESC""",
        (profile["level"], since, current.isoformat()),
    )
    observed_sections = {section for _, _, section in candidates}
    for row in mock_rows:
        section = row["section"]
        if section not in weights or section in observed_sections:
            continue
        observed_sections.add(section)
        error_rate = 1 - row["correct"] / row["question_total"]
        samples.setdefault(section, {})["timed_mock"] = dict(row)
        candidates.append((weights[section] * error_rate, 0, section))
    if candidates:
        candidates.sort(reverse=True)
        chosen = candidates[0][2]
        return (
            chosen,
            f"近 14 天 {chosen} 的独立作答或限时模考证据，暂定为训练主攻。",
            samples,
            _select_focus_detail(chosen, subtype_evidence),
        )
    if profile["weak_section"] in {"listening", "reading", "writing", "translation"}:
        section = profile["weak_section"]
        return (
            section,
            "用户自述弱项，尚需限时检查点校准。",
            samples,
            _select_focus_detail(section, subtype_evidence),
        )
    return None, "缺少足够的同型独立作答证据，先做短时诊断。", samples, None


def plan(
    workspace: Path, *, force: bool = False, current: date | None = None
) -> dict[str, Any]:
    day = current or local_date()
    conn = connect(workspace)
    try:
        return _plan_for_day(conn, require_profile(conn), day, force=force)
    finally:
        conn.close()


def _plan_for_day(
    conn: sqlite3.Connection, profile: dict[str, Any], day: date, *, force: bool = False
) -> dict[str, Any]:
    week = _week_start(day)
    marker = _latest_checkpoint_marker(conn)
    exam_date = (
        date.fromisoformat(profile["exam_date"]) if profile["exam_date"] else None
    )
    days_remaining = (exam_date - day).days if exam_date else None
    if days_remaining is None or days_remaining > 60:
        phase = "build_or_unknown"
    elif days_remaining < 0:
        phase = "past_exam"
    elif days_remaining == 0:
        phase = "exam_day"
    elif days_remaining <= 14:
        phase = "final_14_days"
    else:
        phase = "sprint"
    old = row_dict(
        conn.execute("SELECT * FROM plans WHERE week_start=?", (week,)).fetchone()
    )
    if (
        old
        and not force
        and old["profile_updated_at"] == profile["updated_at"]
        and old["latest_checkpoint"] == marker
    ):
        cached = json.loads(old["payload_json"])
        if cached["phase"] == phase and "focus_detail" in cached:
            return {**cached, "days_remaining": days_remaining, "reused": True}
    focus_detail = None
    if phase == "past_exam":
        focus, basis, samples = (
            None,
            "已过档案中的考试日期；请设置新考试日期后重排。",
            {},
        )
    elif phase == "exam_day":
        focus, basis, samples = (
            None,
            "今天是档案中的考试日；以准考证与考点安排为准，停止常规加题。",
            {},
        )
    else:
        focus, basis, samples, focus_detail = _focus_from_evidence(conn, profile, day)
    payload = {
        "week_start": week,
        "level": profile["level"],
        "exam_date": profile["exam_date"],
        "days_remaining": days_remaining,
        "phase": phase,
        "focus": focus,
        "focus_label": focus or "短时诊断",
        "focus_detail": focus_detail,
        "basis": basis,
        "sample_evidence": samples,
        "buffer_ratio": 0.15,
        "max_daily_tasks": 3,
        "checkpoint_trigger": "新的同型限时模考、官方成绩、考试日期或现实容量变化后重排。",
    }
    with conn:
        conn.execute(
            """INSERT INTO plans VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(week_start) DO UPDATE SET generated_at=excluded.generated_at,
               focus=excluded.focus,basis=excluded.basis,profile_updated_at=excluded.profile_updated_at,
               latest_checkpoint=excluded.latest_checkpoint,payload_json=excluded.payload_json""",
            (
                week,
                now_iso(),
                focus,
                basis,
                profile["updated_at"],
                marker,
                json_text(payload),
            ),
        )
    return {**payload, "reused": False}


def today(workspace: Path, *, current: date | None = None) -> dict[str, Any]:
    day = current or local_date()
    conn = connect(workspace)
    try:
        profile = require_profile(conn)
        week_plan = _plan_for_day(conn, profile, day)
        focus_detail = week_plan["focus_detail"]
        available = (
            profile["weekend_minutes"]
            if day.weekday() >= 5
            else profile["weekday_minutes"]
        )
        budget = int(available * 0.85)
        if week_plan["phase"] in {"past_exam", "exam_day"} or budget == 0:
            with conn:
                conn.execute(
                    "DELETE FROM task_logs WHERE task_date=? AND completed=0",
                    (day.isoformat(),),
                )
            reason = (
                week_plan["basis"]
                if week_plan["phase"] in {"past_exam", "exam_day"}
                else "今日没有可用学习时间。"
            )
            return {
                "date": day.isoformat(),
                "available_minutes": available,
                "training_budget_minutes": budget,
                "buffer_minutes": available - budget,
                "focus": week_plan["focus"],
                "tasks": [],
                "reason": reason,
            }
        due = conn.execute(
            "SELECT COUNT(*) FROM review_cards WHERE due_at<=?", (day.isoformat(),)
        ).fetchone()[0]
        specs: list[tuple[str, str | None, str, int, str]] = []
        if week_plan["focus"] is None:
            count = min(budget, 25)
            specs.append(
                (
                    "diagnostic",
                    None,
                    "做一组来源明确的限时诊断题",
                    count,
                    "记录题型、原始正确数、用时和第一个错因",
                )
            )
        else:
            if due and budget >= 25:
                review_minutes = min(20, max(5, int(budget * 0.20)))
                specs.append(
                    (
                        "review",
                        None,
                        f"无提示提取 {min(due, 6)} 项到期内容",
                        review_minutes,
                        "先回忆再核对，并记录结果",
                    )
                )
            remaining = budget - sum(spec[3] for spec in specs)
            focus = week_plan["focus"]
            maintenance = "reading" if focus == "listening" else "listening"
            maintain_minutes = (
                min(15, max(0, int(remaining * 0.20))) if remaining >= 45 else 0
            )
            main_minutes = remaining - maintain_minutes
            title = f"主攻 {focus}：完成一次无提示或限时训练"
            verification = "独立作答、标来源和用时、写出首个决定性错因"
            if focus_detail:
                title = f"主攻 {focus} / {focus_detail['subtype']}"
                if focus_detail["top_error_tag"]:
                    title += f"：先修「{focus_detail['top_error_tag']}」"
                verification = focus_detail["verification"]
            specs.append(("main", focus, title, main_minutes, verification))
            if maintain_minutes:
                specs.append(
                    (
                        "maintenance",
                        maintenance,
                        f"维持 {maintenance}：完成一组短训练",
                        maintain_minutes,
                        "留下可核验作答，不以看懂代替完成",
                    )
                )

        def daily_task_id(role: str, section: str | None) -> str:
            parts = ("task", day.isoformat(), role, section or "all")
            if role == "main" and focus_detail:
                return stable_id(*parts, focus_detail["subtype"])
            return stable_id(*parts)

        specs = specs[:3]
        task_ids = [daily_task_id(role, section) for role, section, *_ in specs]
        placeholders = ",".join("?" for _ in task_ids)
        stamp = now_iso()
        with conn:
            conn.execute(
                f"""DELETE FROM task_logs
                    WHERE task_date=? AND completed=0 AND id NOT IN ({placeholders})""",
                (day.isoformat(), *task_ids),
            )
            conn.executemany(
                """INSERT OR IGNORE INTO task_logs(id,task_date,role,section,title,planned_minutes,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    (task_id, day.isoformat(), role, section, title, minutes, stamp)
                    for task_id, (role, section, title, minutes, _) in zip(
                        task_ids, specs
                    )
                ),
            )
            progress = {
                row["id"]: row
                for row in conn.execute(
                    f"""SELECT id,completed,actual_minutes,evidence FROM task_logs
                        WHERE id IN ({placeholders})""",
                    task_ids,
                )
            }
        tasks = []
        for order, (
            task_id,
            (role, section, title, minutes, verification),
        ) in enumerate(zip(task_ids, specs), 1):
            row = progress[task_id]
            tasks.append(
                {
                    "id": task_id,
                    "order": order,
                    "role": role,
                    "section": section,
                    "subtype": focus_detail["subtype"]
                    if role == "main" and focus_detail
                    else None,
                    "title": title,
                    "planned_minutes": minutes,
                    "verification": verification,
                    "completed": row["completed"],
                    "actual_minutes": row["actual_minutes"],
                    "evidence": row["evidence"],
                }
            )
        return {
            "date": day.isoformat(),
            "available_minutes": available,
            "training_budget_minutes": budget,
            "buffer_minutes": available - budget,
            "focus": week_plan["focus"],
            "focus_detail": focus_detail,
            "basis": week_plan["basis"],
            "due_review_count": due,
            "tasks": tasks,
        }
    finally:
        conn.close()


def log_task(
    workspace: Path, *, task_id: str, minutes: float, evidence: str
) -> dict[str, Any]:
    if not 0 <= minutes <= 720:
        raise CoachError(
            "invalid_minutes", "实际分钟数需在 0–720 之间。", "核对 --minutes。"
        )
    if not evidence.strip():
        raise CoachError(
            "missing_evidence",
            "完成任务需要可核验的产出。",
            "提供答题、闭卷回忆或限时结果的简述。",
        )
    conn = connect(workspace)
    try:
        require_profile(conn)
        row = row_dict(
            conn.execute("SELECT * FROM task_logs WHERE id=?", (task_id,)).fetchone()
        )
        if row is None:
            raise CoachError(
                "task_not_found", "找不到今日任务。", "先运行 today 获取 task ID。"
            )
        with conn:
            conn.execute(
                "UPDATE task_logs SET completed=1,actual_minutes=?,evidence=?,updated_at=? WHERE id=?",
                (minutes, evidence.strip(), now_iso(), task_id),
            )
        return {
            "saved": True,
            "task_id": task_id,
            "completed": True,
            "actual_minutes": minutes,
            "evidence": evidence.strip(),
        }
    finally:
        conn.close()


def review(
    workspace: Path, *, limit: int = 20, through: str | None = None
) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise CoachError(
            "invalid_limit", "复习列表上限需在 1–100 之间。", "调整 --limit。"
        )
    target_day = parse_date(through, "date") or today_iso()
    conn = connect(workspace)
    try:
        require_profile(conn)
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT r.item_id,r.due_at,r.scheduler,i.section,i.subtype,i.source_type,
                          i.audio_path,i.audio_sha256,i.material_id,i.locator
               FROM review_cards r JOIN items i ON i.id=r.item_id
               WHERE r.due_at<=? AND i.active=1 ORDER BY r.due_at,r.item_id LIMIT ?""",
                (target_day, limit),
            )
        ]
        fingerprints: dict[str, str] = {}
        for row in rows:
            row["audio_ready"] = None
            row["audio_error"] = None
            if row["section"] == "listening":
                try:
                    _check_bound_audio(conn, row, fingerprints)
                except CoachError as exc:
                    row["audio_ready"] = False
                    row["audio_error"] = exc.code
                else:
                    row["audio_ready"] = True
            for column in ("audio_path", "audio_sha256", "material_id"):
                row.pop(column)
        total = conn.execute(
            "SELECT COUNT(*) FROM review_cards r JOIN items i ON i.id=r.item_id WHERE r.due_at<=? AND i.active=1",
            (target_day,),
        ).fetchone()[0]
        return {
            "through": target_day,
            "total_due": total,
            "items": rows,
            "instruction": "用 drill --item-id ID 精确取到期题；先独立回忆，这里不显示答案。",
        }
    finally:
        conn.close()


def report(workspace: Path, *, days: int = 7) -> dict[str, Any]:
    if not 1 <= days <= 90:
        raise CoachError("invalid_days", "复盘窗口需在 1–90 天之间。", "调整 --days。")
    conn = connect(workspace)
    try:
        profile = require_profile(conn)
        since = (local_date() - timedelta(days=days - 1)).isoformat()
        task_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT role,completed,actual_minutes,evidence FROM task_logs WHERE task_date>=?",
                (since,),
            )
        ]
        attempt_rows = [
            dict(row)
            for row in conn.execute(
                """SELECT i.section,i.subtype,a.result,a.error_tag,a.minutes,a.judgment_basis,a.practice_mode,a.revises_attempt_id
               FROM attempts a JOIN items i ON i.id=a.item_id WHERE a.made_at>=? AND i.level=?""",
                (since, profile["level"]),
            )
        ]
        sections: dict[str, dict[str, Any]] = {}
        for section in SECTIONS:
            events = [
                row
                for row in attempt_rows
                if row["section"] == section and row["practice_mode"] == "independent"
            ]
            if events:
                sections[section] = {
                    "attempts": len(events),
                    "correct": sum(row["result"] == "correct" for row in events),
                    "partial": sum(row["result"] == "partial" for row in events),
                    "wrong": sum(row["result"] == "wrong" for row in events),
                    "unverified": sum(row["result"] == "unverified" for row in events),
                    "minutes": round(sum(row["minutes"] for row in events), 1),
                }
        error_counts = Counter(
            row["error_tag"]
            for row in attempt_rows
            if row["error_tag"] and row["practice_mode"] == "independent"
        )
        checkpoints = [
            dict(row)
            for row in conn.execute(
                "SELECT kind,taken_on,section,correct,question_total,minutes,score_total,score_listening,score_reading,score_writing_translation,source,comparison_key FROM checkpoints WHERE taken_on>=? AND level=? ORDER BY taken_on,created_at,id",
                (since, profile["level"]),
            )
        ]
        return {
            "level": profile["level"],
            "window_days": days,
            "since": since,
            "execution": {
                "planned_tasks": len(task_rows),
                "completed_tasks": sum(row["completed"] for row in task_rows),
                "actual_minutes": round(
                    sum(row["actual_minutes"] or 0 for row in task_rows), 1
                ),
            },
            "independent_attempts": sections,
            "assisted_attempts": sum(
                row["practice_mode"] == "assisted" for row in attempt_rows
            ),
            "guided_rewrites": sum(
                row["revises_attempt_id"] is not None for row in attempt_rows
            ),
            "repeated_error_tags": {
                tag: count for tag, count in error_counts.items() if count >= 2
            },
            "checkpoints": checkpoints,
            "due_reviews": conn.execute(
                "SELECT COUNT(*) FROM review_cards r JOIN items i ON i.id=r.item_id WHERE r.due_at<=? AND i.active=1",
                (today_iso(),),
            ).fetchone()[0],
            "interpretation_limit": "只反映已记录行为；模考正确率与官方报道分分列，不能线性换算。",
        }
    finally:
        conn.close()


def efficiency(workspace: Path, *, days: int = 7) -> dict[str, Any]:
    """Interpret separate learning signals without inventing one efficiency score."""
    summary = report(workspace, days=days)
    conn = connect(workspace)
    try:
        profile = require_profile(conn)
        since = summary["since"]
        attempts = [
            dict(row)
            for row in conn.execute(
                f"""SELECT i.section,i.subtype,i.source_type,a.result,a.judgment_basis,
                           a.error_tag,a.feedback,a.evidence,{FIRST_ATTEMPT_SQL} AS first_attempt
                    FROM attempts a JOIN items i ON i.id=a.item_id
                    WHERE i.level=? AND a.made_at>=? AND a.practice_mode='independent'""",
                (profile["level"], since),
            )
        ]
        task_evidence = conn.execute(
            "SELECT COUNT(*) FROM task_logs WHERE task_date>=? AND completed=1 AND evidence IS NOT NULL AND trim(evidence)!=''",
            (since,),
        ).fetchone()[0]
    finally:
        conn.close()

    verified = [
        row
        for row in attempts
        if row["section"] in {"listening", "reading"}
        and row["judgment_basis"] == "verified_key"
    ]
    generated = [
        row
        for row in attempts
        if row["section"] in {"listening", "reading"}
        and row["judgment_basis"] == "generated_key"
    ]
    fresh_verified = [row for row in verified if row["first_attempt"]]
    fresh_generated = [row for row in generated if row["first_attempt"]]
    subjective = [
        row
        for row in attempts
        if row["section"] in {"writing", "translation"}
        and row["judgment_basis"] == "coach_review"
        and row["feedback"]
        and row["evidence"]
    ]
    unverified = [row for row in attempts if row["result"] == "unverified"]
    mocks = [row for row in summary["checkpoints"] if row["kind"] == "mock"]
    official = [row for row in summary["checkpoints"] if row["kind"] == "official"]

    def breakdown(
        rows: list[dict[str, Any]],
    ) -> dict[str, dict[str, int | float | None]]:
        result: dict[str, dict[str, int | float | None]] = {}
        for section in ("listening", "reading"):
            events = [row for row in rows if row["section"] == section]
            if events:
                correct = sum(row["result"] == "correct" for row in events)
                result[section] = {
                    "correct": correct,
                    "total": len(events),
                    "raw_accuracy": round(correct / len(events), 3),
                }
        return result

    verified_quality = breakdown(fresh_verified)
    generated_quality = breakdown(fresh_generated)
    planned = summary["execution"]["planned_tasks"]
    completed = summary["execution"]["completed_tasks"]
    execution_rate = round(completed / planned, 3) if planned else None

    comparable: list[dict[str, Any]] = []
    by_format: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for checkpoint in mocks:
        if (
            checkpoint["question_total"]
            and checkpoint["section"]
            and checkpoint["comparison_key"]
        ):
            by_format[
                (
                    checkpoint["section"],
                    checkpoint["question_total"],
                    checkpoint["comparison_key"],
                )
            ].append(checkpoint)
    for (section, total, key), records in by_format.items():
        newer = records[-1]
        older = next(
            (
                row
                for row in reversed(records[:-1])
                if row["taken_on"] < newer["taken_on"]
                and row["source"].casefold() != newer["source"].casefold()
                and row["minutes"]
                and newer["minutes"]
                and abs(newer["minutes"] - row["minutes"])
                / max(row["minutes"], newer["minutes"])
                <= 0.2
            ),
            None,
        )
        if older is None:
            continue
        comparable.append(
            {
                "section": section,
                "comparison_key": key,
                "question_total": total,
                "earlier_date": older["taken_on"],
                "later_date": newer["taken_on"],
                "earlier_source": older["source"],
                "later_source": newer["source"],
                "earlier_raw_accuracy": round(older["correct"] / total, 3),
                "later_raw_accuracy": round(newer["correct"] / total, 3),
                "limit": "卷组和时长相近只提供原始正确率对照，试卷难度仍可能有差异。",
            }
        )

    low_accuracy = next(
        (
            section
            for section, value in verified_quality.items()
            if value["total"] >= 5 and value["raw_accuracy"] < 0.5
        ),
        None,
    )
    repeated = summary["repeated_error_tags"]
    if (
        planned == 0
        and not official
        and len(fresh_verified) + len(fresh_generated) + len(subjective) + len(mocks)
        < 3
    ):
        diagnosis = (
            "evidence_insufficient",
            "先完成一组来源明确的限时题，并记录用时与错因。",
        )
    elif planned >= 3 and execution_rate is not None and execution_rate < 0.6:
        diagnosis = (
            "capacity_mismatch",
            "下次只保留一个主攻任务，并按真实课表缩小计划。",
        )
    elif completed >= 2 and not (verified or generated or subjective or mocks):
        diagnosis = ("input_heavy", "把下一次学习改成无提示作答，而不是继续只看材料。")
    elif low_accuracy:
        diagnosis = (
            "accuracy_gap",
            f"暂停扩量，先定位 {low_accuracy} 的一个决定性错因并换题复测。",
        )
    elif repeated:
        diagnosis = ("recurring_error", "优先修复一个重复错因，再用新题检验是否复发。")
    elif len(fresh_verified) >= 5 and not mocks:
        diagnosis = (
            "checkpoint_missing",
            "安排同题型、有限时的检查点，检验日常练习是否转化。",
        )
    else:
        diagnosis = ("loop_present", "保持当前主攻，继续积累同口径检查点和错因记录。")

    return {
        "window_days": days,
        "since": since,
        "execution": {**summary["execution"], "completion_rate": execution_rate},
        "effective_evidence": {
            "verified_key_attempts": len(verified),
            "new_verified_questions": len(fresh_verified),
            "original_generated_key_attempts": len(generated),
            "new_original_questions": len(fresh_generated),
            "review_attempts": sum(not row["first_attempt"] for row in attempts),
            "assisted_attempts": summary["assisted_attempts"],
            "guided_writing_translation_rewrites": summary["guided_rewrites"],
            "supported_writing_translation_reviews": len(subjective),
            "timed_mock_checkpoints": len(mocks),
            "official_score_reports": len(official),
            "completed_tasks_with_evidence": task_evidence,
            "unverified_attempts": len(unverified),
        },
        "independent_answer_quality": {
            "verified_source_questions": verified_quality,
            "original_practice_questions": generated_quality,
            "verified_subtype_evidence": _subtype_evidence(fresh_verified),
            "original_subtype_evidence": _subtype_evidence(fresh_generated),
        },
        "repeated_error_tags": repeated,
        "comparable_mock_pairs": comparable,
        "diagnosis": {"code": diagnosis[0], "next_action": diagnosis[1]},
        "limits": [
            "只统计已记录行为；未记录的学习不等于没有发生。",
            "原创题结果与已核验来源题分开，写译教练反馈不换算官方分。",
            "独立作答质量只用每题首次作答；重做原题单列为复习尝试。",
            "没有同口径限时检查点时，不能宣称考试表现提升。",
        ],
    }


def export_state(
    workspace: Path, *, format_name: str, output: str | None = None
) -> dict[str, Any]:
    if format_name not in {"json", "md"}:
        raise CoachError(
            "invalid_export_format", "只支持 json 或 md。", "选择 --format json 或 md。"
        )
    conn = connect(workspace)
    try:
        require_profile(conn)
        tables = (
            "profile",
            "materials",
            "items",
            "attempts",
            "review_cards",
            "checkpoints",
            "plans",
            "task_logs",
        )
        data = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
    finally:
        conn.close()
    destination = (
        Path(output).expanduser().resolve()
        if output
        else state_root(workspace) / f"export.{format_name}"
    )
    if destination.suffix.lower() != f".{format_name}":
        raise CoachError(
            "invalid_export_path",
            "导出后缀与格式不一致。",
            f"使用 .{format_name} 文件名。",
        )
    if format_name == "json":
        content = (
            json.dumps(
                {"schema_version": SCHEMA_VERSION, "exported_at": now_iso(), **data},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    else:
        profile = data["profile"][0]
        lines = [
            f"# CET-{profile['level']} 学习档案",
            "",
            f"- 导出时间：{now_iso()}",
            f"- 考试日期：{profile['exam_date'] or '待核实'}",
            f"- 目标报道分：{profile['target_score'] if profile['target_score'] is not None else '未设置'}",
            "",
            "## 数据概览",
            "",
        ]
        for table in tables[1:]:
            lines.append(f"- {table}: {len(data[table])} 条")
        lines += ["", "## 最近检查点", ""]
        for row in data["checkpoints"][-10:]:
            value = (
                f"官方报道总分 {row['score_total']}"
                if row["kind"] == "official"
                else f"{row['section']} 原始正确 {row['correct']}/{row['question_total']}，{row['minutes']} 分钟"
            )
            lines.append(f"- {row['taken_on']}：{value}（{row['source']}）")
        lines += [
            "",
            "练习原始表现不能换算为官方报道分。完整题目和记录请看 JSON 导出。",
            "",
        ]
        content = "\n".join(lines)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(destination)
    return {
        "format": format_name,
        "path": str(destination),
        "bytes": destination.stat().st_size,
        "includes_private_material": format_name == "json",
    }
