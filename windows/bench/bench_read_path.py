"""Measure the read path on synthetic data, one factor at a time.

Run from ``windows/``::

    python bench/bench_read_path.py > bench-result.json

Two families, each varying one axis around a base case so the cases stay few:

* snapshot: database 8/32/64 MiB with a 1 MiB log, and log 0/1/8 MiB under a
  32 MiB database. For each it times a full decryption (``build_image``) and
  the log-only overlay (``update_image_with_wal``), checks that both produce
  the same bytes, and records the traced peak of one full decryption.
* sql: 10k/50k/200k rows, with and without a ``(chatId, logId)`` index, and a
  room whose newest rows are deleted. It counts SQLite VM steps for ``MAX``
  and for ``ORDER BY logId DESC LIMIT 1`` and checks they answer the same.

Every number is wall-clock on this machine with synthetic pages; none of it
is a claim about a real account. Timings are reported, never asserted.
"""

from __future__ import annotations

import hashlib
import json
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from kanao.snapshot import build_image, update_image_with_wal  # noqa: E402
from tests.support import PAGE_SIZE, KEY, build_database, encrypt_pages, encrypted_page, make_wal  # noqa: E402

MIB = 1024 * 1024
WARM_RUNS = 7


def timed(action, runs: int = WARM_RUNS) -> dict:
    cold_start = time.perf_counter()
    result = action()
    cold = time.perf_counter() - cold_start
    warm = []
    for _ in range(runs):
        start = time.perf_counter()
        action()
        warm.append(time.perf_counter() - start)
    return {
        "cold_ms": round(cold * 1000, 2),
        "warm_median_ms": round(statistics.median(warm) * 1000, 2),
        "warm_min_ms": round(min(warm) * 1000, 2),
        "warm_max_ms": round(max(warm) * 1000, 2),
        "result": result,
    }


def synthetic_pages(count: int, seed: int) -> list[bytes]:
    first = build_database(Path(tempfile.mkdtemp()) / "head.db", [(1, "synthetic")])[0]
    rng = random.Random(seed)
    return [first] + [rng.randbytes(PAGE_SIZE) for _ in range(count - 1)]


def snapshot_case(root: Path, database_mib: int, log_mib: float) -> dict:
    pages = synthetic_pages(database_mib * MIB // PAGE_SIZE, seed=database_mib)
    database = root / f"db{database_mib}-{log_mib}.edb"
    database.write_bytes(encrypt_pages(pages))
    log_pages = int(log_mib * MIB // PAGE_SIZE)
    log = database.with_name(database.name + "-wal")
    if log_pages:
        rng = random.Random(7)
        frames = [
            (number, len(pages) if index == log_pages - 1 else 0,
             encrypted_page(rng.randbytes(PAGE_SIZE), number=number))
            for index, number in enumerate(range(2, log_pages + 2))
        ]
        log.write_bytes(make_wal(frames))
    elif log.exists():
        log.unlink()

    full = timed(lambda: build_image(database, KEY))
    base = full["result"].base
    overlay = timed(lambda: update_image_with_wal(base, database, KEY))
    same = bytes(full["result"]) == bytes(overlay["result"])

    tracemalloc.start()
    build_image(database, KEY)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    for entry in (full, overlay):
        entry.pop("result")
    return {
        "database_mib": database_mib,
        "log_mib": log_mib,
        "full": full,
        "overlay": overlay,
        "overlay_equals_full": same,
        "full_traced_peak_mib": round(peak / MIB, 1),
        "full_traced_peak_over_database": round(peak / (database_mib * MIB), 2),
    }


def vm_steps(connection: sqlite3.Connection, sql: str, arguments: tuple) -> tuple[int, object]:
    steps = [0]

    def count() -> int:
        steps[0] += 1
        return 0

    connection.set_progress_handler(count, 1)
    try:
        answer = connection.execute(sql, arguments).fetchone()
    finally:
        connection.set_progress_handler(None, 0)
    return steps[0], answer


def sql_case(rows: int, indexed: bool, deleted_tail: bool) -> dict:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE messages (logId INTEGER PRIMARY KEY, chatId INTEGER, deleted INTEGER)"
    )
    rng = random.Random(rows)
    room = 17
    data = [(i, rng.randrange(50), 0) for i in range(1, rows + 1)]
    if deleted_tail:
        # The room's newest 500 rows are deleted, so the newest live one is deep.
        mine = [i for i, chat, _ in data if chat == room][-500:]
        marked = set(mine)
        data = [(i, chat, 1 if i in marked else 0) for i, chat, _ in data]
    connection.executemany("INSERT INTO messages VALUES (?, ?, ?)", data)
    if indexed:
        connection.execute("CREATE INDEX room_log ON messages (chatId, logId)")
    largest = "SELECT MAX(logId) FROM messages WHERE chatId = ? AND deleted = 0"
    walked = "SELECT logId FROM messages WHERE chatId = ? AND deleted = 0 ORDER BY logId DESC LIMIT 1"
    max_steps, max_answer = vm_steps(connection, largest, (room,))
    walk_steps, walk_answer = vm_steps(connection, walked, (room,))
    connection.close()
    return {
        "rows": rows,
        "indexed": indexed,
        "deleted_tail": deleted_tail,
        "max_vm_steps": max_steps,
        "desc_limit_vm_steps": walk_steps,
        "same_answer": max_answer == walk_answer,
    }


def main() -> int:
    report: dict = {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "warm_runs": WARM_RUNS,
        "snapshot": [],
        "sql": [],
    }
    with tempfile.TemporaryDirectory(prefix="nichirin-bench-") as directory:
        root = Path(directory)
        for database_mib, log_mib in ((8, 1), (32, 1), (64, 1), (32, 0), (32, 8)):
            report["snapshot"].append(snapshot_case(root, database_mib, log_mib))
    for rows in (10_000, 50_000, 200_000):
        for indexed in (False, True):
            report["sql"].append(sql_case(rows, indexed, deleted_tail=False))
    report["sql"].append(sql_case(50_000, False, deleted_tail=True))
    report["sql"].append(sql_case(50_000, True, deleted_tail=True))
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
