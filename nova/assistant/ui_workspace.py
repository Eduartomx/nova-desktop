from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from .doctor import NovaDoctor

SUPERVISOR_ALREADY_RUNNING_CODE = 5
PIP_TERMINATION_UNCONFIRMED_CODE = 6
UPDATE_POLL_MS = 300


def _set_update_button_state(ui, state: str) -> None:
    button = getattr(ui, 'update_button', None)
    if button is None:
        return
    try:
        button.configure(state=state)
    except Exception:
        pass


def _update_status_token(path: Path) -> str:
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return ''
        return '|'.join(
            str(data.get(key) or '')
            for key in ('timestamp', 'state', 'before', 'after', 'error', 'log')
        )
    except Exception:
        return ''


def _mark_update_status_displayed(path: Path, data: dict) -> None:
    """Preserve the last result for Doctor while preventing repeated UI display."""
    payload = dict(data)
    payload['displayed'] = True
    try:
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def _schedule_update_poll(ui, *, root: Path, consume_status=None) -> None:
    tk_root = getattr(ui, 'root', None)
    if tk_root is None:
        return
    try:
        tk_root.after(
            UPDATE_POLL_MS,
            lambda: _poll_update_supervisor(ui, root=root, consume_status=consume_status),
        )
    except Exception:
        # Tk may already be destroyed because shutdown_for_update succeeded.
        return


def _poll_update_supervisor(ui, *, root: Path, consume_status=None) -> None:
    if not bool(getattr(ui, '_update_supervisor_active', False)):
        return
    proc = getattr(ui, '_update_supervisor_process', None)
    if proc is None:
        ui._update_supervisor_active = False
        _set_update_button_state(ui, 'normal')
        return
    try:
        rc = proc.poll()
    except Exception as exc:
        ui._update_supervisor_active = False
        ui._update_supervisor_process = None
        _set_update_button_state(ui, 'normal')
        try:
            ui.status_var.set(f'No pude consultar el supervisor: {exc}')
        except Exception:
            pass
        return

    if rc is None:
        try:
            ui.status_var.set('Actualización en curso…')
        except Exception:
            pass
        _schedule_update_poll(ui, root=root, consume_status=consume_status)
        return

    ui._update_supervisor_active = False
    ui._update_supervisor_process = None
    _set_update_button_state(ui, 'normal')

    if int(rc) == SUPERVISOR_ALREADY_RUNNING_CODE:
        try:
            ui.status_var.set('Ya existe una actualización en curso.')
            if hasattr(ui, '_append'):
                ui._append('system', 'Ya existe una actualización en curso; no inicié otro supervisor.')
        except Exception:
            pass
        return

    consumed = False
    if callable(consume_status):
        try:
            consumed = bool(consume_status(only_if_new=True))
        except TypeError:
            consumed = bool(consume_status())
        except Exception:
            consumed = False

    if consumed:
        return
    try:
        if int(rc) == 4:
            ui.status_var.set('No se pudo coordinar la actualización; Nova continúa abierta.')
        elif int(rc) == PIP_TERMINATION_UNCONFIRMED_CODE:
            ui.status_var.set('Actualización detenida por seguridad: terminación de pip no confirmada.')
        elif int(rc) != 0:
            ui.status_var.set(f'El supervisor terminó con código {rc}; revisa el estado de actualización.')
        else:
            ui.status_var.set('El supervisor terminó; no publicó un resultado nuevo.')
    except Exception:
        pass


