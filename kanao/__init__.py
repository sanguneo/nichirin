"""Local KakaoTalk reading and friends, plus client-driven sending on Windows."""

__version__ = "0.7.0"

PAGE_SIZE = 4096
"""SQLCipher page size used by the KakaoTalk Windows client."""

RESERVE_SIZE = 80
"""Bytes reserved at the end of each page for the IV and HMAC."""
