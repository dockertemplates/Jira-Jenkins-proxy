"""
Persistent JSON store for JQL, Jenkins webhook URL, poll interval, and
deduplication keys (processed Jira issue keys with timestamps).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class AppConfig:
    jql: str
    jenkins_webhook_url: str
    poll_interval_seconds: int


class JsonStore:
    """Thread-safe JSON file with atomic replace on write."""

    def __init__(self, path: Union[str, Path]) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def _default_payload(
        self,
        jql: str,
        jenkins_url: str,
        poll_interval: int,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "config": {
                "jql": jql,
                "jenkins_webhook_url": jenkins_url,
                "poll_interval_seconds": max(5, int(poll_interval)),
            },
            "processed_issues": {},
        }

    def ensure_exists(
        self,
        default_jql: str,
        default_jenkins_url: str,
        default_poll_interval: int,
    ) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.exists():
                return
            data = self._default_payload(
                default_jql, default_jenkins_url, default_poll_interval
            )
            self._atomic_write_unlocked(data)

    def _read_unlocked(self) -> dict[str, Any]:
        with self._path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _atomic_write_unlocked(self, data: dict[str, Any]) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, self._path)

    def read_all(self) -> dict[str, Any]:
        with self._lock:
            return self._read_unlocked()

    def get_config(self) -> AppConfig:
        with self._lock:
            raw = self._read_unlocked()
            c = raw.get("config") or {}
            return AppConfig(
                jql=str(c.get("jql", "")),
                jenkins_webhook_url=str(c.get("jenkins_webhook_url", "")),
                poll_interval_seconds=max(
                    5, int(c.get("poll_interval_seconds", 30))
                ),
            )

    def update_config(
        self,
        *,
        jql: Optional[str] = None,
        jenkins_webhook_url: Optional[str] = None,
        poll_interval_seconds: Optional[int] = None,
    ) -> AppConfig:
        with self._lock:
            data = self._read_unlocked()
            cfg = data.setdefault("config", {})
            if jql is not None:
                cfg["jql"] = jql.strip()
            if jenkins_webhook_url is not None:
                cfg["jenkins_webhook_url"] = jenkins_webhook_url.strip()
            if poll_interval_seconds is not None:
                cfg["poll_interval_seconds"] = max(5, int(poll_interval_seconds))
            self._atomic_write_unlocked(data)
            return self.get_config()

    def is_processed(self, issue_key: str) -> bool:
        with self._lock:
            data = self._read_unlocked()
            return issue_key in (data.get("processed_issues") or {})

    def mark_processed(self, issue_key: str) -> None:
        with self._lock:
            data = self._read_unlocked()
            issues = data.setdefault("processed_issues", {})
            issues[issue_key] = _utc_now_iso()
            self._atomic_write_unlocked(data)

    def list_processed(self) -> list[dict[str, str]]:
        with self._lock:
            data = self._read_unlocked()
            items = data.get("processed_issues") or {}
            out = [
                {"issue_key": k, "processed_at": v}
                for k, v in sorted(items.items(), key=lambda x: x[1], reverse=True)
            ]
            return out

    def delete_processed(self, issue_key: str) -> bool:
        with self._lock:
            data = self._read_unlocked()
            issues = data.setdefault("processed_issues", {})
            if issue_key not in issues:
                return False
            del issues[issue_key]
            self._atomic_write_unlocked(data)
            return True
