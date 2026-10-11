const test = require('node:test');
const assert = require('node:assert/strict');
const {ProjectClient} = require('../renderer/js/project-client');

test('project operations escape names and retain task revision', async () => {
  const calls=[]; const client=new ProjectClient(async (...args)=>{calls.push(args);return {ok:true};});
  await client.pin('中文 name', true);
  await client.saveTasks('中文 name',[{text:'todo',done:false}],'revision-1');
  await client.file('中文 name',{area:'preview',path:'space & name.txt'});
  assert.deepEqual(calls[0],['/api/projects/'+encodeURIComponent('中文 name'),'PUT',{pinned:true}]);
  assert.equal(calls[1][2].revision,'revision-1');
  assert.match(calls[2][0],/path=space%20%26%20name.txt$/);
});

test('attachment upload sends actual bytes and checks size before reading', async () => {
  const calls=[];const client=new ProjectClient(async (...args)=>{calls.push(args);return {name:'saved'};});
  const data=new TextEncoder().encode('内容');
  await client.upload('project',{name:'note.txt',size:data.length,arrayBuffer:async()=>data.buffer});
  assert.equal(Buffer.from(calls[0][2].data,'base64').toString(),'内容');
  await assert.rejects(client.upload('project',{size:1024*1024,arrayBuffer:()=>{throw Error('must not read');}}),/512/);
});
