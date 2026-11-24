#!/usr/bin/env python3
"""Entrypoint that keeps imports stable from any student workspace."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def run() -> int:
    if not (3, 12) <= sys.version_info[:2] < (3, 15):
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "unsupported_python",
                    "message": "本地引擎需要 Python 3.12、3.13 或 3.14。",
                    "recovery": "使用宿主已有的兼容 Python，或进入仅对话模式。",
                },
                separators=(",", ":"),
            )
        )
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from cet_sprint.cli import main

    return main()


if __name__ == "__main__":
    raise SystemExit(run())
