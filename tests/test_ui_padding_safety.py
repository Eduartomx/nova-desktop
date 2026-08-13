from __future__ import annotations

import ast
import unittest
from pathlib import Path


TK_WIDGETS = {
    "Frame", "Label", "Button", "Text", "Entry", "Listbox", "Canvas",
    "Checkbutton", "Radiobutton", "Scale", "Scrollbar", "Spinbox",
}


class TkPaddingSafetyTests(unittest.TestCase):
    def test_widget_constructor_padding_is_never_a_tuple(self):
        assistant_dir = Path(__file__).resolve().parents[1] / "nova" / "assistant"
        offenders: list[str] = []
        for path in sorted(assistant_dir.glob("ui*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "tk"
                    and func.attr in TK_WIDGETS
                ):
                    continue
                for keyword in node.keywords:
                    if keyword.arg in {"padx", "pady"} and isinstance(keyword.value, (ast.Tuple, ast.List)):
                        offenders.append(f"{path.name}:{node.lineno}:{func.attr}:{keyword.arg}")
        self.assertEqual(offenders, [], "Padding asimétrico debe ir en pack/grid, no en el constructor Tk")


if __name__ == "__main__":
    unittest.main()
