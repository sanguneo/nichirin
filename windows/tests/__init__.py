"""Make the shared ``kakaocommon`` package importable before any test runs.

A test module that imports ``kakaocommon`` first used to fail on its own and
pass inside the full suite, because only importing the platform package put
the repository root on the path. The root is *appended*, never put in front:
it also holds the redirect packages ``kanao``, ``kamado`` and ``kakaoread``,
which must never shadow the real ones in ``windows/``.
"""

import importlib.util
import sys
from pathlib import Path

if importlib.util.find_spec("kakaocommon") is None:
    sys.path.append(str(Path(__file__).resolve().parents[2]))
