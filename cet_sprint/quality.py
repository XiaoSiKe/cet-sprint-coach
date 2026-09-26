"""Deterministic gates for trainable and original CET items.

These checks catch structural mistakes. They do not prove that a generated
question has one semantically correct answer; the authoring protocol requires
an independent evidence check before using generated keys for feedback.
"""

from __future__ import annotations

import re

from .storage import CoachError

CHOICE_SUBTYPES = {
    "short_news",
    "long_conversation",
    "listening_passage",
    "lecture",
    "careful_reading",
}
OPTION_RE = re.compile(r"(?<![A-Za-z0-9])([A-D])[.)、]\s*", re.IGNORECASE)
LEAK_RE = re.compile(
    r"(?:参考答案|正确答案|答案|correct\s+answer|answer(?:\s+key)?)\s*[:：]\s*\S+",
    re.IGNORECASE,
)


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def _check_four_options(prompt: str) -> None:
    matches = list(OPTION_RE.finditer(prompt))
    labels = [match.group(1).upper() for match in matches]
    if labels != ["A", "B", "C", "D"]:
        raise CoachError(
            "invalid_choice_options",
            "单选题必须恰有按 A、B、C、D 排列的四个选项。",
            "补齐题干中的四个选项，或选择实际题型。",
        )
    options = [
        prompt[
            match.end() : matches[index + 1].start() if index < 3 else len(prompt)
        ].strip()
        for index, match in enumerate(matches)
    ]
    if (
        any(not value for value in options)
        or len({_normalized(value) for value in options}) != 4
    ):
        raise CoachError(
            "duplicate_choice_options",
            "单选题选项为空或重复。",
            "让四个选项完整且彼此不同，再核对唯一最佳答案。",
        )


def validate_item_quality(
    *,
    section: str,
    subtype: str,
    prompt: str,
    answer: str | None,
    answer_status: str,
    evidence: str | None,
    transcript: str | None,
    source_type: str,
    material_text: str = "",
) -> None:
    if LEAK_RE.search(prompt):
        raise CoachError(
            "answer_leak_in_prompt",
            "题干中含显式答案标记，不能直接展示给学生。",
            "把答案移到 answer 字段，题干只留材料、问题和选项。",
        )
    if subtype in CHOICE_SUBTYPES:
        _check_four_options(prompt)
        if answer_status in {"verified", "generated"} and (
            answer or ""
        ).strip().upper() not in {"A", "B", "C", "D"}:
            raise CoachError(
                "invalid_choice_answer",
                "已给答案的单选题只能用 A、B、C 或 D 作答案。",
                "核对唯一最佳选项和来源依据。",
            )
    if (
        source_type == "original"
        and answer_status == "generated"
        and section in {"reading", "listening"}
    ):
        if section == "listening" and not (transcript or "").strip():
            raise CoachError(
                "missing_original_transcript",
                "原创听力题需要独立保存的脚本供答案核验。",
                "先补 transcript，作答前保持隐藏。",
            )
        quote = (evidence or "").strip()
        if len(quote) < 8:
            raise CoachError(
                "missing_generated_evidence",
                "原创阅读或听力答案需要具体原文依据。",
                "提供至少一句可定位的原文短句，不能只写题号或答案字母。",
            )
        if section == "listening":
            source = transcript or ""
        else:
            option_start = OPTION_RE.search(prompt)
            source = (
                material_text
                + "\n"
                + (prompt[: option_start.start()] if option_start else prompt)
            )
        if _normalized(quote) not in _normalized(source or ""):
            raise CoachError(
                "generated_evidence_mismatch",
                "原创题的答案依据未出现在阅读材料或听力脚本中。",
                "逐字核对依据与材料；不确定时将答案标为 unverified。",
            )
