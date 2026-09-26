#!/usr/bin/env python3
"""Entrypoint that keeps imports stable from any student workspace."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cet_sprint.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
