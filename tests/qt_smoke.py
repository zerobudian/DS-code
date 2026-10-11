"""Optional legacy Qt smoke and import sweep, isolated from user configuration."""
import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'engine'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

with tempfile.TemporaryDirectory() as directory:
    os.environ['DSCODE_DATA_DIR'] = directory
    from PySide6.QtWidgets import QApplication
    application = QApplication([])
    count = 0
    for path in sorted((ROOT / 'engine/src').rglob('*.py')):
        module = '.'.join(path.relative_to(ROOT / 'engine').with_suffix('').parts)
        if path.name == '__init__.py': module = module.rsplit('.', 1)[0]
        importlib.import_module(module)
        count += 1
    from src.gui.main_window import MainWindow
    window = MainWindow()
    window.add_user_msg('Regression user message')
    assert window.add_ai_msg('```python\nprint(42)\n```') is not None
    window.add_tool_msg('read_file', 'Tool result')
    window.add_plan_msg('A small plan')
    window.set_todos([{'text':'task', 'done':False}])
    window.set_files([{'name':'hello.py', 'path':'hello.py', 'size':10}])
    window.update_status_bar('Smoke test')
    window.show(); application.processEvents()
    window.clear_chat(); window.close(); application.processEvents()
    print(f'Qt smoke passed: {count} Python modules imported; window, messages, plan, tasks and files exercised.')
