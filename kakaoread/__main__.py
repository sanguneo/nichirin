"""Entry point for the old ``python -m kakaoread``, and for the old script name.

The command still runs; it names the current one on the way through.  The
notice goes to stderr because stdout carries the JSON envelope a caller parses.
"""

from __future__ import annotations

import sys

from kamado.cli import main as _run

NOTICE = "kakaoread is now kamado-cli; run 'python3 -m kamado' instead."


def main() -> int:
    print(NOTICE, file=sys.stderr)
    return _run()


if __name__ == "__main__":
    raise SystemExit(main())
