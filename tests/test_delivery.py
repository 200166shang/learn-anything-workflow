"""T22 one-way Lark delivery, dedupe, and uncertain recovery."""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from test_review_workflow import explained_question
from video_extract.delivery import disable, enable, reconcile, status, tick
from video_extract.delivery_adapter import DeliveryAdapterError, LarkCliAdapter, register_delivery_adapter
from video_extract.learning import record_feedback


def at(value: str):
    return lambda: datetime.fromisoformat(value)


class FakeDeliveryAdapter:
    adapter_identity = "fixture/lark-delivery-v1"

    def __init__(self, outcome: str = "completed") -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str]] = []
        self.reconcile_calls = 0
        self.operation_root: Path | None = None

    def ready(self):
        return {"ready": True, "auth_verified": True, "identity": "fixture",
                "target_fingerprint": "f" * 16}

    def send(self, message: str, *, idempotency_key: str):
        if self.operation_root is not None:
            intents = list(self.operation_root.glob("*.json"))
            assert len(intents) == 1
            assert json.loads(intents[0].read_text(encoding="utf-8"))["status"] == "busy"
        self.calls.append((message, idempotency_key))
        if self.outcome == "crash":
            raise KeyboardInterrupt("worker vanished after intent")
        if self.outcome == "incomplete":
            return {"message_id": None, "sent_at": None, "query_handle": idempotency_key}
        if self.outcome == "uncertain":
            raise DeliveryAdapterError("connection ended after submission", submitted=True)
        if self.outcome == "safe_failure":
            raise DeliveryAdapterError("not submitted", submitted=False)
        return {"message_id": "om_fixture", "sent_at": "2026-09-18T09:00:01+08:00",
                "query_handle": "om_fixture", "target_fingerprint": "f" * 16}

    def reconcile(self, attempt):
        self.reconcile_calls += 1
        if self.outcome == "recovered":
            return {"state": "available", "message_id": "om_recovered",
                    "sent_at": "2026-09-18T09:00:02+08:00", "target_fingerprint": "f" * 16}
        if self.outcome == "not_submitted":
            return {"state": "not_submitted"}
        return {"state": "unknown"}


def configured(tmp_path: Path, outcome: str = "completed"):
    config, _, question_id = explained_question(tmp_path)
    adapter = FakeDeliveryAdapter(outcome)
    register_delivery_adapter("fixture", adapter)
    result = enable(config, "personal-review", "fixture", "approval-t22", "2026-09-18")
    assert result["status"] == "completed"
    adapter.operation_root = config.results / "delivery/operations"
    return config, question_id, adapter


def test_tick_recomputes_persists_intent_sends_once_and_saves_sanitized_receipt(tmp_path: Path) -> None:
    config, question_id, adapter = configured(tmp_path)

    first = tick(config, "2026-09-18", clock=at("2026-09-18T09:00:00+08:00"))
    repeated = tick(config, "2026-09-18", clock=at("2026-09-18T10:00:00+08:00"))

    assert first["status"] == repeated["status"] == "completed"
    assert first["operation_id"] == repeated["operation_id"]
    assert len(adapter.calls) == 1
    assert "回到 Codex" in adapter.calls[0][0]
    assert first["result"]["intent"]["related_identifiers"] == [question_id]
    assert first["result"]["receipt"]["message_id"] == "om_fixture"
    receipt = config.results / "operation-receipts" / f'{first["operation_id"]}.json'
    assert receipt.is_file()
    serialized = receipt.read_text(encoding="utf-8")
    assert "VIDEO_EXTRACT" not in serialized and "approval-t22" in serialized


