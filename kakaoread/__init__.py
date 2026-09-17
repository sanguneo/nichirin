"""The name this project carried before it became ``kanao-cli``.

Nothing is implemented here.  Every module under ``kakaoread`` resolves to the
module of the same name under :mod:`kanao`, and to the *same object* — so a
class imported through the old name is the class imported through the new one,
and an exception raised under one is caught under the other.  Loading a second
copy instead would give the two names separate ``KanaoError`` classes, which is
the kind of breakage a compatibility shim exists to prevent.

Existing commands keep working; new code should import :mod:`kanao`.
"""

from __future__ import annotations

import sys
import warnings
from importlib import import_module
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType
from typing import Sequence

CURRENT_NAME = "kanao"
DEPRECATED_NAME = __name__

#: Modules this package really ships, which must not be aliased away.
_OWN_MODULES = frozenset({f"{DEPRECATED_NAME}.__main__"})


class _AliasLoader(Loader):
    """Hand back the :mod:`kanao` module that a ``kakaoread`` name stands for."""

    def __init__(self, target: str) -> None:
        self._target = target
        self._spec: ModuleSpec | None = None

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        module = import_module(self._target)
        self._spec = module.__spec__
        return module

    def exec_module(self, module: ModuleType) -> None:
        """Undo the one attribute that loading under an alias rewrites.

        The module already ran under its real name, so there is nothing to
        execute.  Import sets ``__spec__`` to the alias on the way through and
        leaves ``__name__`` alone, so the real spec is put back rather than
        letting :mod:`kanao` describe itself as ``kakaoread``.
        """
        module.__spec__ = self._spec


class _AliasFinder(MetaPathFinder):
    """Resolve ``kakaoread`` and everything under it to :mod:`kanao`."""

    def find_spec(
        self,
        name: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if name in _OWN_MODULES:
            return None
        if name != DEPRECATED_NAME and not name.startswith(f"{DEPRECATED_NAME}."):
            return None
        current = CURRENT_NAME + name[len(DEPRECATED_NAME) :]
        return ModuleSpec(name, _AliasLoader(current), is_package=True)


sys.meta_path.insert(0, _AliasFinder())

warnings.warn(
    f"{DEPRECATED_NAME} is now kanao-cli; import {CURRENT_NAME} instead.",
    DeprecationWarning,
    stacklevel=2,
)


def __getattr__(name: str) -> object:
    """Read ``kanao``'s own attributes, such as ``__version__``, through here."""
    return getattr(import_module(CURRENT_NAME), name)
