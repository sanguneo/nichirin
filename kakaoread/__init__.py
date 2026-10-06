"""The old ``kakaoread`` name from the repository root, as before the merge.

Each build ships its own alias package; this one runs the alias of the build
for this machine (``macos/`` on a Mac, ``windows/`` elsewhere).
"""

import sys
from pathlib import Path

_build = "macos" if sys.platform == "darwin" else "windows"
_real = Path(__file__).resolve().parents[1] / _build / "kakaoread"
__path__ = [str(_real)]
__file__ = str(_real / "__init__.py")
exec(compile((_real / "__init__.py").read_bytes(), __file__, "exec"))
