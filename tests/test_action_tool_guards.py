from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


_PRELUDE = r'''
import tempfile
from pathlib import Path
from assistant.memory import MemoryStore
from assistant.tools import LocalTools
from assistant.tools_desktop import install_tools_desktop
from assistant.tools_file_safety import install_tools_file_safety
from assistant.tools_action_guard import install_tools_action_guard
install_tools_desktop(); install_tools_file_safety(); install_tools_action_guard()
class Locator:
    def __init__(self, attrs=None, fail_inspection=False):
        self.fills=[]; self.keys=[]; self.clicks=0; self.attrs=dict(attrs or {}); self.fail_inspection=fail_inspection
    def fill(self, value): self.fills.append(value)
    def press(self, key): self.keys.append(key)
    def click(self): self.clicks += 1
    def inner_text(self): return self.attrs.get('label','')
    def get_attribute(self, name):
        if self.fail_inspection: raise RuntimeError('inspection failed')
        return self.attrs.get(name,'')
    def evaluate(self, script):
        if self.fail_inspection: raise RuntimeError('inspection failed')
        if 'return !!el.form' in script: return bool(self.attrs.get('may_submit',False))
        if '!!el.form' in script: return bool(self.attrs.get('form_associated',False))
        if 'String(el.type' in script: return self.attrs.get('effective_type',self.attrs.get('type',''))
        if 'tagName' in script: return self.attrs.get('tag','button')
        return ''
class Page: url='https://example.test/form'
class Browser:
    def __init__(self, attrs=None, fail_inspection=False): self._page=Page(); self.locator=Locator(attrs,fail_inspection)
    def call(self, callback, timeout=None): return callback()
    @property
    def page(self): return self._page
    def resolve(self, target): return self.locator
def make(root, profile='balanced'):
    cfg={'security':{'profile':profile,'allowed_roots':[str(root)],'restrict_files_to_allowed_roots':True,'backup_overwritten_files':True}}
    return LocalTools(cfg, MemoryStore(root/'memory.db'))
'''


