const {_electron}=require('playwright');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
const electron=require('electron');
const assert=require('node:assert/strict');
(async()=>{
 const app=await _electron.launch({executablePath:electron,args:['--no-sandbox',root],timeout:20000});
 try {
  const page=await app.firstWindow();console.log('Electron renderer opened');const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.waitForFunction(()=>document.getElementById('st-model')?.textContent==='test-model');
  await page.evaluate(()=>{window.newChat();});
  await page.locator('.dialog-panel textarea').fill('electron-chat-test');
  await page.locator('.dialog-panel button[type=submit]').click();
  await page.locator('.pj-item').filter({hasText:'electron-chat-test'}).waitFor();
  await page.locator('#input-box').fill('native-tool-test');await page.locator('#btn-send').click();
  await page.locator('.ai-inner.done').waitFor();
  assert.equal(await page.locator('.code-block code').textContent(),'print(42)');
  await page.reload();await page.locator('.code-block code').waitFor();
  assert.equal(await page.locator('.code-block code').textContent(),'print(42)');
  assert.deepEqual(errors,[]);
  assert.equal(await page.locator('.tool-card .tool-dot.ok').count(),1);
  if(process.env.DSCODE_SMOKE_SCREENSHOT)await page.screenshot({path:process.env.DSCODE_SMOKE_SCREENSHOT});
  console.log('Native Electron smoke passed: Sidecar owned startup, IPC/auth, async project creation, native tool, streamed Markdown, history after reload, zero page errors.');
 } finally {await app.close();}
})().catch(e=>{console.error(e);process.exit(1);});
