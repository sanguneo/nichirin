"""Entry point for ``python -m nichirin``."""

import sys
from importlib import import_module

BUILD = "kamado" if sys.platform == "darwin" else "kanao"

if __name__ == "__main__":
    raise SystemExit(import_module(f"{BUILD}.cli").main())
