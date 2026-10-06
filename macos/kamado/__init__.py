"""Local KakaoTalk reading and friends, plus client-driven sending on macOS."""

__version__ = "0.29.0"


def _reach_common() -> None:
    """Make the shared ``kakaocommon`` package importable from a checkout.

    An installed skill ships it beside this package. A checkout keeps it at the
    repository root, one level above ``macos/``.
    """
    import importlib.util
    import sys
    from pathlib import Path

    if importlib.util.find_spec("kakaocommon") is None:
        sys.path.append(str(Path(__file__).resolve().parents[2]))


_reach_common()
del _reach_common
