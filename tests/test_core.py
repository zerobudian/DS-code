import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from src.core.config import Config
from src.core.deepseek_api import DeepSeekAPI
from src.core.chat_engine import ChatEngine
from src.sandbox.history import ConversationHistory
from sidecar.service import SidecarService


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        Config._instance = None
        Config._BASE_DIR = Path(self.temp.name)
        Config._CONFIG_FILE = Config._PROJECTS_DIR = None
        self.cfg = Config()
    def tearDown(self):
        self.cfg._flush_pending_save()
        Config._instance = None
        Config._BASE_DIR = Config._CONFIG_FILE = Config._PROJECTS_DIR = None
        self.temp.cleanup()
    def test_active_provider_update_persists_and_encrypts(self):
        self.cfg.data['providers'] = [{'model':'old', 'base_url':'https://old.example', 'api_key':'old-key'}]
        self.cfg.update_settings({'model':'new', 'base_url':'https://new.example/v1', 'api_key':'test-key-for-regression'})
        self.assertEqual(self.cfg.model, 'new')
        self.assertEqual(self.cfg.base_url, 'https://new.example/v1')
        self.assertEqual(self.cfg.api_key, 'test-key-for-regression')
        disk = self.cfg.CONFIG_FILE.read_text()
        self.assertNotIn('test-key-for-regression', disk)
        Config._instance = None
        self.assertEqual(Config().api_key, 'test-key-for-regression')
    def test_failed_save_rolls_back_memory(self):
        previous = copy.deepcopy(self.cfg.data)
        with patch.object(self.cfg, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.cfg.update_settings({'model':'new'})
        self.assertEqual(self.cfg.data, previous)
    def test_invalid_config_does_not_partially_change(self):
        from sidecar.http_protocol import HTTPError
        rt = NS(cfg=self.cfg, lock=__import__('threading').RLock(), require_idle=lambda:None)
        svc = SidecarService(rt)
        old = self.cfg.model
        with self.assertRaises(HTTPError): svc.update_config({'model':'new', 'permission_mode':'invalid'})
        self.assertEqual(self.cfg.model, old)
        with self.assertRaises(HTTPError): svc.update_config({'api_key':'********'})
    def test_custom_models_preserve_context_flag(self):
        rt = NS(cfg=self.cfg, lock=__import__('threading').RLock(), require_idle=lambda:None)
        svc = SidecarService(rt)
        data = svc.update_config({'model':'custom', 'custom_models':[{'name':'custom', 'context1m':True}]})
        self.assertEqual(data['available_models'][0]['name'], 'custom')
        self.assertTrue(data['custom_models'][0]['context_1m'])
    def test_client_switch_does_not_close_replacement_or_diagnostics(self):
        self.cfg.update_settings({'api_key':'fake-test-key', 'base_url':'http://127.0.0.1:1234/v1'})
        api = DeepSeekAPI()
        try:
            first = api._get_client()
            diagnostics = api._get_http_session()
            self.cfg.update_settings({'base_url':'http://127.0.0.1:1235/v1'})
            second = api._get_client()
            self.assertTrue(first.is_closed())
            self.assertFalse(second.is_closed())
            self.assertFalse(diagnostics.is_closed)
        finally: api.close()
    def test_official_balance_currency_selection(self):
        data = DeepSeekAPI._normalize_balance({'is_available':True,'balance_infos':[
            {'currency':'USD','total_balance':'3.20'}, {'currency':'CNY','total_balance':'15.30'}]})
        self.assertEqual(data['available_balance'], 15.30)
        self.assertEqual(data['currency'], 'CNY')
    def test_native_tool_fragments_survive_usage_chunk(self):
        class Stream:
            def __iter__(self):
                def delta(name='', args='', id=None):
                    return NS(usage=None, choices=[NS(delta=NS(content=None, tool_calls=[NS(index=0,id=id,function=NS(name=name,arguments=args))]))])
                yield delta('read_', '{"path":', 'call_native')
                yield delta('file', '"hello.py"}')
                yield NS(choices=[], usage=NS(prompt_tokens=10, completion_tokens=5, total_tokens=15))
            def close(self): pass
        client = NS(chat=NS(completions=NS(create=lambda **kw:Stream())))
        result = DeepSeekAPI()._chat_stream_once(client, 'test-model', [], 'high')
        self.assertEqual(result['tool_calls'][0]['function'], {'name':'read_file','arguments':'{"path":"hello.py"}'})
        self.assertEqual(result['tool_calls'][0]['id'], 'call_native')
        self.assertEqual(result['total_tokens'], 15)
    def test_clear_really_clears_persistent_history(self):
        history = ConversationHistory(Path(self.temp.name)/'history')
        history.add_user_message('old message')
        engine = ChatEngine()
        engine.history = history
        engine._cmd_clear([])
        self.assertEqual(ConversationHistory(history.dir).messages, [])
        engine.stop()
    def test_cancellation_before_send_does_not_execute(self):
        import threading
        event = threading.Event(); event.set()
        engine = ChatEngine()
        with patch.object(engine, '_send_message_impl') as send:
            engine.send_message('hello', cancel_event=event)
            send.assert_not_called()
        engine.stop()

    def test_api_history_omits_internal_fields_and_legacy_tool_calls(self):
        history = ConversationHistory(Path(self.temp.name)/'history')
        history.add_assistant_message('', '', [{'index':0,'id':'call_1','type':'function',
            'tool':'create_file','filename':'hello.py','function':{'name':'create_file','arguments':'{}'}}])
        messages=history.to_api_messages('system')
        self.assertNotIn('id',messages[0])
        call=messages[1]['tool_calls'][0]
        self.assertEqual(set(call),{'id','type','function'})
        self.assertIn('reasoning_content',messages[1])
        history.clear()
        history.add_assistant_message('legacy tool text','',[{'tool':'read_file','path':'hello.py'}])
        self.assertNotIn('tool_calls',history.to_api_messages('system')[1])
    def test_history_snapshot_is_detached(self):
        history = ConversationHistory(Path(self.temp.name)/'history')
        history.add_assistant_message('hello','',[{'tool':'read_file'}])
        snapshot=history.snapshot();snapshot[0]['tool_calls'][0]['tool']='changed'
        self.assertEqual(history.messages[0]['tool_calls'][0]['tool'],'read_file')
