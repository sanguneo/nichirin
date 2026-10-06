# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""The launcher's old name, kept so commands written against it keep working.

It resolves the runtime exactly as ``kamado-cli.py`` does — an installed skill
has to keep working after the checkout it came from is moved or deleted — and
names the current launcher on stderr, where it cannot corrupt the JSON envelope
on stdout.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

NOTICE = "kakaoread is now kamado-cli; use scripts/kamado-cli.py instead."


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")

    print(NOTICE, file=sys.stderr)

    here = Path(__file__).resolve().parent
    runtime = here if (here / "kamado").is_dir() else here.parents[1]
    sys.path.insert(0, str(runtime))

    from kamado.cli import main as run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
