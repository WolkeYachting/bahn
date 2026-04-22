"""Storage via GitHub Contents API, local cache as fallback.

On Render, the filesystem is ephemeral; data files are committed to the GitHub
repo via the Contents API. Without GITHUB_TOKEN/GITHUB_REPO, runs locally
only — convenient for tests and development.
"""

import base64
import os
import threading
import time
from pathlib import Path

import requests

BASE = Path(__file__).parent
API = "https://api.github.com"


class Storage:
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.token = os.environ.get("GITHUB_TOKEN")
        self.repo = os.environ.get("GITHUB_REPO")
        self.branch = os.environ.get("GITHUB_BRANCH", "main")
        self.author = os.environ.get("GITHUB_AUTHOR", "bahn-pb-bot")
        self.email = os.environ.get("GITHUB_EMAIL", "bot@users.noreply.github.com")
        self.local_base = BASE

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.repo)

    def read(self, filename: str) -> str | None:
        local = self.local_base / filename
        if local.exists():
            return local.read_text(encoding="utf-8")
        if self.enabled:
            try:
                content, _ = self._gh_read(filename)
                if content is not None:
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_text(content, encoding="utf-8")
                    return content
            except Exception as e:
                print(f"[storage] GitHub read failed for {filename}: {e}")
        return None

    def write(self, filename: str, content: str, commit_message: str | None = None) -> None:
        local = self.local_base / filename
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(content, encoding="utf-8")
        if self.enabled:
            try:
                self._gh_put(filename, content.encode("utf-8"),
                             commit_message or f"chore: update {filename}")
            except Exception as e:
                print(f"[storage] GitHub write failed for {filename}: {e}")

    def write_binary(self, filename: str, data: bytes,
                     commit_message: str | None = None) -> None:
        local = self.local_base / filename
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        if self.enabled:
            try:
                self._gh_put(filename, data,
                             commit_message or f"chore: update {filename}")
            except Exception as e:
                print(f"[storage] GitHub write_binary failed for {filename}: {e}")

    def hydrate(self, filename: str) -> None:
        """Pull file from GitHub if not in local cache."""
        if not self.enabled:
            return
        local = self.local_base / filename
        if local.exists():
            return
        with self._lock:
            try:
                content, _ = self._gh_read(filename)
                if content is not None:
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_text(content, encoding="utf-8")
            except Exception as e:
                print(f"[storage] hydrate failed for {filename}: {e}")

    # ---- internals ----

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "bahn-pb/1.0",
        }

    def _gh_read(self, filename: str) -> tuple[str | None, str | None]:
        url = f"{API}/repos/{self.repo}/contents/{filename}"
        r = requests.get(url, headers=self._headers(),
                         params={"ref": self.branch}, timeout=30)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data.get("sha")

    def _gh_put(self, filename: str, data: bytes, message: str) -> None:
        for attempt in range(2):
            _, sha = self._gh_read(filename)
            body = {
                "message": message,
                "content": base64.b64encode(data).decode("ascii"),
                "branch": self.branch,
                "committer": {"name": self.author, "email": self.email},
            }
            if sha:
                body["sha"] = sha
            url = f"{API}/repos/{self.repo}/contents/{filename}"
            r = requests.put(url, headers=self._headers(), json=body, timeout=30)
            if r.status_code == 409 and attempt == 0:
                time.sleep(0.5)
                continue
            r.raise_for_status()
            return
