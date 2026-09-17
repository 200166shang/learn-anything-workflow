"""Durable, one-way delivery of deterministic daily learning suggestions."""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator

from .command_response import engineering_revision, response
from .delivery_adapter import DELIVERY_ADAPTERS, DeliveryAdapterError
from .manifest import atomic_write_json, read_json
from .package_lock import package_lock
from .suggestions import today as suggestions_today
from .workspace import PORTABLE_SCHEMA_VERSION, WorkspaceConfig, WorkspaceError

SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schemas" /
                     "delivery-operation-v1.schema.json").read_text(encoding="utf-8"))


def _roots(config: WorkspaceConfig) -> tuple[Path, Path, Path]:
    if config.schema_version != PORTABLE_SCHEMA_VERSION or config.results is None or config.local is None:
        raise WorkspaceError("delivery requires workspace schema v2")
    return config.results / "delivery", config.results / "operation-receipts", config.local / "delivery"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _config_path(config: WorkspaceConfig) -> Path:
    return _roots(config)[2] / "config.json"


def _save(path: Path, record: dict[str, Any]) -> None:
    Draft202012Validator(SCHEMA).validate(record)
    atomic_write_json(path, record)


def enable(config: WorkspaceConfig, logical_target: str, adapter: str, authorization_ref: str,
           effective_from: str) -> dict[str, Any]:
    if not logical_target.strip() or not authorization_ref.strip():
        raise ValueError("logical target and authorization reference are required")
    effective = date.fromisoformat(effective_from)
    selected = DELIVERY_ADAPTERS.get(adapter)
    if selected is None:
        return response(status="missing_dependency", workspace=str(config.config_path),
                        diagnostics=["delivery adapter is unavailable"])
    try:
        readiness = selected.ready()
    except DeliveryAdapterError as exc:
        return response(status="missing_dependency", workspace=str(config.config_path),
                        validation={"adapter_readiness": "failed"}, diagnostics=[str(exc)])
    if not readiness.get("ready") or not readiness.get("auth_verified"):
        return response(status="missing_dependency", workspace=str(config.config_path),
                        validation={"adapter_readiness": "failed"}, diagnostics=["Lark adapter is not ready"])
    path = _config_path(config); path.parent.mkdir(parents=True, exist_ok=True)
    value = {"schema_version": 1, "enabled": True, "logical_target": logical_target,
             "adapter": adapter, "adapter_identity": selected.adapter_identity,
             "authorization_ref": authorization_ref, "effective_from": effective.isoformat(),
             "readiness": readiness, "updated_at": datetime.now(SHANGHAI).isoformat()}
    atomic_write_json(path, value)
    return response(status="completed", workspace=str(config.config_path), result=value,
                    validation={"explicit_authorization": "passed", "adapter_readiness": "passed"})


def disable(config: WorkspaceConfig, logical_target: str) -> dict[str, Any]:
    path = _config_path(config)
    if not path.is_file():
        return response(status="completed", workspace=str(config.config_path), result={"enabled": False})
    value = read_json(path)
    if value.get("logical_target") != logical_target:
        return response(status="missing_input", workspace=str(config.config_path),
                        diagnostics=["logical target does not match the enabled delivery"])
    value.update(enabled=False, updated_at=datetime.now(SHANGHAI).isoformat())
    atomic_write_json(path, value)
    return response(status="completed", workspace=str(config.config_path), result={"enabled": False,
                    "logical_target": logical_target})


def _message(suggestions: list[dict[str, Any]], marker: str, on_date: str) -> str:
    lines = [f"## 今日学习复习建议 · {on_date}", ""]
    for index, item in enumerate(suggestions, 1):
        lines.append(f'{index}. **{item["title"]}**（约 {item["estimated_minutes"]} 分钟）')
        lines.append(f'   - {item["reason"]}')
    lines.extend(["", "请回到 Codex 选择、改选或跳过；飞书只做单向提醒。", f"提醒标识：`{marker}`"])
    return "\n".join(lines)


