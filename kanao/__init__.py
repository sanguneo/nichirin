"""``python -m kanao`` from the repository root, as before the merge.

The package lives in ``windows/kanao``. This one points its search path there
and runs the real ``__init__``, so every ``kanao.*`` module is loaded from
``windows/`` exactly once and under its usual name.
"""

from pathlib import Path

_real = Path(__file__).resolve().parents[1] / "windows" / "kanao"
__path__ = [str(_real)]
__file__ = str(_real / "__init__.py")
exec(compile((_real / "__init__.py").read_bytes(), __file__, "exec"))
