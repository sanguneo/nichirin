"""Local KakaoTalk reading and friends, plus client-driven sending on Windows."""

__version__ = "0.31.1"


def _reach_common() -> None:
    """Make the shared ``kakaocommon`` package importable from a checkout.

    An installed skill ships it beside this package. A checkout keeps it at the
    repository root, one level above ``windows/``.
    """
    import importlib.util
    import sys
    from pathlib import Path

    if importlib.util.find_spec("kakaocommon") is None:
        sys.path.append(str(Path(__file__).resolve().parents[2]))


_reach_common()
del _reach_common

PAGE_SIZE = 4096
"""SQLCipher page size used by the KakaoTalk Windows client."""

RESERVE_SIZE = 80
"""Bytes reserved at the end of each page for the IV and HMAC."""