def test_before_nine_empty_and_disabled_are_quiet_without_adapter_calls(tmp_path: Path) -> None:
    config, question_id, adapter = configured(tmp_path)
    early = tick(config, "2026-09-18", clock=at("2026-09-18T08:59:59+08:00"))
    record_feedback(config, question_id, "understood", "已经理解")
    empty = tick(config, "2026-09-18", clock=at("2026-09-18T09:01:00+08:00"))
    disabled = disable(config, "personal-review")
    after_disable = tick(config, "2026-09-18", clock=at("2026-09-18T10:00:00+08:00"))

    assert early["result"]["reason"] == "before_09_00_or_not_today"
    assert empty["result"]["reason"] == "empty_suggestions"
    assert disabled["status"] == "completed" and after_disable["status"] == "awaiting_user"
    assert adapter.calls == []


def test_submitted_unknown_never_resends_and_reconcile_can_complete(tmp_path: Path) -> None:
    config, _, adapter = configured(tmp_path, "uncertain")
    uncertain = tick(config, "2026-09-18", clock=at("2026-09-18T09:00:00+08:00"))
    repeated = tick(config, "2026-09-18", clock=at("2026-09-18T09:05:00+08:00"))
    assert uncertain["status"] == repeated["status"] == "uncertain"
    assert len(adapter.calls) == 1

    adapter.outcome = "recovered"
    recovered = reconcile(config, uncertain["operation_id"])
    assert recovered["status"] == "completed"
    assert recovered["result"]["receipt"]["message_id"] == "om_recovered"
    assert len(adapter.calls) == 1 and adapter.reconcile_calls == 1


def test_not_found_is_not_treated_as_not_sent_but_explicit_not_submitted_is_retryable(tmp_path: Path) -> None:
    config, _, adapter = configured(tmp_path, "uncertain")
    uncertain = tick(config, "2026-09-18", clock=at("2026-09-18T09:00:00+08:00"))
    still_unknown = reconcile(config, uncertain["operation_id"])
    assert still_unknown["status"] == "uncertain"
    assert still_unknown["next_action"]["type"] == "reconcile"

    adapter.outcome = "not_submitted"
    retryable = reconcile(config, uncertain["operation_id"])
    assert retryable["status"] == "recoverable_failure"
    assert retryable["next_action"]["type"] == "retry"

    adapter.outcome = "completed"
    completed = tick(config, "2026-09-18", clock=at("2026-09-18T09:10:00+08:00"))
    authority = json.loads((config.results / "delivery/operations" /
                            f'{completed["operation_id"]}.json').read_text(encoding="utf-8"))
    assert completed["status"] == "completed" and len(adapter.calls) == 2
    assert authority["history"][0]["reconciliation"][-1]["state"] == "not_submitted"


def test_corrupt_suggestion_authority_records_failure_fact_not_empty(tmp_path: Path) -> None:
    config, _, adapter = configured(tmp_path)
    pointer = config.results / "learning/current.json"
    value = json.loads(pointer.read_text(encoding="utf-8")); value["manifest_sha256"] = "0" * 64
    pointer.write_text(json.dumps(value), encoding="utf-8")

    failed = tick(config, "2026-09-18", clock=at("2026-09-18T09:00:00+08:00"))

    assert failed["status"] == "failed" and adapter.calls == []
    assert list((config.results / "delivery/failures").glob("*.json"))


def test_status_does_not_expose_a_physical_recipient(tmp_path: Path) -> None:
    config, _, _ = configured(tmp_path)
    shown = status(config)
    assert shown["result"]["configuration"]["logical_target"] == "personal-review"
    assert "recipient_id" not in json.dumps(shown, ensure_ascii=False)


def test_changed_recipient_fingerprint_requires_new_authorization(tmp_path: Path) -> None:
    config, _, adapter = configured(tmp_path)
    adapter.ready = lambda: {"ready": True, "auth_verified": True, "identity": "fixture",
                             "target_fingerprint": "different-target"}

    blocked = tick(config, "2026-09-18", clock=at("2026-09-18T09:00:00+08:00"))

    assert blocked["status"] == "awaiting_user"
    assert blocked["validation"]["adapter_readiness"] == "failed"
    assert adapter.calls == []


