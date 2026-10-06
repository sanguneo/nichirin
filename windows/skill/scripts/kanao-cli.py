# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Launcher for an installed copy of the skill.

Imports resolve relative to this file, so the installed skill keeps working
after the source checkout it came from is moved or deleted.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path


def main() -> int:
    # Console pipes on a Korean Windows install default to a legacy code page,
    # which would mangle the conversation text this tool exists to return.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")

    here = Path(__file__).resolve().parent
    runtime = here if (here / "kanao").is_dir() else here.parents[1]
    sys.path.insert(0, str(runtime))

    from kanao.cli import main as run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
