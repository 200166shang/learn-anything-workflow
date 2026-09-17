"""NetEase remote boundary; contains no operation persistence or receipt policy."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Protocol


class NetEaseAdapter(Protocol):
    adapter_identity: str
    def list_page(self, cursor: str | None, limit: int) -> dict[str, Any]: ...
    def upload(self, path: Path, *, idempotency_token: str) -> dict[str, Any]: ...
    def status(self, query_handle: str) -> dict[str, Any]: ...
    def reconcile(self, attempt: dict[str, Any]) -> dict[str, Any]: ...


class NcmCliAdapter:
    adapter_identity = "ncm-cli/cloudupload-v1"

    @staticmethod
    def _check_help(command: list[str], required: tuple[str, ...]) -> None:
        completed = subprocess.run([*command, "--help"], text=True, capture_output=True)
        if completed.returncode or any(flag not in completed.stdout for flag in required):
            raise RuntimeError("installed ncm-cli public command contract is incompatible")

    @staticmethod
    def _json(command: list[str]) -> dict[str, Any]:
        completed = subprocess.run(command, text=True, capture_output=True)
        if completed.returncode:
            raise RuntimeError("ncm-cli request failed; inspect login and service state")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("ncm-cli returned an unreadable public response") from exc
        if not isinstance(value, dict):
            raise RuntimeError("ncm-cli returned an incompatible public response")
        return value

    def list_page(self, cursor: str | None, limit: int) -> dict[str, Any]:
        self._check_help(["ncm-cli", "cloud", "list"], ("--cursor", "--limit", "--output"))
        command = ["ncm-cli", "cloud", "list", "--limit", str(limit), "--output", "json"]
        if cursor:
            command += ["--cursor", cursor]
        return self._json(command)

    def upload(self, path: Path, *, idempotency_token: str) -> dict[str, Any]:
        self._check_help(["ncm-cli", "cloudupload", "upload"], ("--background",))
        completed = subprocess.run(["ncm-cli", "cloudupload", "upload", "--background", str(path)],
                                   text=True, capture_output=True)
        if completed.returncode:
            raise RuntimeError("ncm-cli upload outcome is unknown; reconcile before any retry")
        match = re.search(r"[A-Za-z0-9_-]{4,}", completed.stdout)
        return {"state": "submitted", "query_handle": match.group(0) if match else idempotency_token}

    def status(self, query_handle: str) -> dict[str, Any]:
        self._check_help(["ncm-cli", "cloudupload", "status"], ("taskId",))
        raw = self._json(["ncm-cli", "cloudupload", "status", query_handle])
        value = raw.get("status") or raw.get("state")
        if isinstance(raw.get("data"), dict):
            value = value or raw["data"].get("status") or raw["data"].get("state")
        normalized = str(value or "unknown").casefold()
        mapping = {"success": "completed", "succeeded": "completed", "done": "completed",
                   "completed": "completed", "running": "running", "pending": "running",
                   "queued": "running", "failed": "failed", "error": "failed"}
        return {"state": mapping.get(normalized, "unknown"), "query_handle": query_handle}

    def reconcile(self, attempt: dict[str, Any]) -> dict[str, Any]:
        return self.status(str(attempt["query_handle"]))


NETEASE_ADAPTERS: dict[str, NetEaseAdapter] = {"ncm-cli": NcmCliAdapter()}


def register_netease_adapter(name: str, adapter: NetEaseAdapter) -> None:
    NETEASE_ADAPTERS[name] = adapter
