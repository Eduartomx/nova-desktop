from __future__ import annotations

"""Small, unauthenticated and read-only GitHub client for Nova's own repo."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class GitHubReadError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = str(code)
        self.detail = str(detail or code)[:500]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GitHubReadClient:
    def __init__(
        self,
        repository: str,
        *,
        api_base: str = "https://api.github.com",
        timeout: float = 8.0,
        max_bytes: int = 512_000,
        cache_path: Path | None = None,
        opener=None,
    ):
        repo = str(repository or "").strip()
        parts = repo.split("/")
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or len(parts) != 2
            or any(part in {".", ".."} or part.startswith((".", "-")) for part in parts)
        ):
            raise ValueError("invalid_repository")
        if str(api_base or "").rstrip("/").casefold() != "https://api.github.com":
            raise ValueError("invalid_github_api")
        self.repository = repo
        self.api_base = "https://api.github.com"
        self.timeout = max(0.2, min(float(timeout), 30.0))
        self.max_bytes = max(1024, min(int(max_bytes), 2_000_000))
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self._opener = opener or urlopen
        self._cache = self._load_cache()

    @classmethod
    def from_config(cls, config_path: Path, *, cache_path: Path | None = None, opener=None):
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        return cls(
            str(data.get("repository") or ""),
            api_base=str(data.get("github_api") or "https://api.github.com"),
            cache_path=cache_path,
            opener=opener,
        )

    def _load_cache(self) -> dict[str, Any]:
        if self.cache_path is None or not self.cache_path.is_file():
            return {"schema": 1, "entries": {}}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                return data
        except Exception:
            pass
        return {"schema": 1, "entries": {}}

    def _save_cache(self) -> None:
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        entries = self._cache.get("entries", {})
        # Keep the informational cache bounded and deterministic.
        if len(entries) > 40:
            ordered = sorted(entries.items(), key=lambda item: str(item[1].get("updated_at") or ""), reverse=True)[:40]
            self._cache["entries"] = dict(ordered)
        payload = json.dumps(self._cache, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > 1_500_000:
            self._cache = {"schema": 1, "entries": {}}
            payload = json.dumps(self._cache, separators=(",", ":"))
        fd, tmp_name = tempfile.mkstemp(prefix=self.cache_path.name + ".", suffix=".tmp", dir=str(self.cache_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.cache_path)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass

    def _request_json(self, endpoint: str) -> dict[str, Any]:
        if not endpoint.startswith("/") or "//" in endpoint or ".." in endpoint:
            raise GitHubReadError("invalid_endpoint")
        url = self.api_base + endpoint
        cached = self._cache.get("entries", {}).get(endpoint, {})
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Nova-Desktop/0.10 Repository-Intelligence",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if cached.get("etag"):
            headers["If-None-Match"] = str(cached["etag"])
        request = Request(url, headers=headers, method="GET")
        try:
            response = self._opener(request, timeout=self.timeout)
            length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
            try:
                if length and int(length) > self.max_bytes:
                    raise GitHubReadError("response_too_large")
            except ValueError:
                pass
            body = response.read(self.max_bytes + 1)
            if len(body) > self.max_bytes:
                raise GitHubReadError("response_too_large")
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception as exc:
                raise GitHubReadError("invalid_json", type(exc).__name__) from exc
            etag = response.headers.get("ETag", "") if getattr(response, "headers", None) else ""
            self._cache.setdefault("entries", {})[endpoint] = {
                "etag": str(etag), "updated_at": _utc_now(), "data": data,
            }
            self._save_cache()
            return {"ok": True, "data": data, "source": "github", "updated_at": _utc_now(), "etag": str(etag)}
        except HTTPError as exc:
            if exc.code == 304 and "data" in cached:
                return {"ok": True, "data": cached["data"], "source": "cache", "updated_at": cached.get("updated_at"), "etag": cached.get("etag", "")}
            mapping = {403: "rate_limited", 404: "not_found", 429: "rate_limited"}
            if exc.code in {403, 429} and "data" in cached:
                return {"ok": True, "data": cached["data"], "source": "cache", "updated_at": cached.get("updated_at"), "offline": True, "remote_error": mapping[exc.code]}
            raise GitHubReadError(mapping.get(exc.code, "http_error"), str(exc.code)) from exc
        except (TimeoutError, URLError) as exc:
            if "data" in cached:
                return {"ok": True, "data": cached["data"], "source": "cache", "updated_at": cached.get("updated_at"), "offline": True}
            reason = getattr(exc, "reason", exc)
            code = "timeout" if isinstance(reason, TimeoutError) or "timed out" in str(reason).casefold() else "network_unavailable"
            raise GitHubReadError(code, type(exc).__name__) from exc

    def latest_release(self) -> dict[str, Any]:
        return self._request_json(f"/repos/{self.repository}/releases/latest")

    def recent_commits(self, limit: int = 8) -> dict[str, Any]:
        limit = max(1, min(int(limit), 20))
        return self._request_json(f"/repos/{self.repository}/commits?sha=main&per_page={limit}")

    def repository_file(self, path: str, ref: str = "main") -> dict[str, Any]:
        raw_path = str(path or "").replace("\\", "/").strip("/")
        pure = PurePosixPath(raw_path)
        if not raw_path or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise GitHubReadError("invalid_path")
        selected_ref = str(ref or "main").strip()
        if not (
            selected_ref == "main"
            or selected_ref == "latest"
            or re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?", selected_ref)
            or re.fullmatch(r"[0-9a-fA-F]{40}", selected_ref)
        ):
            raise GitHubReadError("invalid_ref")
        if selected_ref == "latest":
            release = self.latest_release()
            selected_ref = str(release["data"].get("tag_name") or "")
            if not selected_ref:
                raise GitHubReadError("latest_release_missing")
        endpoint = f"/repos/{self.repository}/contents/{quote(raw_path, safe='/')}?ref={quote(selected_ref, safe='') }"
        result = self._request_json(endpoint)
        data = result.get("data")
        if not isinstance(data, dict) or data.get("type") != "file":
            raise GitHubReadError("not_a_file")
        size = int(data.get("size") or 0)
        if size > self.max_bytes:
            raise GitHubReadError("response_too_large")
        import base64
        try:
            content = base64.b64decode(str(data.get("content") or ""), validate=False)
            if len(content) > self.max_bytes:
                raise GitHubReadError("response_too_large")
            text = content.decode("utf-8", errors="replace")
        except GitHubReadError:
            raise
        except Exception as exc:
            raise GitHubReadError("invalid_content", type(exc).__name__) from exc
        return {
            "ok": True, "path": raw_path, "ref": selected_ref, "content": text,
            "sha": str(data.get("sha") or ""), "size": len(content),
            "source": result.get("source"), "updated_at": result.get("updated_at"),
        }
