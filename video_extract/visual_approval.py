"""Bind media-note publication to recorded visual preparation decisions."""

import hashlib
import json
from pathlib import Path

from .manifest import atomic_write_json, read_json
from .package_lock import package_lock


def _path(config, source_id, source_version):
    from .authoritative_notes import _safe
    key = hashlib.sha256(f"{source_id}:{source_version}".encode()).hexdigest()
    return _safe(config.results, "source-notes", "visual-preparations", f"{key}.json")


def record_preparation(config, source_id, source_version, review):
    from .authoritative_notes import _safe
    path = _path(config, source_id, source_version)
    path.parent.mkdir(parents=True, exist_ok=True)
    with package_lock(_safe(config.results, "source-notes")):
        value = {"source_id": source_id, "source_version": source_version, "visual_review": review}
        if review is not None:
            decisions = _safe(config.results, "source-notes", "visual-preparations", "decisions")
            decisions.mkdir(parents=True, exist_ok=True)
            atomic_write_json(_safe(decisions, f"{review['approval_id']}.json"), value)
        atomic_write_json(path, value)


def validate_published_review(review, attachment_digests):
    if review is None:
        return
    body = {key: value for key, value in review.items() if key != "approval_id"}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if digest != review["approval_id"] or sorted(attachment_digests) != sorted(
        item["image_sha256"] for item in review["approved"]
    ):
        raise ValueError("visual approval digest or adopted objects do not match")


def approval_record(evidence):
    value = {"review_mode": evidence["review_mode"], "approved": [
        {key: item[key] for key in ("candidate_id", "timestamp_ms", "image_sha256", "source_video_sha256", "adapter_id")}
        | {"image_name": Path(item["image"]).name} for item in evidence["approved"]],
        "no_useful_visuals_reason": evidence.get("no_useful_visuals_reason")}
    value["approval_id"] = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return value


def validate_approval(config, value):
    path = _path(config, value["source_id"], value["source_version"])
    if not path.is_file():
        from .authoritative_notes import _load_current
        note = _load_current(config)["notes"].get(value["source_id"], {})
        approved = next((entry["visual_review"] for entry in reversed(note.get("history", []))
                         if entry.get("source_version") == value["source_version"] and "visual_review" in entry), None)
        if approved is None:
            if "visual_review" in value:
                raise ValueError("visual approval preparation is missing")
            return
    else:
        approved = read_json(path)["visual_review"]
    if approved is None or value.get("visual_review") != approved:
        raise ValueError("visual approval does not match the recorded preparation; select evidence again")
    actual = []
    for raw in value["attachments"]:
        path = Path(raw).expanduser().resolve()
        actual.append((path.name, hashlib.sha256(path.read_bytes()).hexdigest()))
    expected = [(item["image_name"], item["image_sha256"]) for item in approved["approved"]]
    if sorted(actual) != sorted(expected):
        raise ValueError("visual approval attachments changed; select evidence again to record the new decision")
