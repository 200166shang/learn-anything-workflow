"""Outbound reminder transport boundary; contains no scheduling or persistence policy."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Protocol


class DeliveryAdapterError(RuntimeError):
    def __init__(self, message: str, *, submitted: bool) -> None:
        super().__init__(message)
        self.submitted = submitted


class DeliveryAdapter(Protocol):
    adapter_identity: str
    def ready(self) -> dict[str, Any]: ...
    def send(self, message: str, *, idempotency_key: str) -> dict[str, Any]: ...
    def reconcile(self, attempt: dict[str, Any]) -> dict[str, Any]: ...


class LarkCliAdapter:
    """User-identity lark-cli adapter with a searchable non-secret marker."""

    adapter_identity = "lark-cli/im-user-v1"

    @staticmethod
    def _run(arguments: list[str]) -> dict[str, Any]:
        environment = {**os.environ, "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                       "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"}
        try:
            completed = subprocess.run(["lark-cli", *arguments], capture_output=True, text=True,
                                       env=environment, timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise DeliveryAdapterError("lark-cli request timed out",
                                       submitted=arguments[:2] == ["im", "+messages-send"]) from exc
        except OSError as exc:
            raise DeliveryAdapterError("lark-cli is unavailable", submitted=False) from exc
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        try:
            value = json.loads(stream)
        except json.JSONDecodeError as exc:
            raise DeliveryAdapterError("lark-cli returned an unreadable response",
                                       submitted=arguments[:2] == ["im", "+messages-send"]) from exc
        if completed.returncode or value.get("ok") is not True:
            error = value.get("error", {})
            message = error.get("message", "lark-cli request failed")
            submitted = (arguments[:2] == ["im", "+messages-send"]
                         and error.get("type") not in {"authentication", "authorization", "validation",
                                                       "confirmation", "configuration", "dependency"})
            raise DeliveryAdapterError(message, submitted=submitted)
        return value

    @staticmethod
    def _auth_status() -> dict[str, Any]:
        environment = {**os.environ, "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                       "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"}
        try:
            completed = subprocess.run(["lark-cli", "auth", "status", "--json", "--verify"],
                                       capture_output=True, text=True, env=environment, timeout=15)
        except subprocess.TimeoutExpired as exc:
            raise DeliveryAdapterError("lark-cli auth readiness check timed out", submitted=False) from exc
        except OSError as exc:
            raise DeliveryAdapterError("lark-cli is unavailable", submitted=False) from exc
        try:
            value = json.loads(completed.stdout if completed.returncode == 0 else completed.stderr)
        except json.JSONDecodeError as exc:
            raise DeliveryAdapterError("lark-cli auth status was unreadable", submitted=False) from exc
        if completed.returncode:
            raise DeliveryAdapterError(value.get("error", {}).get("message", "lark-cli auth status failed"),
                                       submitted=False)
        return value

    @staticmethod
    def _target() -> tuple[str, str]:
        chat_id = os.environ.get("VIDEO_EXTRACT_LARK_CHAT_ID")
        if not chat_id:
            raise DeliveryAdapterError("set the local VIDEO_EXTRACT_LARK_CHAT_ID recipient", submitted=False)
        return "--chat-id", chat_id

    def ready(self) -> dict[str, Any]:
        target_flag, target = self._target()
        status = self._auth_status()
        user = status.get("identities", {}).get("user", {})
        verified = status.get("verified") is True or (user.get("status") == "authenticated"
                                                       and user.get("tokenStatus") == "valid")
        return {"ready": True, "identity": "user", "target_type": target_flag.removeprefix("--"),
                "target_fingerprint": __import__("hashlib").sha256(target.encode()).hexdigest()[:16],
                "auth_verified": verified}

    def send(self, message: str, *, idempotency_key: str) -> dict[str, Any]:
        target_flag, target = self._target()
        value = self._run(["im", "+messages-send", target_flag, target, "--markdown", message,
                           "--idempotency-key", idempotency_key, "--as", "user"])
        data = value.get("data", value)
        message_id, sent_at = data.get("message_id"), data.get("create_time")
        if (not isinstance(message_id, str) or not message_id.startswith("om_") or sent_at is None
                or data.get("chat_id") != target):
            raise DeliveryAdapterError("Lark accepted the request but returned no verifiable receipt", submitted=True)
        fingerprint = __import__("hashlib").sha256(target.encode()).hexdigest()[:16]
        return {"message_id": message_id, "sent_at": str(sent_at), "query_handle": message_id,
                "target_fingerprint": fingerprint}

    def reconcile(self, attempt: dict[str, Any]) -> dict[str, Any]:
        marker = str(attempt["idempotency_key"])
        _, target = self._target()
        value = self._run(["im", "+messages-search", "--query", marker, "--chat-id", target,
                           "--page-all", "--as", "user"])
        data = value.get("data", {})
        messages = data.get("messages") or data.get("items") or []
        def same_target(item: dict[str, Any]) -> bool:
            chat = item.get("chat")
            nested = chat.get("chat_id") if isinstance(chat, dict) else None
            return item.get("chat_id") == target or nested == target
        matches = [item for item in messages if marker in json.dumps(item, ensure_ascii=False)
                   and same_target(item)]
        if len(matches) == 1:
            return {"state": "available", "message_id": matches[0].get("message_id"),
                    "sent_at": matches[0].get("create_time"),
                    "target_fingerprint": __import__("hashlib").sha256(target.encode()).hexdigest()[:16]}
        return {"state": "unknown", "matches": len(matches)}


DELIVERY_ADAPTERS: dict[str, DeliveryAdapter] = {"lark-cli": LarkCliAdapter()}


def register_delivery_adapter(name: str, adapter: DeliveryAdapter) -> None:
    DELIVERY_ADAPTERS[name] = adapter
