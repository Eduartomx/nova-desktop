from __future__ import annotations

"""Version, changelog and public repository awareness for Nova itself."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from updater.github_read_client import GitHubReadClient, GitHubReadError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _version_key(value: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", str(value or ""))
    return tuple(int(x) for x in nums[:4]) or (0,)


class _UnavailableClient:
    def __init__(self, code="configuration_unavailable"):
        self.code = str(code)
    def _raise(self, *_args, **_kwargs):
        raise GitHubReadError(self.code)
    latest_release = _raise
    recent_commits = _raise
    repository_file = _raise


class RepositoryIntelligence:
    def __init__(self, config=None, *, project_root: Path | None = None, client=None):
        self.config = config or {}
        self.project_root = Path(project_root) if project_root is not None else Path(__file__).resolve().parent.parent
        self.repo_root = self.project_root.parent
        self.update_config_path = self.project_root / "updater" / "update_config.json"
        self.cache_path = self.project_root / "data" / "repository_public_cache.json"
        if client is not None:
            self.client = client
        else:
            try:
                self.client = GitHubReadClient.from_config(self.update_config_path, cache_path=self.cache_path)
            except Exception as exc:
                self.client = _UnavailableClient("configuration_" + type(exc).__name__.casefold())

    def local_versions(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for label, path in (
            ("VERSION", self.repo_root / "VERSION"),
            ("NOVA_VERSION.txt", self.project_root / "NOVA_VERSION.txt"),
        ):
            try:
                values[label] = path.read_text(encoding="utf-8").strip()
            except OSError:
                values[label] = ""
        try:
            init = (self.project_root / "assistant" / "__init__.py").read_text(encoding="utf-8")
            match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", init)
            values["assistant.__version__"] = match.group(1) if match else ""
        except OSError:
            values["assistant.__version__"] = ""
        return values

    def _local_changelog(self) -> str:
        try:
            return (self.repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
        except OSError:
            return ""

    @staticmethod
    def _section(text: str, version: str = "") -> tuple[str, str]:
        headings = list(re.finditer(r"(?m)^##\s+v?([^\s—-]+).*?$", str(text or "")))
        if not headings:
            return "", ""
        selected = None
        wanted = str(version or "").lstrip("vV")
        for match in headings:
            if not wanted or match.group(1).lstrip("vV") == wanted:
                selected = match
                break
        if selected is None:
            return "", ""
        index = headings.index(selected)
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        return selected.group(1).lstrip("vV"), text[selected.start():end].strip()

    def version_status(self, *, refresh=True) -> dict[str, Any]:
        versions = self.local_versions()
        current = next((value for value in versions.values() if value), "")
        last = _read_json(self.project_root / "data" / "update_last.json")
        result: dict[str, Any] = {
            "ok": bool(current), "current": current, "local_versions": versions,
            "consistent": len({x for x in versions.values() if x}) <= 1,
            "update_last": {
                "ok": bool(last.get("ok")), "before": str(last.get("before") or ""),
                "after": str(last.get("after") or ""), "state": str(last.get("state") or ""),
            } if last else None,
            "source": "archivos de versión locales", "updated_at": _now(), "offline": False,
        }
        if refresh:
            try:
                remote = self.client.latest_release()
                release = remote.get("data") if isinstance(remote.get("data"), dict) else {}
                latest = str(release.get("tag_name") or "").lstrip("vV")
                result.update({
                    "latest": latest,
                    "update_available": bool(latest and _version_key(latest) > _version_key(current)),
                    "release_url": str(release.get("html_url") or ""),
                    "release_name": str(release.get("name") or ""),
                    "source": "release " + ("v" + latest if latest else "pública"),
                    "updated_at": remote.get("updated_at") or _now(),
                    "offline": bool(remote.get("offline")),
                })
            except GitHubReadError as exc:
                result.update({"latest": "", "update_available": None, "offline": True, "remote_error": exc.code, "source": "GitHub no disponible; archivos de versión locales"})
        return result

    def whats_new(self, *, version="", refresh=False) -> dict[str, Any]:
        local = self._local_changelog()
        selected_version, section = self._section(local, version)
        source = "CHANGELOG.md local"
        offline = False
        if not section and refresh:
            try:
                remote = self.client.repository_file("CHANGELOG.md", "main")
                selected_version, section = self._section(remote.get("content", ""), version)
                source = "CHANGELOG.md remoto (" + str(remote.get("source") or "github") + ")"
                offline = remote.get("source") == "cache"
            except GitHubReadError:
                offline = True
        last = _read_json(self.project_root / "data" / "update_last.json")
        update_range = None
        if last.get("before") or last.get("after"):
            update_range = {"before": str(last.get("before") or ""), "after": str(last.get("after") or "")}
        return {
            "ok": bool(section), "version": selected_version, "changes": section[:60_000],
            "source": source if section else "GitHub no disponible", "updated_at": _now(),
            "offline": offline, "update_range": update_range,
        }

    def activity(self, *, limit=8) -> dict[str, Any]:
        try:
            remote = self.client.recent_commits(limit)
            rows = []
            for item in remote.get("data") if isinstance(remote.get("data"), list) else []:
                commit = item.get("commit") if isinstance(item, dict) and isinstance(item.get("commit"), dict) else {}
                author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
                rows.append({
                    "sha": str(item.get("sha") or "")[:12],
                    "message": str(commit.get("message") or "").splitlines()[0][:300],
                    "date": str(author.get("date") or ""),
                    "url": str(item.get("html_url") or ""),
                })
            return {"ok": True, "commits": rows, "source": "repositorio público" if remote.get("source") == "github" else "cache consultada", "updated_at": remote.get("updated_at"), "offline": bool(remote.get("offline"))}
        except GitHubReadError as exc:
            return {"ok": False, "error": exc.code, "commits": [], "source": "GitHub no disponible", "updated_at": _now(), "offline": True}

    def repository_file(self, path: str, ref="main") -> dict[str, Any]:
        try:
            result = self.client.repository_file(path, ref)
            result["source"] = "repositorio público" if result.get("source") == "github" else "cache consultada"
            result["untrusted_content"] = True
            return result
        except GitHubReadError as exc:
            return {"ok": False, "error": exc.code, "source": "GitHub no disponible", "offline": True, "untrusted_content": True}
