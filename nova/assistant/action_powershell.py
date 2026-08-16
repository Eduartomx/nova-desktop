from __future__ import annotations

"""Conservative classifier for Nova's temporary PowerShell capability.

This is deliberately not a general PowerShell parser.  Only a tiny grammar of
read-only commands is understood; everything else fails closed before a UI
approval request or subprocess can be created.
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class PowerShellAssessment:
    allowed: bool
    cmdlet: str = ""
    effect: str = "forbidden"
    target: str = "PowerShell no clasificado"
    reason: str = "Sintaxis PowerShell fuera de la lista positiva."


_UNSAFE_SYNTAX = re.compile(r"[\r\n;|&<>`$@{}()\[\]]")
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.?*-]{1,80}")
_SAFE_ID = re.compile(r"[0-9]{1,10}")
_CIM_CLASSES = {
    "win32_operatingsystem": "sistema operativo",
    "win32_computersystem": "equipo local",
    "win32_processor": "procesador",
}


def _reject(reason: str) -> PowerShellAssessment:
    return PowerShellAssessment(False, reason=str(reason or "PowerShell no clasificable."))


def classify_powershell(command: str) -> PowerShellAssessment:
    raw = str(command or "").strip()
    if not raw:
        return _reject("Comando vacío.")
    if len(raw) > 1024 or _UNSAFE_SYNTAX.search(raw) or "--%" in raw:
        return _reject("Composición, pipeline, redirección o sintaxis dinámica no permitida.")

    # Quotes, escapes and whitespace continuation deliberately remain outside
    # the supported grammar.  This prevents aliases, concatenation and hidden
    # nested invocations from acquiring a misleading classification.
    if any(ch in raw for ch in ('"', "'", "\\`")):
        return _reject("Cadenas o escapes no admitidos por la gramática acotada.")
    tokens = raw.split()
    if not tokens or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", tokens[0]):
        return _reject("Cmdlet no reconocible.")

    cmdlet = tokens[0].casefold()
    args = tokens[1:]

    if cmdlet == "get-date" and not args:
        return PowerShellAssessment(True, "Get-Date", "sensitive_read", "Get-Date · lectura local · reloj del sistema", "Consulta acotada del reloj local.")
    if cmdlet == "get-location" and not args:
        return PowerShellAssessment(True, "Get-Location", "sensitive_read", "Get-Location · lectura local · directorio actual", "Consulta acotada del directorio actual.")
    if cmdlet == "get-computerinfo" and not args:
        return PowerShellAssessment(True, "Get-ComputerInfo", "sensitive_read", "Get-ComputerInfo · lectura sensible · equipo local", "Consulta acotada de información del equipo.")

    if cmdlet in {"get-process", "get-service"}:
        canonical = "Get-Process" if cmdlet == "get-process" else "Get-Service"
        noun = "proceso" if cmdlet == "get-process" else "servicio"
        if not args:
            return PowerShellAssessment(True, canonical, "sensitive_read", f"{canonical} · lectura sensible · lista de {noun}s", f"Consulta acotada de {noun}s locales.")
        if len(args) == 2 and args[0].casefold() == "-name" and _SAFE_NAME.fullmatch(args[1]):
            return PowerShellAssessment(True, canonical, "sensitive_read", f"{canonical} · lectura sensible · {noun} {args[1][:80]}", f"Consulta acotada de un {noun} local.")
        if cmdlet == "get-process" and len(args) == 2 and args[0].casefold() == "-id" and _SAFE_ID.fullmatch(args[1]):
            return PowerShellAssessment(True, "Get-Process", "sensitive_read", "Get-Process · lectura sensible · proceso por identificador", "Consulta acotada de un proceso local.")
        return _reject(f"Argumentos no permitidos para {canonical}.")

    if cmdlet == "get-ciminstance" and len(args) == 2 and args[0].casefold() == "-classname":
        target = _CIM_CLASSES.get(args[1].casefold())
        if target:
            return PowerShellAssessment(True, "Get-CimInstance", "sensitive_read", f"Get-CimInstance · lectura sensible · {target}", "Consulta CIM local acotada.")
        return _reject("Clase CIM fuera de la lista positiva.")

    return _reject("Cmdlet fuera de la lista positiva de Nova v0.10.0.")
