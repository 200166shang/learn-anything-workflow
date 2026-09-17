"""Sanitized, rebuildable receipt projection for NetEase operations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .manifest import atomic_write_json, read_json


def projection(record: dict[str, Any]) -> dict[str, Any]:
    value = {
        "schema_version": 1, "operation_id": record["operation_id"],
        "authoritative_revision": record["commit"]["revision"],
        "authoritative_digest": record["commit"]["digest"],
        "source_identity": record["intent"]["source_identity"],
        "audio_operation_id": record["intent"]["audio_operation_id"],
        "audio_sha256": record["intent"]["output_sha256"], "filename": record["intent"]["filename"],
        "remote_id": record["remote"]["id"], "visible": True,
        "confirmation": "cloud_list_identity_match", "confirmed_at": record["remote"]["confirmed_at"],
    }
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    value["projection_digest"] = hashlib.sha256(encoded).hexdigest()
    return value


def project(record: dict[str, Any]) -> None:
    value = projection(record)
    for raw in record["artifact_refs"]:
        atomic_write_json(Path(raw), value)


def valid(record: dict[str, Any]) -> bool:
    try:
        expected = projection(record)
    except (KeyError, TypeError, ValueError):
        return False
    for raw in record.get("artifact_refs", []):
        path = Path(raw)
        if not path.is_file():
            return False
        try:
            if read_json(path) != expected:
                return False
        except (OSError, json.JSONDecodeError):
            return False
    return bool(record.get("artifact_refs"))
