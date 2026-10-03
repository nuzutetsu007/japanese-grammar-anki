#!/usr/bin/env python3
"""阶段 0:建库(幂等)。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DB_PATH, connect, init_db, now  # noqa: E402


def main() -> int:
    conn = connect()
    init_db(conn)
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(f"[{now()}] DB ready: {DB_PATH}")
    print("tables:", ", ".join(tables))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
