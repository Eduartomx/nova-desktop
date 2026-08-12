from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from .doctor import NovaDoctor


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
        tk.Button(bar, text='⬆ Actualizar', command=self.quick_update, width=11).pack(side='right', padx=(0, 6))
        self._append('system', 'v0.6.x: Memory, Workspace Intelligence y actualización nativa desde GitHub.')

    def init(self, *a, **kw):
        self.workspace_window = None; self.workspace_listbox = None; self.workspace_rows = []
        original_init(self, *a, **kw)
        self.root.title(f'{self.name} · Asistente local v0.6.2')
        self.root.after(280, self._refresh_workspace_label)
        self.root.after(900, self._consume_update_status)

    def consume_update_status(self):
        path = Path(__file__).resolve().parent.parent / 'data' / 'update_last.json'
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            path.unlink(missing_ok=True)
            before = str(data.get('before') or '?')
            after = str(data.get('after') or '?')
            log = str(data.get('log') or '')
            if data.get('ok'):
                self._append('system', f'Actualización completada: Nova {before} → {after}.')
                self.status_var.set(f'Nova {after} · actualización correcta')
            else:
                error = str(data.get('error') or 'Error desconocido')
                self._append('system', f'La actualización no se pudo completar. Nova se reinició sin quedar cerrada.\n{error}\nLog: {log}')
                self.status_var.set('La actualización falló; Nova fue restaurada/reiniciada')
                messagebox.showwarning('Nova · Actualización', f'La actualización no se completó.\n\n{error}\n\nLog:\n{log}', parent=self.root)
        except Exception:
            pass

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
        root = Path(__file__).resolve().parent.parent
        runner = root / 'updater' / 'update_runner.py'
        py = root / '.venv' / 'Scripts' / 'python.exe'
        if not py.exists():
            current = Path(sys.executable)
            py = current.with_name('python.exe') if current.name.casefold() == 'pythonw.exe' and current.with_name('python.exe').exists() else current
        if not runner.exists():
            messagebox.showerror('Nova · Actualizador', f'Falta el supervisor de actualización:\n{runner}', parent=self.root)
            return
        try:
            subprocess.Popen(
                [str(py), str(runner), '--parent-pid', str(os.getpid())],
                cwd=str(root),
                creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0),
            )
            self.status_var.set('Actualizando desde GitHub… Nova se cerrará y volverá a abrirse automáticamente.')
            self.root.after(250, self._close)
        except Exception as exc:
            messagebox.showerror('Nova · Actualizador', str(exc), parent=self.root)

    UI._build = build; UI.__init__ = init; UI._refresh_workspace_label = refresh_label; UI._refresh_workspace_manager = refresh_manager
    UI.show_workspace_manager = show_manager; UI._selected_workspace = selected; UI._workspace_add_folder = add_folder; UI._workspace_activate_selected = activate; UI._workspace_open_selected = open_selected
    UI.quick_doctor = quick_doctor; UI.quick_update = quick_update; UI._consume_update_status = consume_update_status; UI._nova_v060_patched = True
    return mod
