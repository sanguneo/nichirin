"""``python3 -m kamado`` from the repository root, as before the merge.

The package lives in ``macos/kamado``. This one points its search path there
and runs the real ``__init__``, so every ``kamado.*`` module is loaded from
``macos/`` exactly once and under its usual name.
"""

from pathlib import Path

_real = Path(__file__).resolve().parents[1] / "macos" / "kamado"
__path__ = [str(_real)]
__file__ = str(_real / "__init__.py")
exec(compile((_real / "__init__.py").read_bytes(), __file__, "exec"))
