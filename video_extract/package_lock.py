"""Process-safe advisory locks for writes to one package."""

from __future__ import annotations

import fcntl
import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path


class PackageBusyError(RuntimeError):
    pass


@contextmanager
def package_lock(package: Path):
    digest = hashlib.sha256(str(package.expanduser().resolve()).encode()).hexdigest()
    path = Path(tempfile.gettempdir()) / f"video-extract-{digest}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PackageBusyError(f"package is busy: {package}") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