def _public(config: WorkspaceConfig, record: dict[str, Any]) -> dict[str, Any]:
    return response(status=record["status"], workspace=str(config.config_path),
                    operation_id=record["operation_id"], result={
                        "intent": record["intent"], "receipt": record.get("receipt"),
                        "suggestion_count": record.get("suggestion_count", 0),
                        "content_digest": record.get("content_digest")},
                    validation=record.get("validation"), next_action=record.get("next_action"),
                    diagnostics=record.get("diagnostics"), artifact_refs=record.get("artifact_refs"))


def _verify_receipt(config: WorkspaceConfig, record: dict[str, Any]) -> bool:
    if record.get("status") != "completed":
        return True
    _, receipts, _ = _roots(config)
    path = receipts / f'{record["operation_id"]}.json'
    if not path.is_file():
        return False
    try:
        projected = read_json(path)
        return (projected.get("operation_id") == record["operation_id"]
                and projected.get("content_digest") == record.get("content_digest")
                and projected.get("receipt") == record.get("receipt"))
    except (OSError, ValueError, TypeError):
        return False


def tick(config: WorkspaceConfig, on_date: str | None = None, *,
         clock: Callable[[], datetime] | None = None) -> dict[str, Any]:
    moment = (clock or (lambda: datetime.now(SHANGHAI)))()
    if moment.tzinfo is None:
        raise ValueError("delivery clock must be timezone-aware")
    moment = moment.astimezone(SHANGHAI)
    day = date.fromisoformat(on_date) if on_date else moment.date()
    path = _config_path(config)
    if not path.is_file():
        return response(status="awaiting_user", workspace=str(config.config_path),
                        diagnostics=["delivery is not enabled"], next_action={"type": "user", "action": "delivery enable"})
    configuration = read_json(path)
    if not configuration.get("enabled") or day < date.fromisoformat(configuration["effective_from"]):
        return response(status="awaiting_user", workspace=str(config.config_path), diagnostics=["delivery is disabled or not effective"])
    if moment.date() != day or moment.timetz().replace(tzinfo=None) < time(9, 0):
        return response(status="completed", workspace=str(config.config_path), result={"sent": False, "reason": "before_09_00_or_not_today"})
    adapter = DELIVERY_ADAPTERS.get(configuration["adapter"])
    if adapter is None or adapter.adapter_identity != configuration["adapter_identity"]:
        return response(status="uncertain", workspace=str(config.config_path), diagnostics=["pinned delivery adapter is unavailable or changed"])
    try:
        readiness = adapter.ready()
    except DeliveryAdapterError as exc:
        return response(status="awaiting_user", workspace=str(config.config_path),
                        validation={"adapter_readiness": "failed"}, diagnostics=[str(exc)],
                        next_action={"type": "user", "action": "repair Lark readiness"})
    if (not readiness.get("ready") or not readiness.get("auth_verified")
            or readiness.get("target_fingerprint") != configuration.get("readiness", {}).get("target_fingerprint")):
        return response(status="awaiting_user", workspace=str(config.config_path),
                        validation={"adapter_readiness": "failed"},
                        diagnostics=["Lark identity, credentials, or physical recipient changed since authorization"])
    dedupe = {"workspace_id": config.workspace_id, "reminder_type": "daily_review",
              "date": day.isoformat(), "logical_target": configuration["logical_target"]}
    operation_id = "operation-delivery-" + _digest(dedupe)
    root, receipts, _ = _roots(config); operations = root / "operations"; operation_path = operations / f"{operation_id}.json"
    with package_lock(operation_path):
        if operation_path.is_file():
            existing = read_json(operation_path)
            if existing["status"] == "busy":
                existing.update(status="uncertain",
                                diagnostics=["previous delivery worker ended before confirmation; do not resend"],
                                next_action={"type": "reconcile", "operation_id": operation_id})
                _save(operation_path, existing); return _public(config, existing)
            if existing["status"] == "completed" and not _verify_receipt(config, existing):
                existing.update(status="uncertain", diagnostics=["delivery receipt projection is missing or invalid; do not resend"],
                                next_action={"type": "reconcile", "operation_id": operation_id})
                _save(operation_path, existing); return _public(config, existing)
            if existing["status"] in {"completed", "uncertain", "awaiting_user"}:
                return _public(config, existing)
    try:
        calculated = suggestions_today(config, day.isoformat())
    except Exception as exc:
        root, _, _ = _roots(config); failures = root / "failures"; failures.mkdir(parents=True, exist_ok=True)
        atomic_write_json(failures / f'{day.isoformat()}-{_digest(str(exc))[:12]}.json',
                          {"schema_version": 1, "date": day.isoformat(), "status": "failed", "diagnostic": str(exc)})
        return response(status="failed", workspace=str(config.config_path), diagnostics=[str(exc)])
    suggestions = calculated["result"]["suggestions"][:3]
    if not suggestions:
        return response(status="completed", workspace=str(config.config_path), result={"sent": False, "reason": "empty_suggestions"})
    with package_lock(operation_path):
        history: list[dict[str, Any]] = []
        if operation_path.is_file():
            existing = read_json(operation_path)
            if existing["status"] == "busy":
                existing.update(status="uncertain",
                                diagnostics=["previous delivery worker ended before confirmation; do not resend"],
                                next_action={"type": "reconcile", "operation_id": operation_id})
                _save(operation_path, existing)
                return _public(config, existing)
            if existing["status"] == "completed" and not _verify_receipt(config, existing):
                existing.update(status="uncertain", diagnostics=["delivery receipt projection is missing or invalid; do not resend"],
                                next_action={"type": "reconcile", "operation_id": operation_id})
                _save(operation_path, existing)
                return _public(config, existing)
            if existing["status"] in {"completed", "uncertain"}:
                return _public(config, existing)
            if existing["status"] != "recoverable_failure":
                return _public(config, existing)
            if (existing["intent"]["adapter_identity"] != configuration["adapter_identity"]
                    or existing["intent"]["authorization_ref"] != configuration["authorization_ref"]):
                existing.update(status="awaiting_user", diagnostics=["delivery configuration changed after the original intent; a new explicit authorization is required"],
                                next_action={"type": "user", "action": "delivery enable"})
                _save(operation_path, existing)
                return _public(config, existing)
            history = [*existing.get("history", []), {
                "final_status": existing["status"], "intent": existing["intent"],
                "attempt": existing["attempt"],
                "reconciliation": existing.get("reconciliation", []),
            }]
        marker = _digest({**dedupe, "authorization_ref": configuration["authorization_ref"]})[:40]
        message = _message(suggestions, marker, day.isoformat())
        input_fingerprint = _digest({"suggestions": suggestions, "date": day.isoformat(),
                                     "maximum_items": 3, "timezone": "Asia/Shanghai"})
        intent = {**dedupe, "capability_id": "delivery.lark", "capability_contract_version": 1,
                  "engine_revision": engineering_revision(), "record_schema": "delivery-operation-v1",
                  "adapter": configuration["adapter"],
                  "adapter_identity": configuration["adapter_identity"],
                  "authorization_ref": configuration["authorization_ref"],
                  "related_identifiers": [item["object_id"] for item in suggestions],
                  "suggestion_source_revisions": calculated["result"]["source_revisions"],
                  "effective_parameters_digest": input_fingerprint,
                  "recipient_fingerprint": readiness["target_fingerprint"],
                  "host_fingerprint": hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16],
                  "content_digest": hashlib.sha256(message.encode()).hexdigest()}
        record = {"schema_version": 1, "operation_id": operation_id, "status": "busy", "intent": intent,
                  "suggestion_count": len(suggestions), "content_digest": intent["content_digest"],
                  "attempt": {"idempotency_key": marker, "query_handle": marker,
                              "submitted_at": moment.isoformat()},
                  "validation": {"suggestions_recomputed": "passed", "authorization": "passed"},
                  "diagnostics": [], "next_action": {"type": "reconcile", "operation_id": operation_id}}
        if history:
            record["history"] = history
        operations.mkdir(parents=True, exist_ok=True); _save(operation_path, record)
        try:
            result = adapter.send(message, idempotency_key=marker)
        except DeliveryAdapterError as exc:
            record.update(status="uncertain" if exc.submitted else "recoverable_failure",
                          diagnostics=[str(exc)], next_action={"type": "reconcile" if exc.submitted else "retry",
                          "operation_id": operation_id})
            _save(operation_path, record); return _public(config, record)
        if (not isinstance(result.get("message_id"), str) or not result["message_id"]
                or result.get("sent_at") is None
                or result.get("target_fingerprint") != intent["recipient_fingerprint"]):
            record.update(status="uncertain", diagnostics=["transport returned no verifiable recipient-bound receipt"],
                          next_action={"type": "reconcile", "operation_id": operation_id})
            _save(operation_path, record); return _public(config, record)
        record["attempt"].update(query_handle=result.get("query_handle", marker))
        record.update(status="completed", receipt={"message_id": result.get("message_id"),
                      "sent_at": result.get("sent_at"), "target_fingerprint": result["target_fingerprint"],
                      "status": "sent"}, next_action=None)
        receipts.mkdir(parents=True, exist_ok=True); receipt_path = receipts / f"{operation_id}.json"
        record["artifact_refs"] = [str(receipt_path)]
        _save(receipt_path, record)
        _save(operation_path, record)
        return _public(config, record)


