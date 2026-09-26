#!/usr/bin/env python3
"""Small repository gate for the bundled Skill and local references."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def validate(root: Path) -> list[str]:
    failures: list[str] = []
    skill = root / "SKILL.md"
    if not skill.is_file():
        return ["SKILL.md is missing"]
    body = skill.read_text(encoding="utf-8")
    if not body.startswith("---\n") or "\n---\n" not in body[4:]:
        failures.append("SKILL.md needs YAML frontmatter")
    else:
        frontmatter = body.split("\n---\n", 1)[0]
        name = re.search(r"(?m)^name:\s*(\S+)\s*$", frontmatter)
        description = re.search(r"(?m)^description:\s*(.+)$", frontmatter)
        if name is None or name.group(1) != root.name:
            failures.append("frontmatter name must match the skill directory")
        if description is None or not description.group(1).strip():
            failures.append("frontmatter description is missing")
    for markdown in (
        root / "SKILL.md",
        *(root / "references").glob("*.md"),
        root / "README.md",
    ):
        if not markdown.is_file():
            continue
        content = markdown.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content):
            if target.startswith(("https://", "http://", "#", "mailto:")):
                continue
            if not (markdown.parent / target.split("#", 1)[0]).is_file():
                failures.append(
                    f"broken link: {markdown.relative_to(root)} -> {target}"
                )
    for filename in ("sources.lock.json", "eval/cases.json"):
        try:
            json.loads((root / filename).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"invalid {filename}: {exc}")
    return failures


if __name__ == "__main__":
    errors = validate(Path(__file__).resolve().parents[1])
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)
    print("Skill metadata, local links and bundled JSON are valid.")