def test_incomplete_transport_receipt_stays_uncertain(tmp_path: Path) -> None:
    config, _, adapter = configured(tmp_path, "incomplete")

    result = tick(config, "2026-09-18", clock=at("2026-09-18T09:00:00+08:00"))

    assert result["status"] == "uncertain"
    assert result["result"]["receipt"] is None
    assert result["next_action"]["type"] == "reconcile"
    assert len(adapter.calls) == 1


def test_crashed_busy_intent_becomes_uncertain_without_resubmission(tmp_path: Path) -> None:
    config, _, adapter = configured(tmp_path, "crash")
    try:
        tick(config, "2026-09-18", clock=at("2026-09-18T09:00:00+08:00"))
    except KeyboardInterrupt:
        pass

    recovered = tick(config, "2026-09-18", clock=at("2026-09-18T09:01:00+08:00"))

    assert recovered["status"] == "uncertain"
    assert recovered["next_action"]["type"] == "reconcile"
    assert len(adapter.calls) == 1


def test_existing_uncertain_operation_is_not_hidden_by_changed_suggestions(tmp_path: Path) -> None:
    config, question_id, adapter = configured(tmp_path, "uncertain")
    first = tick(config, "2026-09-18", clock=at("2026-09-18T09:00:00+08:00"))
    record_feedback(config, question_id, "understood", "发送后已完成学习")

    repeated = tick(config, "2026-09-18", clock=at("2026-09-18T09:05:00+08:00"))

    assert repeated["operation_id"] == first["operation_id"]
    assert repeated["status"] == "uncertain" and len(adapter.calls) == 1


def test_lark_readiness_parses_auth_status_root_contract(monkeypatch) -> None:
    monkeypatch.setenv("VIDEO_EXTRACT_LARK_CHAT_ID", "oc_fixture")
    monkeypatch.delenv("VIDEO_EXTRACT_LARK_USER_ID", raising=False)
    monkeypatch.setattr("video_extract.delivery_adapter.subprocess.run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"identity": "user", "verified": True,
                           "identities": {"user": {"status": "authenticated", "tokenStatus": "valid"}}}),
        stderr="",
    ))

    readiness = LarkCliAdapter().ready()

    assert readiness["ready"] is True and readiness["auth_verified"] is True
    assert readiness["identity"] == "user" and readiness["target_type"] == "chat-id"


def test_lark_adapter_rejects_receipt_without_exact_chat_identity(monkeypatch) -> None:
    monkeypatch.setenv("VIDEO_EXTRACT_LARK_CHAT_ID", "oc_fixture")
    monkeypatch.setattr("video_extract.delivery_adapter.subprocess.run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"ok": True, "data": {"message_id": "om_fixture", "create_time": "1"}}),
        stderr="",
    ))

    try:
        LarkCliAdapter().send("hello", idempotency_key="marker")
        assert False, "missing chat identity must not complete"
    except DeliveryAdapterError as exc:
        assert exc.submitted is True


def test_missing_lark_cli_is_a_pre_submit_readiness_error(monkeypatch) -> None:
    monkeypatch.setenv("VIDEO_EXTRACT_LARK_CHAT_ID", "oc_fixture")
    def missing(*args, **kwargs):
        raise FileNotFoundError("lark-cli")
    monkeypatch.setattr("video_extract.delivery_adapter.subprocess.run", missing)

    try:
        LarkCliAdapter().ready()
        assert False, "missing executable must fail readiness"
    except DeliveryAdapterError as exc:
        assert exc.submitted is False


def test_missing_completed_receipt_is_uncertain_and_reconciled_without_resend(tmp_path: Path) -> None:
    config, _, adapter = configured(tmp_path)
    completed = tick(config, "2026-09-18", clock=at("2026-09-18T09:00:00+08:00"))
    receipt = config.results / "operation-receipts" / f'{completed["operation_id"]}.json'
    receipt.unlink()

    shown = status(config, completed["operation_id"])
    assert shown["status"] == "uncertain"
    adapter.outcome = "recovered"
    repaired = reconcile(config, completed["operation_id"])

    assert repaired["status"] == "completed" and receipt.is_file()
    assert len(adapter.calls) == 1
