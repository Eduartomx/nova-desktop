from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

SECURITY_PROFILES: dict[str, dict[str, Any]] = {
    "safe": {
        "confirm_file_writes": True,
        "confirm_powershell": True,
        "confirm_input_actions": True,
        "confirm_window_close": True,
        "confirm_window_layout": False,
        "confirm_clipboard_write": True,
        "confirm_read_selection": True,
        "confirm_uia_actions": True,
        "confirm_browser_typing": True,
        "confirm_browser_submit": True,
        "confirm_browser_sensitive_clicks": True,
        "confirm_browser_close_tab": False,
        "backup_overwritten_files": True,
    },
    "trusted": {
        "confirm_file_writes": False,
        "confirm_powershell": False,
        "confirm_input_actions": False,
        "confirm_window_close": False,
        "confirm_window_layout": False,
        "confirm_clipboard_write": False,
        "confirm_read_selection": False,
        "confirm_uia_actions": False,
        "confirm_browser_typing": False,
        "confirm_browser_submit": False,
        "confirm_browser_sensitive_clicks": True,
        "confirm_browser_close_tab": False,
        "backup_overwritten_files": True,
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "assistant_name": "Nova",
    "model": "qwen3.5:4b",
    "ollama_host": "http://127.0.0.1:11434",
    "context_tokens": 8192,
    "max_agent_steps": 10,
    "task_engine": {
        "enabled": True,
        "performance_mode": "fast",
        "fast_system_diagnostics": True,
        "direct_read_tools": True,
        "fast_verification": True,
        "read_tool_cache_seconds": 8,
        "task_context_tokens": 4096,
        "json_max_tokens": 320,
        "executor_max_tokens": 520,
        "summary_max_tokens": 480,
        "diagnostic_max_tokens": 420,
        "max_plan_steps": 8,
        "max_agent_turns_per_step": 6,
        "max_step_retries": 1,
        "stop_on_failed_step": True,
        "auto_replan": True,
        "max_replans": 2,
        "max_tool_calls": 40,
        "max_task_minutes": 20,
        "pause_poll_ms": 150,
    },
    "recent_messages": 16,
    "workspace": {
        "enabled": True,
        "auto_memory_context": True,
        "relevant_memory_limit": 8,
        "recent_memory_limit": 8,
        "refresh_metadata_on_select": True,
        "registered_paths_allowed": True,
        "index_max_files": 12000,
        "index_max_depth": 8,
        "index_hash_max_bytes": 2097152,
        "index_ignore_dirs": [],
        "index_ignore_globs": ["*.pyc", "*.tmp", "*.part", "*.log.gz"],
    },
    "semantic_memory": {
        "enabled": True,
        "model": "qwen3-embedding:0.6b",
        "auto_pull_model": False,
        "request_timeout_seconds": 8,
        "batch_size": 16,
        "lazy_index": True,
        "lazy_index_limit": 24,
        "semantic_weight": 0.58,
        "lexical_weight": 0.27,
        "importance_weight": 0.10,
        "recency_weight": 0.05,
        "minimum_semantic_score": 0.12,
    },
    "continuity": {
        "enabled": True,
        "auto_checkpoint_tasks": True,
        "inject_context": True,
        "history_limit": 12,
        "max_items_per_checkpoint": 60,
    },
    "hotkey": "<ctrl>+<space>",
    "desktop": {
        "auto_context": True,
        "auto_visual_context": True,
        "context_hotkey": "<ctrl>+<shift>+<space>",
        "refresh_context_ms": 900,
    },
    "voice": {
        "enabled": True,
        "language": "es",
        "push_to_talk_hotkey": "<f9>",
        "stt_model": "small",
        "stt_device": "cpu",
        "stt_compute_type": "int8",
        "tts_enabled": True,
        "wakeword_enabled": True,
        "wakeword": "hey_jarvis",
        "wakeword_threshold": 0.55,
        "max_command_seconds": 15,
        "speech_start_timeout": 4.0,
        "silence_seconds": 1.0,
        "energy_threshold": 0.015,
    },
    "internet": {"enabled": True, "timeout_seconds": 12, "max_page_chars": 18000},
    "browser": {
        "enabled": True,
        "channel": "msedge",
        "headless": False,
        "search_engine": "google",
        "action_timeout_ms": 9000,
        "navigation_timeout_ms": 25000,
        "command_timeout_seconds": 35,
        "snapshot_depth": 6,
    },
    "openai": {"enabled": True, "model": "gpt-5.6-luna", "confirm_paid_requests": True},
    "security": {
        "profile": "trusted",
        "restrict_files_to_allowed_roots": True,
        "allowed_roots": ["~"],
        **SECURITY_PROFILES["trusted"],
    },
}


def apply_security_profile(config: dict[str, Any], profile: str) -> dict[str, Any]:
    profile = str(profile or "trusted").lower().strip()
    if profile not in SECURITY_PROFILES:
        raise ValueError(f"Perfil de seguridad desconocido: {profile}")
    security = config.setdefault("security", {})
    security["profile"] = profile
    for key, value in SECURITY_PROFILES[profile].items():
        security[key] = value
    return config


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        return deepcopy(DEFAULT_CONFIG)

    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    merged = deepcopy(DEFAULT_CONFIG)
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value

    original_security = data.get("security", {}) if isinstance(data.get("security", {}), dict) else {}
    profile = str(original_security.get("profile", "trusted")).lower().strip()
    if profile not in SECURITY_PROFILES:
        profile = "trusted"
    apply_security_profile(merged, profile)
    return merged
