# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Run kanao-cli.py through WMI, outside an agent's CPU-capped Job Object.

Takes the same arguments as ``kanao-cli.py`` and returns the same output and
exit status; see ``kanao/wmi.py`` and the skill's "CPU-capped sandboxes".
"""

from __future__ import annotations

import io
import sys
from pathlib import Path


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")

    here = Path(__file__).resolve().parent
    runtime = here if (here / "kanao").is_dir() else here.parents[1]
    sys.path.insert(0, str(runtime))

    from kanao.wmi import Entry, main as run

    return run(sys.argv[1:], Entry(cli=here / "kanao-cli.py", launcher=Path(__file__).resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
