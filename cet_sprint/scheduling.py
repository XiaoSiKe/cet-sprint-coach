"""FSRS adapter with an explicit, deterministic offline fallback."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from .storage import local_date, now_iso

FALLBACK_DAYS = (1, 3, 7, 14, 30)


def fsrs_status() -> dict[str, Any]:
    try:
        from importlib.metadata import version

        import fsrs

        installed_version = version("fsrs")
        return {"available": hasattr(fsrs, "Scheduler"), "version": installed_version}
    except ImportError:
        return {"available": False, "version": None}


def _cap_before_exam(due: date, exam_date: str | None, current: date) -> date:
    if exam_date is None:
        return due
    last = date.fromisoformat(exam_date) - timedelta(days=1)
    if last < current:
        return current
    return min(due, last)


def next_review(
    *,
    section: str,
    result: str,
    previous: dict[str, Any] | None,
    exam_date: str | None,
    rating: str | None = None,
    current: date | None = None,
) -> dict[str, Any] | None:
    if result not in {"correct", "partial", "wrong"}:
        return None
    today = current or local_date()
    old_stage = int(previous["stage"]) if previous else 0
    if section == "vocab":
        try:
            from fsrs import Card, Rating, Scheduler

            card = (
                Card.from_json(previous["card_json"])
                if previous
                and previous.get("scheduler") == "fsrs"
                and previous.get("card_json")
                else Card()
            )
            grade = (
                Rating.Again
                if result == "wrong"
                else Rating.Hard
                if result == "partial"
                else Rating.Easy
                if rating == "easy"
                else Rating.Good
            )
            card, _ = Scheduler().review_card(
                card, grade, review_datetime=datetime.now(UTC)
            )
            due = _cap_before_exam(card.due.date(), exam_date, today)
            return {
                "due_at": due.isoformat(),
                "stage": old_stage + 1 if result == "correct" else 0,
                "scheduler": "fsrs",
                "card_json": card.to_json(),
                "updated_at": now_iso(),
            }
        except ImportError:
            pass
    if result == "correct":
        due_days = FALLBACK_DAYS[min(old_stage, len(FALLBACK_DAYS) - 1)]
        stage = old_stage + 1
    else:
        due_days = 1
        stage = 0
    due = _cap_before_exam(today + timedelta(days=due_days), exam_date, today)
    return {
        "due_at": due.isoformat(),
        "stage": stage,
        "scheduler": "fixed_fallback" if section == "vocab" else "fixed_interval",
        "card_json": None,
        "updated_at": now_iso(),
    }
