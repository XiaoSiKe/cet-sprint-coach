"""Read-only material extraction and safe local audio generation."""

from __future__ import annotations

import hashlib
import html
import re
import shutil
import subprocess
import tempfile
import wave
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .storage import CoachError

TEXT_SUFFIXES = {".txt", ".md", ".html", ".htm", ".docx", ".pdf"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".aiff", ".flac"}
SUPPORTED = TEXT_SUFFIXES | AUDIO_SUFFIXES


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_encoded(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def _read_docx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            data = archive.read("word/document.xml")
        root = ElementTree.fromstring(data)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise CoachError(
            "invalid_docx",
            "DOCX 无法提取文字。",
            "检查文件是否损坏，或转换为 TXT/MD 后导入。",
        ) from exc
    paragraphs = []
    for paragraph in root.iter():
        if paragraph.tag.endswith("}p"):
            words = [
                node.text or "" for node in paragraph.iter() if node.tag.endswith("}t")
            ]
            if words:
                paragraphs.append("".join(words))
    return "\n".join(paragraphs)


def _read_pdf(path: Path) -> tuple[str, list[str]]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", ["未安装 pypdf；PDF 文字未提取。"]
    try:
        reader = PdfReader(str(path))
        pages = [
            f"[Page {index}]\n{page.extract_text() or ''}"
            for index, page in enumerate(reader.pages, 1)
        ]
    except Exception as exc:
        raise CoachError(
            "invalid_pdf", f"PDF 无法解析：{exc}", "检查文件是否损坏或换用文本版。"
        ) from exc
    text = "\n\n".join(pages)
    warnings = []
    if len(re.sub(r"\s+", "", text)) < 20:
        warnings.append(
            "PDF 几乎没有可提取文字，可能是扫描件；需 OCR 并人工核对，不自动建题。"
        )
    return text, warnings


def extract(path: Path) -> tuple[str, list[str]]:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise CoachError(
            "unsupported_material",
            f"不支持 {suffix or '无扩展名'} 文件。",
            "使用 PDF、DOCX、TXT、MD、HTML 或常见音频。",
        )
    if suffix in TEXT_SUFFIXES and path.stat().st_size > 50 * 1024 * 1024:
        raise CoachError(
            "material_too_large",
            "文本材料超过 50 MiB，未导入。",
            "按试卷或章节拆分后再导入，避免把大量无关内容写进学习档案。",
        )
    if suffix in AUDIO_SUFFIXES:
        return "", ["音频仅建立本地索引；未自动生成转写或题目。"]
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix == ".docx":
        return _read_docx(path), []
    text = _read_encoded(path)
    if suffix in {".html", ".htm"}:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
        text = html.unescape(re.sub(r"(?s)<[^>]+>", "\n", text))
    return text, []


def validate_file(raw: str) -> Path:
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise CoachError(
            "material_not_found", f"找不到文件：{path}", "核对路径后重新导入。"
        )
    return path


def validate_audio(raw: str) -> Path:
    path = validate_file(raw)
    if path.suffix.lower() not in AUDIO_SUFFIXES:
        raise CoachError(
            "invalid_audio",
            f"不是支持的音频格式：{path.suffix}",
            "改用 MP3、M4A、WAV、OGG、AAC、AIFF 或 FLAC。",
        )
    if path.stat().st_size == 0:
        raise CoachError("empty_audio", "音频文件为空。", "提供可播放的音频。")
    probe = shutil.which("ffprobe")
    converter = shutil.which("ffmpeg")
    try:
        if probe:
            checked = subprocess.run(
                [
                    probe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if checked.returncode != 0 or not checked.stdout.strip():
                raise CoachError(
                    "unplayable_audio",
                    "音频无法解码或没有音轨。",
                    "检查文件后重新绑定。",
                )
        elif converter:
            checked = subprocess.run(
                [
                    converter,
                    "-v",
                    "error",
                    "-i",
                    str(path),
                    "-map",
                    "0:a:0",
                    "-t",
                    "0.1",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if checked.returncode != 0:
                raise CoachError(
                    "unplayable_audio",
                    "音频无法解码或没有音轨。",
                    "检查文件后重新绑定。",
                )
        elif path.suffix.lower() == ".wav":
            try:
                with wave.open(str(path), "rb") as handle:
                    if handle.getnframes() <= 0:
                        raise ValueError("no frames")
            except (wave.Error, EOFError, ValueError) as exc:
                raise CoachError(
                    "unplayable_audio", "WAV 文件无法解码。", "检查文件后重新绑定。"
                ) from exc
        else:
            raise CoachError(
                "audio_probe_unavailable",
                "缺少 ffmpeg/ffprobe，无法验证这种音频是否可播放。",
                "安装 ffmpeg，或提供有效 WAV 文件。",
            )
    except subprocess.TimeoutExpired as exc:
        raise CoachError(
            "audio_probe_timeout", "音频校验超时。", "检查文件是否损坏或改用较短音频。"
        ) from exc
    return path


def synthesize_mac_audio(transcript: str, output: Path) -> None:
    if not transcript.strip():
        raise CoachError(
            "missing_transcript",
            "原创听力题还没有脚本。",
            "先为题目添加 --transcript-file。",
        )
    if shutil.which("say") is None:
        raise CoachError(
            "tts_unavailable",
            "本机没有可用的 macOS say。",
            "改用用户提供的可播放音频，或仅做脚本分析。",
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".txt", delete=False
    ) as handle:
        handle.write(transcript)
        source = Path(handle.name)
    try:
        done = subprocess.run(
            ["say", "-f", str(source), "-o", str(output)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if done.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            output.unlink(missing_ok=True)
            raise CoachError(
                "tts_failed",
                f"音频生成失败：{done.stderr.strip() or '未知错误'}",
                "检查系统语音功能，或使用已有音频。",
            )
        validate_audio(str(output))
    finally:
        source.unlink(missing_ok=True)