def status(config: WorkspaceConfig, operation_id: str | None = None) -> dict[str, Any]:
    root, _, _ = _roots(config)
    if operation_id:
        path = root / "operations" / f"{operation_id}.json"
        if not path.is_file():
            return response(status="missing_input", workspace=str(config.config_path), diagnostics=["unknown delivery operation"])
        record = read_json(path)
        if record["status"] == "completed" and not _verify_receipt(config, record):
            record.update(status="uncertain", diagnostics=["delivery receipt projection is missing or invalid"],
                          next_action={"type": "reconcile", "operation_id": operation_id})
            _save(path, record)
        return _public(config, record)
    configuration = read_json(_config_path(config)) if _config_path(config).is_file() else {"enabled": False}
    return response(status="completed", workspace=str(config.config_path), result={"configuration": configuration})


def reconcile(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    root, receipts, _ = _roots(config); path = root / "operations" / f"{operation_id}.json"
    if not path.is_file():
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=["unknown delivery operation"])
    with package_lock(path):
        record = read_json(path)
        if record["status"] == "completed" and _verify_receipt(config, record):
            return _public(config, record)
        if record["status"] == "completed":
            record.update(status="uncertain", diagnostics=["delivery receipt projection requires reconciliation"])
        adapter = DELIVERY_ADAPTERS.get(record["intent"]["adapter"])
        if adapter is None or adapter.adapter_identity != record["intent"]["adapter_identity"]:
            record.update(status="uncertain", diagnostics=["pinned adapter cannot query the original attempt"])
        else:
            try: checked = adapter.reconcile(record["attempt"])
            except Exception as exc: checked = {"state": "unknown", "diagnostic": str(exc)}
            record.setdefault("reconciliation", []).append({**checked, "queried_at": datetime.now(SHANGHAI).isoformat()})
            if (checked.get("state") == "available"
                    and isinstance(checked.get("message_id"), str) and checked["message_id"]
                    and checked.get("sent_at") is not None
                    and checked.get("target_fingerprint") == record["intent"]["recipient_fingerprint"]):
                record.update(status="completed", receipt={"message_id": checked.get("message_id"),
                              "sent_at": str(checked.get("sent_at")),
                              "target_fingerprint": checked["target_fingerprint"],
                              "status": "reconciled"}, diagnostics=[], next_action=None)
                receipts.mkdir(parents=True, exist_ok=True)
                receipt_path = receipts / f"{operation_id}.json"
                record["artifact_refs"] = [str(receipt_path)]
                _save(receipt_path, record)
            elif checked.get("state") == "not_submitted":
                record.update(status="recoverable_failure", diagnostics=[], next_action={"type": "retry", "operation_id": operation_id})
            else:
                record.update(status="uncertain", diagnostics=["remote outcome remains unknown; do not resend"],
                              next_action={"type": "reconcile", "operation_id": operation_id})
        _save(path, record); return _public(config, record)
