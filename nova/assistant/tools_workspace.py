from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .workspace import WorkspaceManager


def schemas_v060() -> list[dict[str, Any]]:
    def fn(name, description, properties=None, required=None):
        return {"type": "function", "function": {"name": name, "description": description,
                "parameters": {"type": "object", "properties": properties or {}, "required": required or []}}}
    return [
        fn("memory_search", "Busca en memoria local, priorizando el workspace activo.", {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
        fn("workspace_list", "Lista proyectos/workspaces conocidos y el activo."),
        fn("workspace_create", "Registra una carpeta como workspace y la activa.", {"path": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"}}, ["path"]),
        fn("workspace_set_active", "Cambia el workspace activo por nombre o ID.", {"workspace": {"type": "string"}}, ["workspace"]),
        fn("workspace_info", "Obtiene información del workspace activo o indicado.", {"workspace": {"type": "string"}, "refresh": {"type": "boolean"}}),
        fn("workspace_open", "Abre el workspace en Explorador de Windows.", {"workspace": {"type": "string"}}),
    ]


def install_tools_v060():
    from . import tools as mod

    existing = {x.get('function', {}).get('name') for x in mod.TOOL_SCHEMAS}
    for schema in schemas_v060():
        if schema['function']['name'] not in existing:
            mod.TOOL_SCHEMAS.append(schema)
    for schema in mod.TOOL_SCHEMAS:
        if schema.get('function', {}).get('name') == 'remember':
            fn = schema['function']; fn['description'] = 'Guarda un dato estable en memoria local; opcionalmente en el workspace activo.'
            props = fn.setdefault('parameters', {}).setdefault('properties', {})
            props.setdefault('category', {'type': 'string'}); props.setdefault('workspace', {'type': 'boolean'}); break

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, '_nova_v060_patched', False):
        original_init = LocalTools.__init__; original_allowed = LocalTools._allowed_roots

        def init(self, *a, **kw):
            original_init(self, *a, **kw); self.workspaces = WorkspaceManager(self.memory)

        def allowed(self):
            roots = list(original_allowed(self))
            if self.config.get('workspace', {}).get('registered_paths_allowed', True):
                try:
                    for ws in self.memory.list_workspaces(100):
                        p = Path(str(ws.get('path', ''))).resolve()
                        if p not in roots: roots.append(p)
                except Exception: pass
            return roots

        def remember(self, key, value, category='fact', workspace=False):
            active = self.memory.active_workspace() if workspace else None
            if workspace and not active: return {'ok': False, 'error': 'No hay workspace activo.'}
            wid = int(active['id']) if active else None
            self.memory.set_memory(key, value, category=category or 'fact', workspace_id=wid)
            return {'ok': True, 'stored': key, 'scope': 'workspace' if wid else 'global', 'workspace': active.get('name') if active else None}

        def memory_search(self, query, limit=8):
            active = self.memory.active_workspace(); wid = int(active['id']) if active else None
            return {'ok': True, 'query': query, 'workspace': active.get('name') if active else None,
                    'results': self.memory.search_memory(query, limit, workspace_id=wid)}

        def workspace_list(self):
            rows = self.workspaces.list(40)
            return {'ok': True, 'active': next((x for x in rows if x.get('is_active')), None), 'workspaces': rows}

        def workspace_create(self, path, name='', description=''):
            p = self._resolve_path(path)
            if not p.is_dir(): return {'ok': False, 'error': f'La carpeta no existe: {p}'}
            if not self._trusted_mode(): self._ensure_allowed(p)
            return {'ok': True, 'workspace': self.workspaces.create(str(p), name=name or None, description=description or '')}

        def workspace_set_active(self, workspace):
            ws = self.workspaces.set_active(workspace)
            return {'ok': bool(ws), 'workspace': ws, 'error': None if ws else f'No encontré el workspace: {workspace}'}

        def workspace_info(self, workspace='', refresh=True):
            ws = self.workspaces.inspect(workspace or None, refresh=bool(refresh))
            if not ws: return {'ok': False, 'error': 'No hay workspace activo o no encontré el indicado.'}
            return {'ok': True, 'exists': Path(ws['path']).is_dir(), 'workspace': ws}

        def workspace_open(self, workspace=''):
            ws = self.memory.resolve_workspace(workspace or None)
            if not ws: return {'ok': False, 'error': 'No hay workspace activo o no encontré el indicado.'}
            p = Path(ws['path']); self._ensure_allowed(p)
            if not p.is_dir(): return {'ok': False, 'error': f'La carpeta ya no existe: {p}'}
            os.startfile(str(p)); self.memory.set_active_workspace(int(ws['id']))
            return {'ok': True, 'opened': str(p), 'workspace': ws.get('name')}

        LocalTools.__init__ = init; LocalTools._allowed_roots = allowed
        LocalTools.remember = remember; LocalTools.memory_search = memory_search
        LocalTools.workspace_list = workspace_list; LocalTools.workspace_create = workspace_create
        LocalTools.workspace_set_active = workspace_set_active; LocalTools.workspace_info = workspace_info
        LocalTools.workspace_open = workspace_open; LocalTools._nova_v060_patched = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, '_nova_v060', False):
        all_by_name = {x['function']['name']: x for x in mod.TOOL_SCHEMAS}
        extra = {'remember', 'memory_search', 'workspace_list', 'workspace_create', 'workspace_set_active', 'workspace_info', 'workspace_open'}
        cues = ('recuerda', 'recordar', 'memoria', 'proyecto', 'workspace', 'servidor', 'continúa', 'continua', 'lo de ayer', 'dónde está', 'donde esta', 'ruta del proyecto')
        def selector(text):
            rows = list(original_selector(text)); names = {x['function']['name'] for x in rows}
            if any(c in (text or '').casefold() for c in cues):
                rows += [all_by_name[n] for n in extra if n in all_by_name and n not in names]
            return rows
        selector._nova_v060 = True; mod.select_tool_schemas = selector
    return mod
