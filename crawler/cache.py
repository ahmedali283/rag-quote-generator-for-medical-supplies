"""
Filesystem cache for raw HTTP responses.

Layout: cache/<source>/<sha256(url)>.<ext>

All writes are atomic (write to .tmp, then os.replace) to prevent partial
reads if the process is killed mid-write. A cache miss always returns None
rather than raising, so callers never need to guard against cache errors.
"""

import hashlib
import os
from pathlib import Path
from typing import Optional

CACHE_ROOT = Path("cache")


def cache_key(url: str) -> str:
    """Return the sha256 hex digest of a URL string. Used as the filename stem."""
    return hashlib.sha256(url.encode()).hexdigest()


def _cache_path(source: str, url: str, ext: str) -> Path:
    return CACHE_ROOT / source / f"{cache_key(url)}.{ext}"


def exists(source: str, url: str, ext: str = "html") -> bool:
    """Fast existence check without reading content."""
    return _cache_path(source, url, ext).exists()


def get(source: str, url: str, ext: str = "html") -> Optional[bytes]:
    """
    Return cached bytes if the file exists, else None.
    Never raises — a missing or unreadable file is treated as a cache miss.
    """
    path = _cache_path(source, url, ext)
    try:
        return path.read_bytes()
    except (FileNotFoundError, PermissionError):
        return None


def put(source: str, url: str, content: bytes, ext: str = "html") -> None:
    """
    Atomically write bytes to the cache.

    Uses a .tmp sibling file + os.replace so that concurrent readers never
    observe a partial write, even on a hard kill.
    """
    path = _cache_path(source, url, ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(content)
        os.replace(tmp, path)
    finally:
        # Clean up the temp file if replace failed for any reason.
        if tmp.exists():
            tmp.unlink(missing_ok=True)
