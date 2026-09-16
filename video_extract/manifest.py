"""Safe manifest I/O, paths, fingerprints, and sanitized stage observations."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .contracts import SCHEMA_VERSION


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def package_path(package: Path, value: str | Path | None) -> Path | None:
    if not value:
        return None
    raw = Path(value)
    candidate = raw if raw.is_absolute() else package / raw
    resolved_package = package.resolve()
    resolved = candidate.resolve(strict=False)
    if resolved != resolved_package and resolved_package not in resolved.parents:
        raise ValueError(f"artifact escapes package: {value}")
    return resolved


def relative_path(package: Path, path: Path) -> str:
    return path.resolve().relative_to(package.resolve()).as_posix()


def fingerprint(paths: list[Path], parameters: dict[str, Any] | None = None) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda p: str(p)):
        digest.update(str(path.resolve()).encode())
        if path.exists():
            stat = path.stat()
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    digest.update(json.dumps(parameters or {}, ensure_ascii=False, sort_keys=True).encode())
    return digest.hexdigest()


def sanitize(value: Any) -> Any:
    """Remove query strings and common credential fields before persistence/output."""
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items() if k.lower() not in {"cookie", "cookies", "token", "authorization", "headers", "media_url", "signed_url"}}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        parts = urlsplit(value)
        safe_query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() in {"p", "page", "v", "bvid", "product_id", "sub_course_id"}]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), ""))
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(sanitize(value), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def update_stage(manifest_path: Path, gate: str, observation: dict[str, Any]) -> dict[str, Any]:
    data = read_json(manifest_path) if manifest_path.exists() else {}
    data["schema_version"] = SCHEMA_VERSION
    stages = data.setdefault("stages", {})
    stages[gate] = sanitize(observation)
    data["verified_gate"] = gate
    atomic_write_json(manifest_path, data)
    return data