def _start_update_supervisor(ui, *, root: Path | None = None, popen=None, show_error=None, consume_status=None) -> bool:
    """Start one resident-aware updater without initiating local shutdown."""
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    runner = root / 'updater' / 'update_runner.py'
    py = root / '.venv' / 'Scripts' / 'python.exe'
    if not py.exists():
        current = Path(sys.executable)
        py = current.with_name('python.exe') if current.name.casefold() == 'pythonw.exe' and current.with_name('python.exe').exists() else current

    existing = getattr(ui, '_update_supervisor_process', None)
    if bool(getattr(ui, '_update_supervisor_active', False)):
        try:
            if existing is None or existing.poll() is None:
                ui.status_var.set('Actualización ya en curso.')
                return False
        except Exception:
            ui.status_var.set('Actualización ya en curso.')
            return False
        ui._update_supervisor_active = False
        ui._update_supervisor_process = None

    def report_error(message: str):
        ui._update_supervisor_active = False
        ui._update_supervisor_process = None
        _set_update_button_state(ui, 'normal')
        try:
            ui.status_var.set('No pude iniciar el supervisor; Nova continúa abierta.')
        except Exception:
            pass
        if callable(show_error):
            show_error('Nova · Actualizador', str(message))

    if not runner.exists():
        report_error(f'Falta el supervisor de actualización:\n{runner}')
        return False

    status_file = root / 'data' / 'update_last.json'
    ui._update_status_before_supervisor = _update_status_token(status_file)
    ui._update_supervisor_root = root
    try:
        ui.status_var.set('Iniciando supervisor de actualización…')
    except Exception:
        pass

    try:
        launcher = popen or subprocess.Popen
        proc = launcher(
            [str(py), str(runner), '--parent-pid', str(os.getpid())],
            cwd=str(root),
            creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0),
        )
    except Exception as exc:
        report_error(str(exc))
        return False

    ui._update_supervisor_process = proc
    ui._update_supervisor_active = True
    _set_update_button_state(ui, 'disabled')
    try:
        ui.status_var.set('Actualización iniciada. Nova permanecerá activa hasta que el supervisor solicite el cierre.')
    except Exception:
        pass
    _schedule_update_poll(ui, root=root, consume_status=consume_status)
    return True


