import threading
import unittest
from types import SimpleNamespace
from sidecar.http_protocol import HTTPError
from sidecar.runtime import ChatRuntime
from src.core.confirmation import ConfirmationGate


class Projects:
    def __init__(self):
        self.names = {}
    def _safe_project_name(self, name):
        return name.replace('-', '_') if isinstance(name, str) and name and '/' not in name else None
    def list_projects(self):
        return list(self.names)
    def get(self, name):
        return self.names.get(name)
    def create(self, name):
        sb = SimpleNamespace(name=name)
        self.names[name] = sb
        return sb


class Engine:
    def __init__(self):
        self.sandbox = None
        self.mode = 'normal'
        self._gate = ConfirmationGate()
        self.started = threading.Event()
        self.release = threading.Event()
        self.after_confirmation = threading.Event()
        self.confirmed = None
    def set_project(self, sb): self.sandbox = sb
    def set_mode(self, mode): self.mode = mode
    def get_mode(self): return self.mode
    def send_message(self, text, cancel_event=None, images=None):
        self.started.set()
        if text == 'confirm':
            self.confirmed = self._gate.wait_for_command_confirmation({'command': 'echo hello'}, self.on_confirm_command)
            self.after_confirmation.set()
        else:
            self.release.wait(2)
            self.on_content_delta(text)
        self.on_finished(1, 50, 3, 6)
    def submit_command_confirmation(self, value): self._gate.submit_command_confirmation(value)
    def confirm_edit(self, value): self._gate.confirm_edit(value)
    def submit_user_answer(self, value): self._gate.submit_user_answer(value)
    def cancel_pending_confirmations(self): self._gate.cancel_all()
    def stop(self):
        self._gate.cancel_all()
        self.release.set()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.runtime = ChatRuntime(SimpleNamespace(get=lambda key, default: default), Projects(), self.engine)
    def tearDown(self):
        self.runtime.stop()
        if self.runtime.worker: self.runtime.worker.join(3)
    def test_busy_request_cannot_switch_project_or_clear_events(self):
        run = self.runtime.start('project-a', 'first')
        self.assertTrue(self.engine.started.wait(1))
        with self.assertRaises(HTTPError) as error:
            self.runtime.start('project-b', 'second')
        self.assertEqual(error.exception.status, 409)
        self.engine.release.set()
        self.assertTrue(run.finished.wait(2))
        events = list(run.events.queue)
        self.assertIn(('content', 'first'), events)
        self.assertEqual(run.project, 'project_a')
        self.assertNotIn('project_b', self.runtime.projects.names)
        self.assertEqual([e[0] for e in events].count('done'), 1)
    def test_stream_has_one_consumer(self):
        run = self.runtime.start('demo', 'first')
        self.assertIs(self.runtime.attach(run.id), run)
        with self.assertRaises(HTTPError): self.runtime.attach(run.id)
    def test_confirmation_is_scoped_and_stop_unblocks(self):
        run = self.runtime.start('demo', 'confirm')
        kind, event = run.events.get(timeout=2)
        self.assertEqual(kind, 'confirm_command')
        with self.assertRaises(HTTPError):
            self.runtime.submit('stale-run', 'command', event['request_id'], True)
        self.runtime.submit(run.id, 'command', event['request_id'], False)
        self.assertTrue(run.finished.wait(2))
        self.assertFalse(self.engine.confirmed)
        with self.assertRaises(HTTPError):
            self.runtime.submit(run.id, 'command', event['request_id'], True)
    def test_stop_cannot_target_next_run(self):
        first = self.runtime.start('demo', 'confirm')
        first.events.get(timeout=2)
        self.assertTrue(self.runtime.stop(first.id))
        self.assertTrue(first.finished.wait(2))
        second = self.runtime.start('demo', 'confirm')
        second.events.get(timeout=2)
        self.assertFalse(self.runtime.stop(first.id))
        self.assertIs(self.runtime.active, second)
        self.runtime.stop(second.id)
        self.assertTrue(second.finished.wait(2))
    def test_invalid_project_rejected(self):
        with self.assertRaises(HTTPError): self.runtime.start('../escape', 'text')

class BufferTests(unittest.TestCase):
    def test_unconsumed_events_are_bounded_and_preserve_terminal_error(self):
        from sidecar.runtime import ChatRun
        run=ChatRun('demo')
        for _ in range(4100):run.emit('content','chunk')
        self.assertLessEqual(run.events.qsize(),4096)
        self.assertTrue(run.cancel_event.is_set())
        run.emit('done',{})
        self.assertEqual(list(run.events.queue)[-1][0],'srv_error')
