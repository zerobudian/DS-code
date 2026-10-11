const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { ChatClient } = require('../renderer/js/chat-client');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync('renderer/js/utils.js', 'utf8'), context);
class Events {
  constructor() { this.handlers = {}; this.closed = false; }
  addEventListener(type, callback) { this.handlers[type] = callback; }
  close() { this.closed = true; }
  emit(type, data) { this.handlers[type]?.({data: JSON.stringify(data)}); }
}
test('Markdown preserves fences, escapes once, and keeps markup out of code', () => {
  const html = context.renderMarkdown('**Hello**\n```js\nconst x = "<tag>";\n**literal**\n```\n`**inline**`');
  assert.match(html, /<strong>Hello<\/strong><br><pre/);
  assert.match(html, /const x = &quot;&lt;tag&gt;&quot;;\n\*\*literal\*\*/);
  assert.match(html, /<code class="inline-code">\*\*inline\*\*<\/code>/);
  assert.doesNotMatch(html, /&amp;lt;/);
});
test('Unclosed streamed fence remains a code block and HTML is escaped', () => {
  assert.match(context.renderMarkdown('```html\n<script>alert(1)</script>'), /<pre.*&lt;script&gt;/);
});
test('Server error closes stream exactly once, prevents reconnect and releases busy state', async () => {
  const stream = new Events(); let errors = 0;
  const client = new ChatClient(async () => ({run_id:'run1'}), () => stream);
  await client.start('/api/chat/send', {text:'hello'}, {srv_error:()=>errors++}, id=>id);
  stream.emit('srv_error', {message:'failure'}); stream.emit('error');
  assert.equal(errors, 1); assert.equal(stream.closed, true); assert.equal(client.active, null);
});
test('Duplicate send and stale events cannot affect the next run', async () => {
  const streams = []; let content = '';
  const client = new ChatClient(async () => ({run_id:String(streams.length)}), () => {const s=new Events();streams.push(s);return s;});
  await client.start('/api/chat/send', {}, {content:d=>content+=d}, id=>id);
  await assert.rejects(client.start('/api/chat/send', {}, {}, id=>id));
  streams[0].emit('done', {});
  await client.start('/api/chat/send', {}, {content:d=>content+=d}, id=>id);
  streams[0].emit('content', 'old'); streams[1].emit('content', 'new');
  assert.equal(content, 'new');
});
test('Stop while POST is pending cancels only the run that POST creates', async () => {
  let release; const calls=[]; const stream=new Events();
  const client = new ChatClient((path,method,body) => {
    calls.push([path, body]);
    if(path==='/api/chat/send')return new Promise(resolve=>{release=resolve;});
    return Promise.resolve({stopped:true});
  }, () => stream);
  const started=client.start('/api/chat/send', {text:'hello'}, {}, id=>id);
  await client.stop(); release({run_id:'pending-run'}); await started;
  assert.deepEqual(calls[1], ['/api/chat/stop', {run_id:'pending-run'}]);
  assert.ok(client.active); stream.emit('done', {stopped:true}); assert.equal(client.active, null);
});
test('Late dialog result cannot confirm a different run', async () => {
  let calls=0; const stream=new Events();
  const client = new ChatClient(async()=>{calls++;return {run_id:'current'};},()=>stream);
  await client.start('/api/chat/send',{}, {},id=>id);
  await client.confirm('command','question',true,'old'); assert.equal(calls,1);
});