class ActionToolGuardTests(unittest.TestCase):
    def run_isolated(self, body):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "nova")
        code = _PRELUDE + "\n" + textwrap.dedent(body)
        run = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + "\n" + run.stderr)

    def test_browser_form_is_not_filled_before_approval(self):
        self.run_isolated('''
with tempfile.TemporaryDirectory() as td:
    tools=make(Path(td)); tools.browser_agent=Browser()
    result=tools.execute_tool('browser_fill', {'target':'email','text':'private','submit':True})
    assert result['error']=='approval_ui_unavailable', result
    assert tools.browser_agent.locator.fills==[] and tools.browser_agent.locator.keys==[]
''')

    def test_approved_submit_runs_once_and_enter_always_reprompts(self):
        self.run_isolated('''
with tempfile.TemporaryDirectory() as td:
    tools=make(Path(td)); tools.browser_agent=Browser(); prompts=[]
    tools.action_broker.set_approval_handler(lambda row:(prompts.append(row),tools.action_broker.approve(row['request_id'])))
    result=tools.execute_tool('browser_fill', {'target':'email','text':'private','submit':True})
    assert result['ok'] and tools.browser_agent.locator.fills==['private'] and tools.browser_agent.locator.keys==['Enter']
    tools.execute_tool('browser_press', {'key':'Enter'}); tools.execute_tool('browser_press', {'key':'Enter'})
    assert sum(1 for row in prompts if row['tool']=='browser_press')==2
''')

    def test_neutral_selector_submit_and_formaction_are_high_risk_even_trusted(self):
        self.run_isolated('''
with tempfile.TemporaryDirectory() as td:
    for attrs in (
        {'tag':'button','type':'submit','effective_type':'submit','form_associated':True,'may_submit':True},
        {'tag':'button','type':'button','effective_type':'button','formaction':'/send','form_associated':True},
    ):
        tools=make(Path(td),profile='trusted'); tools.browser_agent=Browser(attrs); prompts=[]
        def approve(row):
            prompts.append(row); assert row['risk']=='high' and not row['allow_task_grant']; tools.action_broker.approve(row['request_id'])
        tools.action_broker.set_approval_handler(approve)
        result=tools.execute_tool('browser_click', {'target':'#next'})
        assert result['ok'] and len(prompts)==1 and tools.browser_agent.locator.clicks==1, (result,prompts)
''')

    def test_failed_dom_inspection_never_auto_authorizes(self):
        self.run_isolated('''
with tempfile.TemporaryDirectory() as td:
    tools=make(Path(td),profile='trusted'); tools.browser_agent=Browser(fail_inspection=True)
    result=tools.execute_tool('browser_click', {'target':'#next'})
    assert result['error']=='approval_ui_unavailable', result
    assert tools.browser_agent.locator.clicks==0
''')

    def test_task_grant_does_not_cover_later_submit(self):
        self.run_isolated('''
with tempfile.TemporaryDirectory() as td:
    tools=make(Path(td),profile='balanced'); tools.action_task_id='task-a'; tools.browser_agent=Browser({'tag':'a','href':'/docs','form_associated':False,'may_submit':False}); prompts=[]
    def approve(row):
        prompts.append(row)
        tools.action_broker.approve(row['request_id'],mode='task' if len(prompts)==1 else 'once')
    tools.action_broker.set_approval_handler(approve)
    first=tools.execute_tool('browser_click', {'target':'#next'})
    tools.browser_agent=Browser({'tag':'button','type':'submit','effective_type':'submit','form_associated':True,'may_submit':True})
    second=tools.execute_tool('browser_click', {'target':'#next'})
    assert first['ok'] and second['ok'] and len(prompts)==2
    assert prompts[0]['allow_task_grant'] and not prompts[1]['allow_task_grant']
    assert tools.browser_agent.locator.clicks==1
''')

    def test_write_approval_precedes_directory_backup_and_write(self):
        self.run_isolated('''
with tempfile.TemporaryDirectory() as td:
    root=Path(td); tools=make(root); new=root/'new'/'file.txt'
    result=tools.execute_tool('write_file', {'path':str(new),'content':'new'})
    assert result['error']=='approval_ui_unavailable' and not new.parent.exists()
    target=root/'existing.txt'; target.write_text('old',encoding='utf-8')
    tools.action_broker.set_approval_handler(lambda row:tools.action_broker.approve(row['request_id']))
    result=tools.execute_tool('write_file', {'path':str(target),'content':'new'})
    assert result['ok'] and target.read_text(encoding='utf-8')=='new'
    assert Path(result['backup']).read_text(encoding='utf-8')=='old'
''')

    def test_forbidden_powershell_never_invokes_subprocess(self):
        self.run_isolated(r'''
with tempfile.TemporaryDirectory() as td:
    tools=make(Path(td))
    import assistant.tools as tools_module
    calls=[]
    tools_module.subprocess.run=lambda *a,**k: calls.append((a,k)) or (_ for _ in ()).throw(AssertionError('subprocess invoked'))
    commands=[
        r'rm C:\Users', r"&('Remove'+'-Item') C:\Users", r'cmd /c del C:\Users',
        r'powershell -EncodedCommand AAAA', r'Get-Date; Remove-Item C:\Users',
        r'Get-Process | Remove-Item', r'Start-Process cmd.exe', r'Format-Volume C',
        r'bcdedit /deletevalue safeboot', r'Set-MpPreference -DisableRealtimeMonitoring $true',
        r'Get-Credential', r'Stop-Computer', r'shutdown /s',
    ]
    for command in commands:
        result=tools.execute_tool('powershell', {'command':command})
        assert result['error']=='forbidden_action', (command,result)
    assert calls==[] and tools.action_broker.pending()==[]
''')


if __name__ == '__main__':
    unittest.main()
