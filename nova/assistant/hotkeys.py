from __future__ import annotations

"""Normalización y validación de atajos globales de Nova."""

from typing import Any

DEFAULT_MAIN_HOTKEY = "<ctrl>+<alt>+n"
DEFAULT_CONTEXT_HOTKEY = "<ctrl>+<alt>+<shift>+n"

_MODIFIERS = {"ctrl", "alt", "shift", "cmd"}
_SPECIAL = {
    "ctrl", "alt", "shift", "cmd", "space", "enter", "tab", "esc", "delete",
    "backspace", "home", "end", "page_up", "page_down", "up", "down", "left", "right",
    "insert", "caps_lock", "num_lock", "scroll_lock", "pause", "print_screen",
}
_SPECIAL.update({f"f{i}" for i in range(1, 25)})
_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "option": "alt",
    "windows": "cmd",
    "win": "cmd",
    "super": "cmd",
    "escape": "esc",
    "return": "enter",
    "del": "delete",
    "pgup": "page_up",
    "pgdn": "page_down",
}


def _tokens(value: Any) -> list[str]:
    raw = str(value or "").strip().casefold()
    raw = raw.replace("-", "+") if "+" not in raw and raw.count("-") >= 1 else raw
    out: list[str] = []
    for part in raw.split("+"):
        token = part.strip().strip("<>").replace(" ", "_")
        if not token:
            continue
        token = _ALIASES.get(token, token)
        if token not in out:
            out.append(token)
    return out


def normalize_hotkey(value: Any, default: str = DEFAULT_MAIN_HOTKEY) -> str:
    tokens = _tokens(value)
    if not tokens:
        tokens = _tokens(default)
    rendered: list[str] = []
    for token in tokens:
        if token in _SPECIAL:
            rendered.append(f"<{token}>")
        elif len(token) == 1 and token.isprintable():
            rendered.append(token)
        else:
            rendered.append(f"<{token}>")
    return "+".join(rendered)


def validate_hotkey(value: Any) -> tuple[bool, str, str]:
    tokens = _tokens(value)
    if len(tokens) < 2:
        return False, "Usa una combinación de al menos dos teclas, por ejemplo Ctrl+Alt+N.", ""
    unknown = [x for x in tokens if x not in _SPECIAL and not (len(x) == 1 and x.isprintable())]
    if unknown:
        return False, "Tecla no reconocida: " + ", ".join(unknown), ""
    non_modifiers = [x for x in tokens if x not in _MODIFIERS]
    if not non_modifiers:
        return False, "El atajo necesita una tecla además de los modificadores.", ""
    if len(non_modifiers) > 1:
        return False, "Usa una sola tecla principal además de Ctrl/Alt/Shift/Win.", ""
    if {"ctrl", "alt", "delete"}.issubset(set(tokens)):
        return False, "Ctrl+Alt+Delete está reservado por Windows.", ""
    normalized = normalize_hotkey(value)
    return True, "", normalized


def humanize_hotkey(value: Any) -> str:
    labels = {
        "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win",
        "space": "Espacio", "enter": "Enter", "tab": "Tab", "esc": "Esc",
        "delete": "Supr", "backspace": "Backspace", "page_up": "PageUp", "page_down": "PageDown",
        "up": "↑", "down": "↓", "left": "←", "right": "→",
    }
    parts = []
    for token in _tokens(value):
        parts.append(labels.get(token, token.upper() if len(token) == 1 or token.startswith("f") else token.title()))
    return "+".join(parts) if parts else humanize_hotkey(DEFAULT_MAIN_HOTKEY)
