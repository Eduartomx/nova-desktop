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
    "performance_profiler": {
        "enabled": True,
        "max_events": 5000,
        "slow_ms": 1200,
        "summary_hours": 24,
    },
    "perception": {
        "enabled": True,
        "poll_interval_ms": 1100,
        "inject_context": True,
        "keep_last_external": True,
        "persist_events": True,
        "persist_window_titles": False,
        "max_events": 1200,
        "title_max_chars": 180,
        "system_sample_seconds": 5.0,
        "workspace_suggestion_threshold": 0.78,
        "auto_activate_workspace": False,
        "cpu_warn_percent": 92.0,
        "memory_warn_percent": 90.0,
    },
    "context_intelligence": {
        "enabled": True,
        "recent_event_limit": 32,
        "prompt_event_limit": 4,
        "minimum_prompt_relevance": 0.30,
        "workspace_confidence_bonus": 0.22,
        "system_pressure_bonus": 0.38,
        "app_change_bonus": 0.16,
        "workspace_change_bonus": 0.24,
        "include_window_title_in_prompt": False,
    },
    "workspace_autodetect": {
        "enabled": True,
        "learn_enabled": True,
        "observe_interval_seconds": 4.0,
        "learn_cooldown_seconds": 20.0,
        "suggestion_threshold": 0.84,
        "ambiguity_margin": 0.18,
        "minimum_confirmations": 3,
        "auto_activate": False,
        "auto_activate_threshold": 0.97,
        "auto_activate_min_confirmations": 6,
        "auto_activate_dwell_seconds": 15.0,
        "auto_activate_cooldown_seconds": 90.0,
        "learn_app_kinds": ["code_editor", "terminal", "game", "office"],
    },
    "anomaly_detection": {
        "enabled": True,
        "sample_interval_seconds": 5.0,
        "baseline_min_samples": 24,
        "process_baseline_min_samples": 12,
        "sigma_threshold": 3.0,
        "system_cpu_floor": 82.0,
        "system_memory_floor": 86.0,
        "system_cpu_min_delta": 18.0,
        "system_memory_min_delta": 12.0,
        "new_process_cpu_threshold": 35.0,
        "new_process_memory_threshold": 10.0,
        "process_cpu_floor": 25.0,
        "process_memory_floor": 8.0,
        "process_cpu_min_delta": 12.0,
        "process_memory_min_delta": 5.0,
        "sustained_samples": 3,
        "event_cooldown_seconds": 300.0,
        "max_events": 800,
        "notify_high_only": True,
        "expected_heavy_processes": ["ollama.exe", "llama.exe"],
        "crash_signal_processes": ["werfault.exe", "wermgr.exe"],
    },
    "event_driven_vision": {
        "enabled": True,
        "user_visual_queries": True,
        "auto_event_capture": True,
        "auto_capture_event_types": ["crash_signal"],
        "auto_capture_min_severity": "high",
        "auto_capture_high_anomalies": False,
        "cooldown_seconds": 75.0,
        "max_auto_captures_per_hour": 4,
        "analysis_timeout_seconds": 25.0,
        "model": "",
        "require_vision_capability": True,
        "max_image_dimension": 1440,
        "jpeg_quality": 78,
        "retain_images": False,
        "persist_analysis": False,
        "max_events": 300,
    },
    "skills": {
        "enabled": True,
        "inject_context": True,
        "suggest_on_match": True,
        "suggest_threshold": 0.72,
        "explicit_run_threshold": 0.58,
        "max_prompt_skills": 3,
        "max_steps": 24,
        "max_skills": 500,
        "max_trigger_phrases": 20,
        "workspace_scoped_by_default": False,
        "generated_skills_start_as_draft": True,
        "auto_execute_matches": False,
    },
    "confidence": {
        "enabled": True,
        "persist_assessments": True,
        "max_assessments": 1800,
        "low_threshold": 0.52,
        "high_threshold": 0.78,
        "escalation_candidate_threshold": 0.50,
        "inject_context": True,
        "surface_low_confidence": True,
        "minimum_evidence_for_high": 2,
    },
    "expert_escalation": {
        "enabled": True,
        "auto_free_second_opinion": True,
        "auto_free_max_risk": "normal",
        "provider_order": ["cerebras", "groq"],
        "max_events": 800,
        "free_api": {
            "cerebras": {
                "enabled": True,
                "model": "gpt-oss-120b",
                "endpoint": "https://api.cerebras.ai/v1/chat/completions",
                "api_key_env": "CEREBRAS_API_KEY",
                "timeout_seconds": 24,
                "max_completion_tokens": 900,
                "reasoning_effort": "medium"
            },
            "groq": {
                "enabled": True,
                "model": "qwen/qwen3.6-27b",
                "endpoint": "https://api.groq.com/openai/v1/chat/completions",
                "api_key_env": "GROQ_API_KEY",
                "timeout_seconds": 24,
                "max_completion_tokens": 900
            }
        },
        "chatgpt_assisted": {
            "enabled": True,
            "url": "https://chatgpt.com/",
            "open_browser": True,
            "copy_query_to_clipboard": True,
            "auto_prepare_on_conflict": False
        },
        "privacy": {
            "redact_secrets": True,
            "max_problem_chars": 5200,
            "max_local_answer_chars": 5200,
            "max_external_response_chars": 6500,
            "persist_prompts": False,
            "persist_responses": False
        }
    },
    "hotkey": "<ctrl>+<alt>+<space>",
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
    "openai": {
        "enabled": False,
        "model": "gpt-5.6-luna",
        "confirm_paid_requests": True,
        "paid_api_opt_in": False,
    },
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

    migrated = False
    if str(data.get("hotkey") or "").strip().casefold() == "<ctrl>+<space>":
        merged["hotkey"] = "<ctrl>+<alt>+<space>"
        migrated = True

    # v0.8.2: el antiguo default permitía la API de OpenAI de pago. La preferencia
    # actual del proyecto es no usarla. Solo migramos una configuración antigua que
    # aún no posee el marcador de opt-in; un usuario puede habilitarla de nuevo
    # explícitamente estableciendo paid_api_opt_in=true.
    old_openai = data.get("openai", {}) if isinstance(data.get("openai", {}), dict) else {}
    if old_openai.get("enabled") is True and "paid_api_opt_in" not in old_openai:
        merged.setdefault("openai", {})["enabled"] = False
        merged["openai"]["paid_api_opt_in"] = False
        migrated = True

    original_security = data.get("security", {}) if isinstance(data.get("security", {}), dict) else {}
    profile = str(original_security.get("profile", "trusted")).lower().strip()
    if profile not in SECURITY_PROFILES:
        profile = "trusted"
    apply_security_profile(merged, profile)

    if migrated:
        try:
            save_config(merged)
        except Exception:
            pass
    return merged
