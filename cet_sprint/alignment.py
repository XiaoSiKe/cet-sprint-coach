"""Typed, excerpt-anchored evidence for translation revisions."""

from __future__ import annotations

import unicodedata
from typing import Any

from .storage import CoachError

POINT_FIELDS = {"key", "source_quote", "translation_quote", "status"}
POINT_STATUSES = {"preserved", "distorted", "omitted", "uncertain"}


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _short_text(raw: Any, name: str, limit: int) -> str:
    if not isinstance(raw, str) or not 0 < len(raw.strip()) <= limit:
        raise CoachError(
            "invalid_alignment_point",
            f"{name} 需要 1–{limit} 个字符。",
            "保留可定位的短摘句。",
        )
    return _normalized(raw)


def validate_alignment(
    supplied: Any,
    *,
    source: str,
    draft: str,
    parent_points: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(supplied, list) or not 1 <= len(supplied) <= 24:
        raise CoachError(
            "invalid_alignment",
            "翻译对照需有 1–24 个信息点。",
            "提供有固定 key 的 JSON 数组。",
        )
    source_text, draft_text = _normalized(source), _normalized(draft)
    by_key = {}
    for point in supplied:
        if not isinstance(point, dict) or set(point) - POINT_FIELDS:
            raise CoachError(
                "invalid_alignment_point",
                "逐点对照包含未知信息。",
                "核对 key、source_quote、translation_quote、status。",
            )
        key = _short_text(point.get("key"), "key", 60)
        if key in by_key:
            raise CoachError(
                "duplicate_alignment_key",
                "信息点 key 重复。",
                "每个信息点使用独立 key。",
            )
        source_quote = _short_text(point.get("source_quote"), "source_quote", 140)
        if source_quote not in source_text:
            raise CoachError(
                "source_quote_not_found",
                f"在本题原文找不到「{key}」的摘句。",
                "从题目原文复制短句。",
            )
        state = point.get("status")
        if not isinstance(state, str) or state not in POINT_STATUSES:
            raise CoachError(
                "invalid_alignment_status",
                f"「{key}」的判断无效。",
                "使用 preserved、distorted、omitted 或 uncertain。",
            )
        draft_quote = point.get("translation_quote")
        if state == "omitted":
            if draft_quote not in (None, ""):
                raise CoachError(
                    "omission_has_quote",
                    f"已标遗漏的「{key}」仍带译文摘句。",
                    "确实漏译时清空 translation_quote。",
                )
            draft_quote = None
        else:
            draft_quote = _short_text(draft_quote, "translation_quote", 180)
            if draft_quote not in draft_text:
                raise CoachError(
                    "translation_quote_not_found",
                    f"本次译稿没有「{key}」的摘句。",
                    "按当前稿件核对逐点引文。",
                )
        by_key[key] = {
            "key": key,
            "source_quote": source_quote,
            "translation_quote": draft_quote,
            "status": state,
        }

    if parent_points:
        expected = {row["key"]: row["source_quote"] for row in parent_points}
        if {key: row["source_quote"] for key, row in by_key.items()} != expected:
            raise CoachError(
                "alignment_scope_changed",
                "重译的点位或原文摘句与初译不一致。",
                "沿用原 key 与原文摘句，只更新译文证据与判断。",
            )
        return [by_key[key] for key in expected]
    return list(by_key.values())


def alignment_delta(
    before: list[dict[str, Any]] | None,
    after: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    if not before or not after:
        return None
    old = {row["key"]: row["status"] for row in before}
    if set(old) != {row["key"] for row in after}:
        return None
    transitions = [
        {"key": row["key"], "before": old[row["key"]], "after": row["status"]}
        for row in after
    ]
    return {
        "transitions": transitions,
        "repaired": [
            row["key"]
            for row in transitions
            if row["before"] != "preserved" and row["after"] == "preserved"
        ],
        "regressed": [
            row["key"]
            for row in transitions
            if row["before"] == "preserved" and row["after"] != "preserved"
        ],
        "unresolved": [
            row["key"] for row in transitions if row["after"] != "preserved"
        ],
    }
