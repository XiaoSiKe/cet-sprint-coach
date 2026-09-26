"""SQLite state, migrations and shared validation for the CET coach."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SECTIONS = ("listening", "reading", "writing", "translation", "vocab")
OBJECTIVE_SUBTYPES = {
    "short_news",
    "long_conversation",
    "listening_passage",
    "lecture",
    "banked_cloze",
    "matching",
    "careful_reading",
    "vocab",
}
SUBTYPES = {
    4: {
        "listening": {"short_news", "long_conversation", "listening_passage"},
        "reading": {"banked_cloze", "matching", "careful_reading"},
        "writing": {"essay"},
        "translation": {"paragraph"},
        "vocab": {"vocab"},
    },
    6: {
        "listening": {"long_conversation", "listening_passage", "lecture"},
        "reading": {"banked_cloze", "matching", "careful_reading"},
        "writing": {"essay"},
        "translation": {"paragraph"},
        "vocab": {"vocab"},
    },
}


class CoachError(Exception):
    def __init__(self, code: str, message: str, recovery: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recovery = recovery


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def local_date() -> date:
    """Use the learner machine's local calendar day for daily coaching."""
    return datetime.now().astimezone().date()


def today_iso() -> str:
    return local_date().isoformat()


def stable_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:20]


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def parse_date(value: str | None, field: str = "date") -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise CoachError(
            "invalid_date", f"{field} 必须是 YYYY-MM-DD。", "核对日期后重试。"
        ) from exc


def check_level(level: int) -> None:
    if level not in (4, 6):
        raise CoachError(
            "invalid_level",
            "只支持 CET-4 或 CET-6 笔试。",
            "使用 --level 4 或 --level 6。",
        )


def check_section(level: int, section: str, subtype: str | None = None) -> None:
    check_level(level)
    if section not in SECTIONS:
        raise CoachError(
            "invalid_section",
            f"不支持的模块：{section}",
            f"使用 {', '.join(SECTIONS)}。",
        )
    if subtype is not None and subtype not in SUBTYPES[level][section]:
        allowed = ", ".join(sorted(SUBTYPES[level][section]))
        raise CoachError(
            "invalid_subtype",
            f"CET-{level} 的 {section} 不含题型 {subtype}。",
            f"可用题型：{allowed}。",
        )


def state_root(workspace: Path) -> Path:
    return workspace.expanduser().resolve() / ".cet-sprint"


def db_path(workspace: Path) -> Path:
    return state_root(workspace) / "state.sqlite3"


SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  level INTEGER NOT NULL CHECK (level IN (4, 6)),
  exam_date TEXT,
  target_score INTEGER CHECK (target_score BETWEEN 0 AND 710),
  weekday_minutes INTEGER NOT NULL CHECK (weekday_minutes BETWEEN 0 AND 720),
  weekend_minutes INTEGER NOT NULL CHECK (weekend_minutes BETWEEN 0 AND 720),
  weak_section TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS materials (
  id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL UNIQUE,
  level INTEGER NOT NULL CHECK (level IN (4, 6)),
  section TEXT,
  subtype TEXT,
  source_type TEXT NOT NULL,
  year INTEGER,
  locator TEXT,
  source_url TEXT,
  verified_at TEXT,
  confidence TEXT NOT NULL,
  extracted_text TEXT NOT NULL DEFAULT '',
  warnings_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  material_id TEXT REFERENCES materials(id),
  level INTEGER NOT NULL CHECK (level IN (4, 6)),
  section TEXT NOT NULL,
  subtype TEXT NOT NULL,
  prompt TEXT NOT NULL,
  answer TEXT,
  answer_status TEXT NOT NULL,
  evidence TEXT,
  locator TEXT,
  audio_path TEXT,
  transcript TEXT,
  source_type TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES items(id),
  made_at TEXT NOT NULL,
  response TEXT,
  result TEXT NOT NULL,
  minutes REAL NOT NULL CHECK (minutes >= 0),
  error_tag TEXT,
  judgment_basis TEXT NOT NULL,
  feedback TEXT,
  evidence TEXT
);
CREATE TABLE IF NOT EXISTS review_cards (
  item_id TEXT PRIMARY KEY REFERENCES items(id),
  due_at TEXT NOT NULL,
  stage INTEGER NOT NULL DEFAULT 0,
  scheduler TEXT NOT NULL,
  card_json TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  level INTEGER NOT NULL,
  taken_on TEXT NOT NULL,
  section TEXT,
  correct INTEGER,
  question_total INTEGER,
  minutes REAL,
  score_total INTEGER,
  score_listening INTEGER,
  score_reading INTEGER,
  score_writing_translation INTEGER,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
  week_start TEXT PRIMARY KEY,
  generated_at TEXT NOT NULL,
  focus TEXT,
  basis TEXT NOT NULL,
  profile_updated_at TEXT NOT NULL,
  latest_checkpoint TEXT,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_logs (
  id TEXT PRIMARY KEY,
  task_date TEXT NOT NULL,
  role TEXT NOT NULL,
  section TEXT,
  title TEXT NOT NULL,
  planned_minutes INTEGER NOT NULL,
  completed INTEGER NOT NULL DEFAULT 0,
  actual_minutes REAL,
  evidence TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_section ON items(level, section, subtype, active);
CREATE INDEX IF NOT EXISTS idx_attempts_item ON attempts(item_id, made_at);
CREATE INDEX IF NOT EXISTS idx_attempts_date ON attempts(made_at);
CREATE INDEX IF NOT EXISTS idx_review_due ON review_cards(due_at);
CREATE INDEX IF NOT EXISTS idx_checkpoint_date ON checkpoints(taken_on);
"""


def connect(workspace: Path, create: bool = False) -> sqlite3.Connection:
    root = state_root(workspace)
    path = root / "state.sqlite3"
    exists = path.exists()
    if not exists and not create:
        raise CoachError(
            "not_initialized",
            "这个学习目录还没有四六级档案。",
            "先运行 init；只需目标级别即可建档。",
        )
    if create:
        root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        conn.close()
        raise CoachError(
            "newer_schema",
            "档案由更新版本创建，当前引擎不能安全读取。",
            "使用创建该档案的新版 Skill。",
        )
    if version < SCHEMA_VERSION:
        if exists:
            backup = (
                root
                / f"state.sqlite3.v{version}.{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}.bak"
            )
            with sqlite3.connect(backup) as target:
                conn.backup(target)
            if version == 0:
                tables = [
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                ]
                if tables:
                    conn.close()
                    raise CoachError(
                        "unknown_schema",
                        "发现未标版本的数据库，未做自动迁移。",
                        f"原库已备份为 {backup.name}；先核对来源，再使用适配的迁移器。",
                    )
        try:
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        except sqlite3.Error:
            conn.close()
            raise
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def require_profile(conn: sqlite3.Connection) -> dict[str, Any]:
    row = row_dict(conn.execute("SELECT * FROM profile WHERE id = 1").fetchone())
    if row is None:
        raise CoachError(
            "not_initialized", "学习档案缺少基础信息。", "运行 init 或从备份恢复。"
        )
    return row


def validate_material_source(
    source_type: str, year: int | None, source_url: str | None, verified_at: str | None
) -> None:
    if source_type not in {"official", "past_paper", "user_material", "original"}:
        raise CoachError(
            "invalid_source_type",
            "材料来源类型无效。",
            "选择 official、past_paper、user_material 或 original。",
        )
    if source_type == "official" and (not source_url or not verified_at):
        raise CoachError(
            "unverified_official_source",
            "官方材料需要来源 URL 与核验日期。",
            "补充 --source-url 与 --verified-at。",
        )
    if source_type == "past_paper" and year is None:
        raise CoachError(
            "unverified_past_paper",
            "真题材料必须能确认年份。",
            "补充 --year；无法核实时改为 user_material。",
        )
    if year is not None and not 1980 <= year <= local_date().year:
        raise CoachError("invalid_year", "材料年份不合理。", "核对 --year。")