def install_ui_v060():
    from . import ui as mod
    UI = mod.AssistantUI
    if getattr(UI, '_nova_v060_patched', False): return mod
    tk = mod.tk; messagebox = mod.messagebox
    from tkinter import filedialog
    original_init = UI.__init__; original_build = UI._build

    def build(self):
        original_append = self._append
        def filtered(role, text):
            if role == 'system' and str(text).startswith('v0.5.5:'): return
            return original_append(role, text)
        self._append = filtered
        try: original_build(self)
        finally: self._append = original_append
        bar = tk.Frame(self.root, padx=12, pady=3); bar.pack(fill='x', before=self.chat)
        tk.Label(bar, text='Workspace:', font=('Segoe UI', 9, 'bold')).pack(side='left')
        self.workspace_var = tk.StringVar(value='Sin proyecto activo')
        tk.Label(bar, textvariable=self.workspace_var, anchor='w').pack(side='left', padx=(6, 8), fill='x', expand=True)
        tk.Button(bar, text='📁 Proyectos', command=self.show_workspace_manager, width=12).pack(side='right')
        tk.Button(bar, text='🩺 Doctor', command=self.quick_doctor, width=10).pack(side='right', padx=(0, 6))
        self.update_button = tk.Button(bar, text='⬆ Actualizar', command=self.quick_update, width=11)
        self.update_button.pack(side='right', padx=(0, 6))
        self._append('system', 'v0.6.x: Memory, Workspace Intelligence y actualización nativa desde GitHub.')

    def init(self, *a, **kw):
        self.workspace_window = None; self.workspace_listbox = None; self.workspace_rows = []
        self._update_supervisor_process = None
        self._update_supervisor_active = False
        self._update_supervisor_root = Path(__file__).resolve().parent.parent
        self._update_status_before_supervisor = ''
        self._last_update_status_token = ''
        original_init(self, *a, **kw)
        self.root.title(f'{self.name} · Asistente local')
        self.root.after(280, self._refresh_workspace_label)
        self.root.after(900, self._consume_update_status)

    def consume_update_status(self, only_if_new=False):
        root = Path(getattr(self, '_update_supervisor_root', Path(__file__).resolve().parent.parent))
        path = root / 'data' / 'update_last.json'
        if not path.exists() or bool(getattr(self, '_update_supervisor_active', False)):
            return False
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                return False
            token = _update_status_token(path)
            if bool(data.get('displayed')):
                return False
            if only_if_new and token and token == str(getattr(self, '_update_status_before_supervisor', '') or ''):
                return False
            if token and token == str(getattr(self, '_last_update_status_token', '') or ''):
                return False
            self._last_update_status_token = token
            _mark_update_status_displayed(path, data)
            before = str(data.get('before') or '?')
            after = str(data.get('after') or '?')
            log = str(data.get('log') or '')
            state = str(data.get('state') or '')
            tray = getattr(self, 'tray_controller', None)
            if data.get('ok'):
                self._append('system', f'Actualización completada: Nova {before} → {after}.')
                self.status_var.set(f'Nova {after} · actualización correcta')
                if tray is not None:
                    tray.notify('update_finished', 'Nova actualizada', f'Actualización a Nova {after} completada.')
                return True

            error = str(data.get('error') or 'Error desconocido')
            if state == 'coordination_failed':
                self._append('system', f'No se pudo iniciar la actualización y Nova permaneció abierta.\n{error}\nLog: {log}')
                self.status_var.set('No se pudo coordinar la actualización; Nova continúa abierta')
                messagebox.showwarning('Nova · Actualización', f'La actualización no pudo comenzar.\n\n{error}\n\nLog:\n{log}', parent=self.root)
                return True
            if state == 'pip_termination_unconfirmed':
                pids = [int(pid) for pid in (data.get('remaining_pids') or []) if str(pid).isdigit() and int(pid) > 0]
                pid_text = ('\nPID restantes: ' + ', '.join(str(pid) for pid in pids)) if pids else ''
                self._append('system', f'Actualización detenida por seguridad: no se confirmó la terminación de pip. No se relanzó Nova automáticamente.\n{error}{pid_text}\nLog: {log}')
                self.status_var.set('Recuperación pendiente · terminación de pip no confirmada')
                messagebox.showwarning('Nova · Recuperación requerida', f'{error}{pid_text}\n\nLog:\n{log}', parent=self.root)
                return True

            self._append('system', f'La actualización no se pudo completar. Nova se reinició sin quedar cerrada.\n{error}\nLog: {log}')
            self.status_var.set('La actualización falló; Nova fue restaurada/reiniciada')
            if tray is not None:
                tray.notify('update_error', 'Nova necesita atención', 'La actualización no pudo completarse. Abre Nova para revisarla.')
            messagebox.showwarning('Nova · Actualización', f'La actualización no se completó.\n\n{error}\n\nLog:\n{log}', parent=self.root)
            return True
        except Exception:
            return False

    def refresh_label(self):
        try:
            ws = self.agent.memory.active_workspace()
            self.workspace_var.set(f"{ws.get('name')} · {ws.get('kind','generic')} · {ws.get('path')}" if ws else 'Sin proyecto activo')
        except Exception as exc: self.workspace_var.set(f'Workspace no disponible: {exc}')

    def refresh_manager(self):
        self.workspace_rows = self.agent.memory.list_workspaces(60); lb = self.workspace_listbox
        if lb is not None:
            lb.delete(0, 'end')
            for ws in self.workspace_rows:
                lb.insert('end', f"{'●' if ws.get('is_active') else ' '} {ws.get('name')}  [{ws.get('kind')}]  {ws.get('path')}")
        self._refresh_workspace_label()

    def show_manager(self):
        if self.workspace_window is not None:
            try:
                if self.workspace_window.winfo_exists():
                    self.workspace_window.deiconify(); self.workspace_window.lift(); self._refresh_workspace_manager(); return
            except Exception: pass
        win = tk.Toplevel(self.root); self.workspace_window = win; win.title('Nova · Workspaces'); win.geometry('820x460'); win.minsize(620, 340)
        head = tk.Frame(win, padx=10, pady=8); head.pack(fill='x'); tk.Label(head, text='Proyectos conocidos', font=('Segoe UI', 14, 'bold')).pack(side='left'); tk.Button(head, text='Actualizar', command=self._refresh_workspace_manager).pack(side='right')
        body = tk.Frame(win, padx=10, pady=4); body.pack(fill='both', expand=True); lb = tk.Listbox(body, font=('Segoe UI', 10), activestyle='dotbox'); lb.pack(fill='both', expand=True); self.workspace_listbox = lb
        lb.bind('<Double-Button-1>', lambda _e: self._workspace_activate_selected())
        ctr = tk.Frame(win, padx=10, pady=10); ctr.pack(fill='x'); tk.Button(ctr, text='➕ Añadir carpeta', command=self._workspace_add_folder).pack(side='left'); tk.Button(ctr, text='✓ Activar', command=self._workspace_activate_selected).pack(side='left', padx=(6, 0)); tk.Button(ctr, text='📂 Abrir', command=self._workspace_open_selected).pack(side='left', padx=(6, 0))
        win.protocol('WM_DELETE_WINDOW', lambda: win.withdraw()); self._refresh_workspace_manager()

    def selected(self):
        if self.workspace_listbox is None: return None
        sel = self.workspace_listbox.curselection(); idx = int(sel[0]) if sel else -1
        return self.workspace_rows[idx] if 0 <= idx < len(self.workspace_rows) else None

    def add_folder(self):
        path = filedialog.askdirectory(parent=self.workspace_window or self.root, title='Selecciona la carpeta del proyecto')
        if not path: return
        try:
            ws = self.agent.tools.workspaces.create(path); self.status_var.set(f"Workspace activo: {ws.get('name')}"); self._refresh_workspace_manager()
        except Exception as exc: messagebox.showerror('Nova · Workspace', str(exc), parent=self.workspace_window or self.root)

    def activate(self):
        ws = self._selected_workspace()
        if not ws: self.status_var.set('Selecciona un workspace'); return
        try:
            active = self.agent.tools.workspaces.set_active(int(ws['id']))
            if active and self.config.get('workspace', {}).get('refresh_metadata_on_select', True): self.agent.tools.workspaces.inspect(int(active['id']), refresh=True)
            self.status_var.set(f"Workspace activo: {ws.get('name')}"); self._refresh_workspace_manager()
        except Exception as exc: messagebox.showerror('Nova · Workspace', str(exc), parent=self.workspace_window or self.root)

    def open_selected(self):
        ws = self._selected_workspace()
        if not ws: self.status_var.set('Selecciona un workspace'); return
        result = self.agent.tools.workspace_open(str(ws['id']))
        if not result.get('ok'): messagebox.showerror('Nova · Workspace', result.get('error', 'No pude abrirlo'), parent=self.workspace_window or self.root)

    def quick_doctor(self):
        if self.busy: self.status_var.set('Espera a que termine la tarea actual'); return
        self.busy = True; self.send_button.configure(state='disabled'); self.mic_button.configure(state='disabled'); self.status_var.set('Nova Doctor: comprobando componentes…')
        def worker():
            try: self.result_queue.put(('answer', NovaDoctor.format_text(NovaDoctor(self.config, self.agent.memory).run())))
            except Exception as exc: self.result_queue.put(('error', f'Nova Doctor: {exc}'))
        threading.Thread(target=worker, daemon=True).start()

    def quick_update(self):
        return _start_update_supervisor(
            self,
            root=Path(getattr(self, '_update_supervisor_root', Path(__file__).resolve().parent.parent)),
            show_error=lambda title, message: messagebox.showerror(title, message, parent=self.root),
            consume_status=self._consume_update_status,
        )

    UI._build = build; UI.__init__ = init; UI._refresh_workspace_label = refresh_label; UI._refresh_workspace_manager = refresh_manager
    UI.show_workspace_manager = show_manager; UI._selected_workspace = selected; UI._workspace_add_folder = add_folder; UI._workspace_activate_selected = activate; UI._workspace_open_selected = open_selected
    UI.quick_doctor = quick_doctor; UI.quick_update = quick_update; UI._consume_update_status = consume_update_status; UI._nova_v060_patched = True
    return mod
