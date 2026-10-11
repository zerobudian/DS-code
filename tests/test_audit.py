"""Cross-module regression contracts from the full repository audit."""
import base64
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from src.core.atomic_files import atomic_write_bytes
from src.core.config import Config
from src.core.deepseek_api import APIError, DeepSeekAPI
from src.core.hooks import Hook, HookManager
from src.core.memory_manager import MemoryManager
from src.core.prompts.tool_descriptions import TOOL_DESCRIPTIONS
from src.sandbox.project_manager import ProjectManager, ProjectSandbox
from src.sandbox.history import ConversationHistory
from src.tools.tool_executor import ToolExecutor
from sidecar.http_protocol import HTTPError, Request
from sidecar.project_data import ProjectDataService, prepare_attachments

ROOT = Path(__file__).resolve().parent.parent


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        Config._instance = None
        Config._BASE_DIR = Path(self.temp.name)
        Config._CONFIG_FILE = Config._PROJECTS_DIR = None
        self.cfg = Config()
        self.cfg.update_settings({'permission_mode': 'full'})
        self.manager = ProjectManager()
        self.project = self.manager.create('audit')

    def tearDown(self):
        self.cfg._flush_pending_save()
        Config._instance = None
        Config._BASE_DIR = Config._CONFIG_FILE = Config._PROJECTS_DIR = None
        self.temp.cleanup()

    def test_pin_and_failed_metadata_save_report_actual_result(self):
        self.assertTrue(self.project.set_pinned(True))
        self.assertTrue(self.manager.get('audit').is_pinned())
        with patch('src.sandbox.project_manager._atomic_write_json', return_value=False):
            self.assertFalse(self.project.set_pinned(False))
            self.assertFalse(self.project.set_display_name('unsaved'))
        self.assertTrue(self.project.is_pinned())

    def test_task_completion_matches_exactly_and_preserves_other_tasks(self):
        self.project.add_pending_task('fix api')
        self.project.add_pending_task('fix api timeout')
        self.assertTrue(self.project.move_task_to_completed('fix api'))
        self.assertEqual(self.project.get_tasks(), [
            {'text': 'fix api timeout', 'done': False}, {'text': 'fix api', 'done': True}])
        old = self.project.read_ds_md()
        with patch.object(self.project, 'write_ds_md', return_value=False):
            self.assertFalse(self.project.move_task_to_completed('fix api timeout'))
        self.assertEqual(self.project.read_ds_md(), old)

    @unittest.skipUnless(os.name == 'posix', 'symlink contract')
    def test_project_and_internal_symlinks_do_not_modify_outside(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside)
            (self.manager.projects_root / 'linked').symlink_to(external, target_is_directory=True)
            self.assertIsNone(self.manager.get('linked'))
            self.assertIsNone(self.manager.create('linked'))
            self.assertNotIn('linked', self.manager.list_projects())
            self.assertEqual(list(external.iterdir()), [])
            victim = external / 'victim'; victim.write_text('unchanged')
            self.project.meta_file.unlink(); self.project.meta_file.symlink_to(victim)
            self.assertFalse(self.project.update_meta({'pinned': True}))
            self.assertEqual(victim.read_text(), 'unchanged')

    def test_existing_project_rename_is_never_overwritten(self):
        self.manager.create('other')
        self.assertFalse(self.manager.rename('audit', 'other'))
        self.assertEqual(set(self.manager.list_projects()), {'audit', 'other'})

    def test_utf8_write_limit_counts_bytes(self):
        self.project._MAX_WRITE_BYTES = 5
        self.assertFalse(self.project.write_file('large.txt', '中文'))
        self.assertTrue(self.project.write_file('small.txt', 'abc'))

    def test_atomic_writer_failure_preserves_file_and_removes_temp(self):
        target = self.project.work_dir / 'preserved.txt'; target.write_text('old')
        with patch('src.core.atomic_files.os.replace', side_effect=OSError('disk failure')):
            self.assertFalse(atomic_write_bytes(target, b'new'))
        self.assertEqual(target.read_text(), 'old')
        self.assertFalse(list(target.parent.glob('.tmp_*')))

    def test_atomic_writer_portable_branch(self):
        target = self.project.work_dir / 'portable.txt'
        with patch('src.core.atomic_files.os.supports_dir_fd', set()):
            self.assertTrue(atomic_write_bytes(target, b'portable'))
        self.assertEqual(target.read_bytes(), b'portable')

    def test_memory_snapshots_are_detached_and_failed_update_rolls_back(self):
        memory = MemoryManager(Path(self.temp.name) / 'memory')
        id_ = memory.add('audit memory', 'content', tags=['first'], metadata={'nested': [1]})
        for item in (memory.get(id_), memory.list_all()[0], memory.search('audit')[0]):
            item['tags'].append('mutated'); item['metadata']['nested'].append(2)
        self.assertEqual(memory.get(id_)['tags'], ['first'])
        self.assertEqual(memory.get(id_)['metadata']['nested'], [1])
        old = memory.get(id_)
        with patch.object(memory, '_write_json', return_value=False):
            self.assertFalse(memory.update(id_, content='unsaved'))
        self.assertEqual(memory.get(id_), old)

    def test_nonzero_blocking_hook_cannot_be_ignored(self):
        hooks = HookManager(self.project.work_dir, Path(self.temp.name) / 'config')
        hooks.register(Hook('deny', 'PreToolUse', 'command', {'command': 'exit 1'}, can_block=True))
        blocked, reason, _ = hooks.trigger_blocking('PreToolUse', tool_name='create_file')
        self.assertTrue(blocked); self.assertIn('deny', reason)
        executor = ToolExecutor(self.project, hook_manager=hooks)
        ok, _ = executor.execute({'tool': 'create_file', 'filename': 'blocked.py', 'content': 'print(1)'})
        self.assertFalse(ok); self.assertFalse((self.project.work_dir / 'blocked.py').exists())

    def test_reviewer_blocks_all_mutating_git_operations(self):
        self.cfg.update_settings({'permission_mode': 'reviewer'})
        executor = ToolExecutor(self.project)
        for tool in ('git_add', 'git_branch_create', 'git_branch_delete', 'git_reset', 'git_stash'):
            with self.subTest(tool=tool):
                ok, text = executor.execute({'tool': tool})
                self.assertFalse(ok); self.assertIn('reviewer', text)

    def test_all_registered_routes_and_native_descriptions_are_valid(self):
        executor = ToolExecutor(self.project)
        for name, (owner, method, _, _) in executor._TOOL_ROUTES.items():
            with self.subTest(tool=name):
                self.assertIn(name, executor.TOOL_NAMES)
                self.assertTrue(callable(getattr(getattr(executor, owner), method)))
        names = [item['name'] for item in TOOL_DESCRIPTIONS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), executor.TOOL_NAMES)
        native = DeepSeekAPI._to_openai_tools(TOOL_DESCRIPTIONS)
        self.assertEqual(len(native), len(names))
        for item in native:
            parameters = item['function']['parameters']
            self.assertTrue(set(parameters.get('required', [])) <= set(parameters['properties']))
        self.assertFalse(executor.execute({'tool': []})[0])

    def test_tool_parser_handles_field_order_escaped_code_and_invalid_objects(self):
        import random
        import string
        executor = ToolExecutor(self.project)
        randomizer = random.Random(42)
        for _ in range(200):
            content = ''.join(randomizer.choice(string.ascii_letters+'{}\\"\n\t') for _ in range(200))
            call = {'filename':'code.py', 'content':content, 'tool':'create_file'}
            self.assertEqual(executor.extract_tool_calls('```json\n'+json.dumps(call)+'\n```'), [call])
        self.assertEqual(executor.extract_tool_calls('{"tool":[]}'), [])
        self.assertEqual(executor.extract_tool_calls('{"tool":"create_file","filename":"partial.py","content":"oops}'), [])
        large = {'tool':'create_file', 'filename':'large.py', 'content':'x'*200000}
        self.assertEqual(executor.extract_tool_calls(json.dumps(large)), [large])

    def test_provider_add_switch_delete_and_credential_redaction(self):
        from sidecar.service import SidecarService
        runtime = NS(cfg=self.cfg, projects=self.manager, lock=threading.RLock(), require_idle=lambda:None)
        service = SidecarService(runtime)
        def request(method, path, body=None):
            return service.route(Request(method,path,{}, {},json.dumps(body).encode() if body else b''))
        self.cfg.update_settings({'api_key':'first-test-key','model':'first-model'})
        added = request('POST','/api/providers',{'name':'Second', 'api_key':'second-test-key',
            'model':'second-model','base_url':'https://second.example/v1'})
        self.assertEqual(len(added['providers']), 2)
        self.assertNotIn('second-test-key', json.dumps(added))
        request('PUT','/api/providers/1',{'active':True})
        self.assertEqual(self.cfg.api_key,'second-test-key')
        self.assertEqual(self.cfg.model,'second-model')
        self.assertNotIn('second-test-key',self.cfg.CONFIG_FILE.read_text())
        request('DELETE','/api/providers/1')
        self.assertEqual(self.cfg.api_key,'first-test-key')
        self.assertEqual(self.cfg.model,'first-model')
        with self.assertRaises(HTTPError):request('POST','/api/providers',{'name':'bad','api_key':'********'})

    def test_config_nested_snapshots_and_defaults_do_not_share_state(self):
        providers = [{'name':'One','model':'original','api_key':'nested-test-key','base_url':'https://test.example'}]
        self.cfg.update_settings({'providers':providers})
        self.cfg.get('providers')[0]['model']='mutated'
        self.assertEqual(self.cfg.model,'original')
        self.cfg.get('subagent')['default_model']='mutated'
        self.assertNotEqual(self.cfg.get('subagent')['default_model'],'mutated')

    def test_legacy_custom_model_metadata_is_read_without_key_error(self):
        from sidecar.service import SidecarService
        self.cfg.update_settings({'custom_models': [{'name':'legacy', 'context1m':True}, None]})
        service = SidecarService(NS(cfg=self.cfg, projects=self.manager))
        self.assertEqual(service.config()['custom_models'], [{'name':'legacy', 'context_1m':True}])
        self.assertTrue(self.cfg.get('custom_models')[0]['context1m'])

    def test_legacy_property_and_batch_setters_update_active_provider(self):
        self.cfg.update_settings({'providers':[{'name':'active','model':'old','api_key':'old-test-key'}]})
        self.cfg.model = 'new-model'
        self.cfg.api_key = 'new-test-key'
        self.cfg.set('base_url', 'https://new.example/v1')
        self.assertEqual(self.cfg.model, 'new-model')
        self.assertEqual(self.cfg.get_api_key(), 'new-test-key')
        self.assertEqual(self.cfg.base_url, 'https://new.example/v1')
        self.cfg.update(model='batch-model')
        self.assertEqual(self.cfg.model, 'batch-model')
        self.assertNotIn('new-test-key', self.cfg.CONFIG_FILE.read_text())

    def test_provider_switch_does_not_inherit_credentials_or_capabilities(self):
        self.cfg.update_settings({'api_key':'fallback-test-key', 'provider_capabilities':{'vision':False},
            'custom_models':[{'name':'first','context_1m':False}], 'context_tokens':4000,
            'providers':[{'name':'first','api_key':'first-test-key','provider_capabilities':{'vision':False},'context_tokens':4000},
                         {'name':'second','api_key':''}]})
        self.cfg.active_provider_index = 1
        self.assertEqual(self.cfg.get_api_key(), '')
        self.assertEqual(self.cfg.get('provider_capabilities'), {})
        self.assertEqual(self.cfg.get('custom_models'), [])
        self.assertEqual(self.cfg.get('context_tokens'), 128000)

    def test_provider_settings_do_not_retain_callers_nested_objects(self):
        self.cfg.update_settings({'providers':[{'name':'active'}]})
        values = {'provider_capabilities':{'tools':False}}
        self.cfg.update_settings(values)
        values['provider_capabilities']['tools'] = True
        self.assertFalse(self.cfg.get('provider_capabilities')['tools'])

    def test_new_sessions_in_same_second_do_not_overwrite(self):
        with patch('src.sandbox.history.time.strftime', return_value='20261011_120000'):
            first = ConversationHistory(self.project.history_dir, skip_load=True)
            second = ConversationHistory(self.project.history_dir, skip_load=True)
        self.assertNotEqual(first.current_session_file, second.current_session_file)
        first.add_user_message('first'); second.add_user_message('second')
        self.assertEqual(json.loads(first.current_session_file.read_text())['messages'][0]['content'], 'first')

    def test_history_inputs_and_returned_messages_are_detached(self):
        history = ConversationHistory(self.project.history_dir, skip_load=True)
        calls = [{'id':'one','function':{'name':'read_file','arguments':'{}'}}]
        returned = history.add_assistant_message('test', tool_calls=calls)
        calls[0]['function']['name'] = 'mutated'
        returned['tool_calls'][0]['id'] = 'mutated'
        self.assertEqual(history.snapshot()[0]['tool_calls'][0], {'id':'one','function':{'name':'read_file','arguments':'{}'}})
        messages = history.snapshot(); history.replace_messages(messages)
        messages[0]['tool_calls'][0]['id'] = 'mutated'
        self.assertEqual(history.snapshot()[0]['tool_calls'][0]['id'], 'one')

    def test_failed_history_save_keeps_previous_file_and_dirty_state(self):
        history = ConversationHistory(self.project.history_dir, skip_load=True)
        history.add_user_message('saved')
        before = history.current_session_file.read_bytes()
        with patch('src.core.atomic_files.os.replace', side_effect=OSError('disk failure')):
            history.add_user_message('pending')
        self.assertEqual(history.current_session_file.read_bytes(), before)
        self.assertTrue(history._dirty)
        history.save()
        self.assertFalse(history._dirty)
        self.assertEqual(len(json.loads(history.current_session_file.read_text())['messages']), 2)

    @unittest.skipUnless(os.name == 'posix', 'POSIX shell contract')
    def test_tool_executor_shutdown_closes_shell_and_read_does_not_start_it(self):
        executor = ToolExecutor(self.project)
        ok, text = executor.execute({'tool':'shell_read'})
        self.assertTrue(ok); self.assertIn('没有运行中的shell', text)
        self.assertIsNone(getattr(executor,'_persistent_terminal',None))
        self.assertTrue(executor.execute({'tool':'shell_start'})[0])
        terminal = executor._persistent_terminal
        executor.shutdown(); executor.shutdown()
        self.assertFalse(terminal.is_running())
        self.assertFalse(executor.execute({'tool':'calculator','expression':'1+1'})[0])

    def test_file_tool_and_snapshot_roundtrip(self):
        executor = ToolExecutor(self.project)
        executor.on_edit_apply_request = lambda *args: True
        ok, text = executor.execute({'tool': 'create_file', 'filename': 'sample.py', 'content': 'value = 1\n'})
        self.assertTrue(ok, text)
        snap = self.project.undo_manager.create_snapshot('before edit')
        self.assertTrue(snap)
        self.assertTrue(self.project.write_file('sample.py', 'value = 2\n'))
        self.assertTrue(self.project.undo_manager.restore_snapshot(snap))
        self.assertEqual(self.project.read_file('sample.py'), 'value = 1\n')
        ok, text = executor.execute({'tool': 'read_file', 'filename': '../outside'})
        self.assertFalse(ok, text)

    def test_custom_provider_does_not_receive_deepseek_only_fields(self):
        self.cfg.update_settings({'base_url': 'https://example.test/v1', 'model': 'generic-model'})
        kwargs = DeepSeekAPI()._request_kwargs('generic-model', [], 'high', True)
        self.assertNotIn('reasoning_effort', kwargs); self.assertNotIn('extra_body', kwargs)
        self.cfg.update_settings({'provider_capabilities': {'tools': False, 'stream_usage': False}})
        kwargs = DeepSeekAPI()._request_kwargs('generic-model', [], 'high', True)
        self.assertNotIn('tools', kwargs); self.assertNotIn('stream_options', kwargs)

    def test_partial_stream_failure_is_not_retryable(self):
        class Stream:
            closed = False
            def __iter__(self):
                yield NS(usage=None, choices=[NS(delta=NS(content='already shown'))])
                raise ConnectionError('network interrupted')
            def close(self): self.closed = True
        stream = Stream(); seen = []
        with self.assertRaises(APIError) as error:
            DeepSeekAPI()._consume_stream(stream, 'generic', on_text=seen.append)
        self.assertFalse(error.exception.need_retry)
        self.assertEqual(seen, ['already shown']); self.assertTrue(stream.closed)

    def test_stream_request_retries_only_before_first_delivery(self):
        api = DeepSeekAPI()
        with patch.object(api, '_get_client', return_value=object()), patch.object(
                api, '_chat_stream_once', side_effect=APIError(503, 'partial stream', need_retry=False)) as request:
            with self.assertRaises(APIError): api.chat_stream([])
            self.assertEqual(request.call_count, 1)
        with patch.object(api, '_get_client', return_value=object()), patch.object(
                api, '_chat_stream_once', side_effect=[APIError(503, 'before delivery'), {'content':'ok'}]) as request:
            api._STREAM_RETRY_BACKOFF_BASE = 0
            self.assertEqual(api.chat_stream([])['content'], 'ok')
            self.assertEqual(request.call_count, 2)

    def test_context_budget_keeps_complete_tool_exchange_and_does_not_mutate(self):
        from src.core.context_budget import fit_context
        source = [{'role':'system', 'content':'instructions'}, {'role':'user', 'content':'old'*900},
                  {'role':'assistant', 'content':'old response'}, {'role':'user', 'content':'latest'},
                  {'role':'assistant', 'content':'', 'tool_calls':[
                      {'id':'call_latest', 'type':'function', 'function':{'name':'read_file','arguments':'{}'}}]},
                  {'role':'tool', 'tool_call_id':'call_latest', 'content':'file content'}]
        saved = copy.deepcopy(source)
        result, removed = fit_context(source, 600)
        self.assertEqual(removed, 2)
        self.assertEqual(result[-1]['tool_call_id'], result[-2]['tool_calls'][0]['id'])
        self.assertEqual(source, saved)
        with self.assertRaises(ValueError): fit_context([{'role':'user','content':'huge'*900}], 1024)

    def test_context_repairs_cancelled_and_orphaned_native_results(self):
        from src.core.context_budget import complete_tool_exchanges
        calls = [{'role':'assistant','content':'','tool_calls':[
            {'id':'unfinished','type':'function','function':{'name':'create_file','arguments':'{}'}}]},
            {'role':'user','content':'next question'}, {'role':'tool','tool_call_id':'orphan','content':'old'}]
        result = complete_tool_exchanges(calls)
        self.assertEqual(result[1]['tool_call_id'], 'unfinished')
        self.assertEqual(result[-1]['role'], 'user')

    def test_code_analysis_and_keyword_index_on_real_files(self):
        from src.analysis.code_parser import CodeParser
        from src.analysis.code_vector_store import CodeVectorStore
        from src.analysis.auto_validation import AutoValidator
        self.project.write_file('maths.py', 'def square(value):\n    return value * value\n')
        parser = CodeParser(self.project.work_dir)
        self.assertIsNotNone(parser.parse_file('maths.py'))
        self.assertTrue(parser.get_symbol('square'))
        self.assertIsNone(parser.parse_file('../DS.md'))
        validator = AutoValidator(self.project.work_dir)
        self.assertTrue(validator.check_syntax(['maths.py']).passed)
        self.project.write_file('broken.py', 'def broken(:\n')
        self.assertFalse(validator.check_syntax(['broken.py']).passed)
        store = CodeVectorStore(self.project.work_dir)
        store.index(force=True)
        self.assertTrue(store.search_similar('square value'))

    @unittest.skipUnless(os.name == 'posix', 'POSIX shell contract')
    def test_persistent_shell_small_output_exit_code_and_timeout_cleanup(self):
        executor = ToolExecutor(self.project)
        try:
            self.assertTrue(executor.execute({'tool':'shell_start'})[0])
            ok, result = executor.execute({'tool':'shell_exec', 'command':'printf hello', 'timeout':2})
            self.assertTrue(ok, result); self.assertIn('hello', result)
            ok, result = executor.execute({'tool':'shell_exec', 'command':'false', 'timeout':2})
            self.assertFalse(ok, result); self.assertIn('exit=1', result)
            ok, result = executor.execute({'tool':'shell_exec', 'command':'sleep 5', 'timeout':1})
            self.assertFalse(ok, result)
            self.assertIsNone(executor.command_tools._get_terminal())
        finally: executor.execute({'tool':'shell_stop'})

    def test_project_data_tasks_revision_upload_and_safe_file_preview(self):
        runtime = NS(projects=self.manager, canonical=lambda n:n, lock=threading.RLock(),
                     require_idle=lambda:None, engine=NS(sandbox=None))
        service = ProjectDataService(runtime)
        def request(method, suffix, body=None, query=None):
            return service.route(Request(method, '/api/projects/audit'+suffix, query or {}, {},
                                         json.dumps(body).encode() if body is not None else b''))
        current = request('GET', '/tasks')
        updated = request('PUT', '/tasks', {**current, 'tasks': [{'text':'persist me', 'done':False}]})
        self.assertNotEqual(current['revision'], updated['revision'])
        with self.assertRaises(HTTPError) as error: request('PUT', '/tasks', current)
        self.assertEqual(error.exception.status, 409)
        uploaded = request('POST', '/uploads', {'name':'note.txt', 'data':base64.b64encode('真实附件'.encode()).decode()})
        text, images = prepare_attachments(self.project, [uploaded['name']])
        self.assertIn('真实附件', text); self.assertEqual(images, [])
        preview = request('GET', '/file', query={'path':[uploaded['name']], 'area':['preview']})
        self.assertEqual(preview['content'], '真实附件')
        with self.assertRaises(HTTPError): request('POST', '/uploads', {'name':'../bad', 'data':''})
        with self.assertRaises(HTTPError): request('GET', '/file', query={'path':['../DS.md']})


