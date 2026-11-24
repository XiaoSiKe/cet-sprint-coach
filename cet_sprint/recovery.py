"""Restore an explicitly selected, standalone SQLite backup."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from .materials import sha256_file
from .storage import SCHEMA_VERSION, CoachError, db_path

REQUIRED_TABLES = {
    "profile",
    "materials",
    "items",
    "attempts",
    "review_cards",
    "checkpoints",
    "plans",
    "task_logs",
}
CORRUPTION_CODES = {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def _require_closed(path: Path) -> None:
    if any(
        Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
    ):
        raise CoachError(
            "recovery_sidecars_present",
            "数据库旁还有写入日志或活动副文件。",
            "先关闭该档案的写入进程，备份和恢复均使用完整、独立的 SQLite 文件。",
        )


def _stage_copy(source: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(
        prefix=".restore-", suffix=".sqlite3", dir=directory
    )
    path = Path(filename)
    try:
        with os.fdopen(fd, "wb") as target, source.open("rb") as handle:
            shutil.copyfileobj(handle, target)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _backup_metadata(candidate: Path) -> dict[str, Any]:
    try:
        with closing(_readonly(candidate)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not 1 <= version <= SCHEMA_VERSION or not REQUIRED_TABLES.issubset(
                names
            ):
                raise ValueError("unsupported CET archive")
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("SQLite integrity check failed")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("SQLite foreign key check failed")
            profile = conn.execute("SELECT level FROM profile WHERE id=1").fetchone()
            if not profile or profile[0] not in (4, 6):
                raise ValueError("missing CET-4 or CET-6 profile")
            return {
                "backup_schema_version": version,
                "level": profile[0],
                "attempts": conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
                "review_cards": conn.execute(
                    "SELECT COUNT(*) FROM review_cards"
                ).fetchone()[0],
            }
    except (sqlite3.Error, ValueError) as exc:
        raise CoachError(
            "invalid_database_backup",
            "所选备份未通过四六级档案完整性检查。",
            "核对完整的 SQLite 备份。",
        ) from exc


def _target_digest(target: Path, incoming: str) -> str | None:
    if not target.exists():
        return None
    _require_closed(target)
    current = sha256_file(target)
    if current == incoming or target.stat().st_size == 0:
        return current
    try:
        with closing(_readonly(target)) as conn:
            healthy = conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        if code not in CORRUPTION_CODES:
            raise CoachError(
                "recovery_target_unverified",
                "无法确认当前档案是否损坏。",
                "先确认文件可读且没有活动写入，再核对恢复条件。",
            ) from exc
        healthy = False
    if healthy:
        raise CoachError(
            "recovery_target_healthy",
            "当前档案可读取，恢复命令拒绝覆盖。",
            "继续读取现有档案，或把备份恢复到新的独立工作区进行比对。",
        )
    return current


def _archive_damaged(target: Path, digest: str) -> Path:
    directory = target.parent / "recovery"
    archive = directory / f"state.sqlite3.{digest}.bak"
    if archive.exists():
        if sha256_file(archive) != digest:
            raise CoachError(
                "recovery_archive_conflict",
                "损坏原件的备份路径存在冲突。",
                "核对恢复目录。",
            )
        return archive
    staged = _stage_copy(target, directory)
    try:
        if sha256_file(staged) != digest:
            raise CoachError(
                "recovery_target_changed",
                "保存原件期间档案发生了变化。",
                "关闭写入后重新核对。",
            )
        os.replace(staged, archive)
    finally:
        staged.unlink(missing_ok=True)
    return archive


def restore_database(workspace: Path, backup_path: str) -> dict[str, Any]:
    source = Path(backup_path).expanduser().resolve()
    target = db_path(workspace)
    if target.is_symlink():
        raise CoachError(
            "recovery_target_unverified",
            "目标数据库是符号链接。",
            "先核对实际学习档案位置。",
        )
    if not source.is_file() or source == target:
        raise CoachError(
            "invalid_database_backup",
            "请选择独立的 SQLite 备份文件。",
            "核对 --backup 指定路径。",
        )
    _require_closed(source)
    _require_closed(target)
    incoming = sha256_file(source)
    staged = _stage_copy(source, target.parent)
    try:
        if sha256_file(staged) != incoming:
            raise CoachError(
                "recovery_backup_changed",
                "核对期间备份发生变化。",
                "锁定已确认的备份文件后重试。",
            )
        _require_closed(source)
        metadata = _backup_metadata(staged)
        current = _target_digest(target, incoming)
        if current == incoming:
            return {
                "restored": False,
                "already_matches": True,
                "state_path": str(target),
                **metadata,
            }
        preserved = _archive_damaged(target, current) if current is not None else None
        _require_closed(target)
        if (sha256_file(target) if target.exists() else None) != current:
            raise CoachError(
                "recovery_target_changed",
                "准备发布时当前档案发生变化。",
                "保留原件并重新核对。",
            )
        os.replace(staged, target)
        return {
            "restored": True,
            "state_path": str(target),
            "preserved_damaged_database": str(preserved) if preserved else None,
            **metadata,
        }
    finally:
        staged.unlink(missing_ok=True)
