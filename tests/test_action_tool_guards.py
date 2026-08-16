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
    def __init__(self): self.fills=[]; self.keys=[]
    def fill(self, value): self.fills.append(value)
    def press(self, key): self.keys.append(key)
class Page: url='https://example.test/form'
class Browser:
    def __init__(self): self._page=Page(); self.locator=Locator()
    def call(self, callback, timeout=None): return callback()
    @property
    def page(self): return self._page
    def resolve(self, target): return self.locator
def make(root):
    cfg={'security':{'profile':'balanced','allowed_roots':[str(root)],'restrict_files_to_allowed_roots':True,'backup_overwritten_files':True}}
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
    assert result['error']=='waiting_for_approval', result
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

    def test_write_approval_precedes_directory_backup_and_write(self):
        self.run_isolated('''
with tempfile.TemporaryDirectory() as td:
    root=Path(td); tools=make(root); new=root/'new'/'file.txt'
    result=tools.execute_tool('write_file', {'path':str(new),'content':'new'})
    assert result['error']=='waiting_for_approval' and not new.parent.exists()
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
    result=tools.execute_tool('powershell', {'command':r'Remove-Item C:\Users -Recurse -Force'})
    assert result['error']=='forbidden_action', result
''')


if __name__ == '__main__':
    unittest.main()
