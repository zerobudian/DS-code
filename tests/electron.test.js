const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { SidecarProcess } = require('../electron/sidecar-process');
function child() {
  const c = new EventEmitter(); c.kills=[];
  c.kill=signal=>{c.kills.push(signal);setImmediate(()=>c.emit('close',0));};
  return c;
}
test('Desktop launches one owned child, forwards auth, and stops that same child', async () => {
  const c=child();let options;
  const p=new SidecarProcess({python:'python',script:'sidecar.py',cwd:'.',port:1234,token:'test-token',
    spawnProcess:(exe,args,opts)=>{options=opts;assert.deepEqual(args,['sidecar.py','--port','1234']);return c;},checkHealth:async()=>{}});
  await p.start(); assert.equal(options.env.DSCODE_SIDECAR_TOKEN,'test-token');
  await assert.rejects(p.start()); await p.stop();assert.deepEqual(c.kills,['SIGTERM']);
});
test('Spawn failure rejects promptly and can be cleaned up', async () => {
  const c=child();const p=new SidecarProcess({python:'missing',script:'x',port:1234,token:'test',
    spawnProcess:()=>{setImmediate(()=>{c.emit('error',new Error('ENOENT'));c.emit('close',-2);});return c;},
    checkHealth:async()=>{throw new Error('not ready');}});
  await assert.rejects(p.start(1000),/ENOENT/); await p.stop();
});
test('Health timeout is bounded and never kills another port owner', async () => {
  const c=child(); const p=new SidecarProcess({python:'python',script:'x',port:1234,token:'test',
    spawnProcess:()=>c,checkHealth:async()=>{throw new Error('wrong service');}});
  await assert.rejects(p.start(10),/timed out/);assert.deepEqual(c.kills,[]);await p.stop();
  assert.deepEqual(c.kills,['SIGTERM']);
});