class ProtocolTests(unittest.TestCase):
    def test_lsp_uri_roundtrip_and_real_transport(self):
        from src.core.lsp_client import LSPClient
        with tempfile.TemporaryDirectory(prefix='space # 中文 ') as folder:
            client = LSPClient.from_path(folder)
            self.assertIn('%20', client.root_uri)
            self.assertEqual(client.uri_to_path(client.root_uri), str(Path(folder).resolve()))
            try:
                self.assertTrue(client.start_server('fixture', [sys.executable, str(ROOT/'tests/fixtures/lsp_server.py')]))
                self.assertTrue(client.initialize())
                self.assertTrue(client.is_running)
            finally: client.shutdown()
            self.assertFalse(client.is_running)

    def test_real_mcp_handshake_call_restart_and_cleanup(self):
        from src.mcp.client import MCPClient, _MCP_AVAILABLE
        if not _MCP_AVAILABLE: self.skipTest('Install requirements-optional.lock for MCP contract')
        client = MCPClient('fixture', sys.executable, [str(ROOT/'tests/fixtures/mcp_server.py')])
        try:
            result = client.start(timeout=10)
            self.assertTrue(result['ok'], result)
            self.assertEqual(client.list_tools()[0]['name'], 'echo')
            result = client.call_tool('echo', {'text': '中文'}, timeout=5)
            self.assertTrue(result['ok'], result); self.assertIn('echo:中文', result['content'])
            client.stop()
            self.assertFalse(client.is_running)
            self.assertTrue(client.start(timeout=10)['ok'])
        finally: client.stop()
        self.assertFalse(client.is_running)
        self.assertIsNone(client._loop)
