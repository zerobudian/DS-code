// A POST creates a run; its SSE connection only consumes that run's events.
class ChatClient {
  constructor(request, connect = url => new EventSource(url)) {
    this.request = request;
    this.connect = connect;
    this.active = null;
  }
  async start(path, body, handlers, streamUrl) {
    if (this.active) throw new Error('已有请求正在处理');
    const run = { id: null, stream: null, cancelled: false };
    this.active = run;
    const finish = (type, data) => {
      if (this.active !== run) return;
      run.stream?.close();
      this.active = null;
      handlers[type]?.(data);
    };
    try {
      const result = await this.request(path, 'POST', body);
      run.id = result.run_id;
      handlers.started?.(result);
      run.stream = this.connect(streamUrl(run.id));
      ['content', 'reasoning', 'tool_call', 'tool_result', 'new_turn', 'status',
        'confirm_command', 'confirm_edit', 'ask_user_questions', 'plan_ready', 'chat_clear'].forEach(type => {
        run.stream.addEventListener(type, event => {
          if (this.active !== run) return;
          try { handlers[type]?.(JSON.parse(event.data)); }
          catch (error) { finish('srv_error', { message: error.message }); }
        });
      });
      run.stream.addEventListener('done', event => finish('done', JSON.parse(event.data)));
      run.stream.addEventListener('srv_error', event => finish('srv_error', JSON.parse(event.data)));
      run.stream.addEventListener('error', () => {
        this.request('/api/chat/stop', 'POST', { run_id: run.id }).catch(() => {});
        finish('srv_error', { message: '连接已关闭' });
      });
      if (run.cancelled) await this.stop();
      return result;
    } catch (error) {
      finish('srv_error', { message: error.message });
      return null;
    }
  }
  async stop() {
    const run = this.active;
    if (!run) return;
    run.cancelled = true;
    if (run.id) await this.request('/api/chat/stop', 'POST', { run_id: run.id });
  }
  async confirm(kind, requestId, value, runId) {
    const run = this.active;
    if (!run || run.id !== runId || run.cancelled) return;
    return this.request('/api/chat/confirm', 'POST', {
      run_id: run.id, kind, request_id: requestId, value,
    });
  }
}
if (typeof module !== 'undefined') module.exports = { ChatClient };
