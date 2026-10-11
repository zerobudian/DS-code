// DScode V2 v2.0.1-0712-NEXT
(function(){'use strict';
var API='http://127.0.0.1:8765',authToken='',chatClient=null;
var H=[],MAX_HISTORY=50,HIST_IDX=-1;
var cp='demo',cm='deepseek-v4-pro',cmd='normal',cperm='full',crea='high';
var isDark=false,busy=false,curAI=null,cCost=0,cCache=0,cBal=0,cTok=0;
var sStart=0,sChars=0,todos=[];
var DEFAULT_MODELS=[['deepseek-v4-flash','V4 Flash'],['deepseek-v4-pro','V4 Pro']];
var availModels=DEFAULT_MODELS.slice();
function $(id){return document.getElementById(id);}
function E(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// ── init ──
document.addEventListener('DOMContentLoaded',async function(){
  setupInput();setupKeyboard();setupDragDrop();
  try {
    if(window.dscodeApi){var bootstrap=await window.dscodeApi.bootstrap();API=bootstrap.url;authToken=bootstrap.token;}
    chatClient=new ChatClient(apiRequest);
    await loadCfg();loadModels();loadProjects();loadHistory();loadBalance();updateStatus();
  } catch(error){alert(error.message);}
});

// ── keyboard ──
function setupKeyboard(){
  document.addEventListener('keydown',function(e){
    var t=e.target;
    if(t.tagName==='INPUT'||t.tagName==='TEXTAREA')return;
    if(e.key==='Escape'){
      closeQuickSettings();closeDetailSettings();
      if($('wiz-overlay')&&$('wiz-overlay').classList.contains('show'))return;
    }
    if(e.ctrlKey||e.metaKey){
      if(e.key==='b'||e.key==='B'){e.preventDefault();toggleSidebar();}
      if(e.key==='l'||e.key==='L'){e.preventDefault();var ib=$('input-box');if(ib)ib.focus();}
      if(e.key==='n'||e.key==='N'){e.preventDefault();newChat();}
    }
  });
}

// ── drag & drop ──
function setupDragDrop(){
  var box=$('input-box');if(!box)return;
  ['dragenter','dragover','dragleave','drop'].forEach(function(ev){
    box.addEventListener(ev,function(e){e.preventDefault();e.stopPropagation();});
  });
  box.addEventListener('drop',function(e){
    var files=e.dataTransfer.files;
    if(files.length){var names=[];for(var i=0;i<files.length;i++)names.push(files[i].name);box.value+='\n[Files: '+names.join(',')+']';updateSB();}
  });
}

// ── theme ──
function toggleTheme(){
  isDark=!isDark;
  document.documentElement.setAttribute('data-theme',isDark?'dark':'light');
  var b=$('btn-theme');if(b)b.textContent=I18N.t(isDark?'light_mode':'dark_mode');
  apiPut({theme:isDark?'dark':'light'});
}
function toggleSidebar(){var s=$('sidebar');if(s)s.classList.toggle('collapsed');}

// ── api ──
function apiRequest(p,method,obj){
  var options={method:method||'GET',headers:{}};
  if(authToken)options.headers.Authorization='Bearer '+authToken;
  if(obj!==undefined){options.headers['Content-Type']='application/json';options.body=JSON.stringify(obj);}
  return fetch(API+p,options).then(async function(r){
    var data=await r.json();if(!r.ok)throw new Error(data.error||('HTTP '+r.status));return data;
  });
}
function apiGet(p){return apiRequest(p).catch(function(error){console.error(error.message);return null;});}
function apiPut(obj){return apiRequest('/api/config','PUT',obj).catch(function(error){alert(error.message);loadCfg();return null;});}
function streamUrl(id){return API+'/api/chat/stream?run_id='+encodeURIComponent(id)+(authToken?'&token='+encodeURIComponent(authToken):'');}
function loadBalance(){apiGet('/api/balance').then(function(d){if(d&&d.ok){cBal=Number(d.available_balance)||0;wcStats();}});}

// ── models ──
function loadModels(){
  apiGet('/api/models').then(function(d){
    if(!d||!d.ok||!d.models||!d.models.length)return;
    availModels=d.models.map(function(m){return[m.id,m.label];});
    buildQS(); buildDSModels(); refreshCMDS();
  }).catch(function(){});
}
function buildDSModels(){
  var c=$('ds-model'); if(!c)return;
  buildDSRow('ds-model', availModels, 'model', cm);
}

// ── config ──
function loadCfg(){
  return apiGet('/api/config').then(function(c){
    console.log('[loadCfg] onboarding_completed='+(c?c.onboarding_completed:'null')+' api_key_exists='+(c?c.api_key_exists:'null'));
    if(!c){throw new Error(I18N.t('connection_closed'));}
    cp=c.last_project||cp;
    if(c.available_models&&c.available_models.length){availModels=c.available_models.map(function(m){return[m.name,m.label||m.name];});refreshCMDS();}
    if((!c.onboarding_completed)||(!c.api_key_exists)){
      console.log('[loadCfg] 显示引导页面');
      var o=$('wiz-overlay');if(o)o.classList.add('show');wizPg=0;wizProvider='official';showWiz(0);
      var ak=$('wiz-apikey');if(ak)ak.oninput=function(){var b=$('wiz-btn-1');if(b)b.disabled=!ak.value.trim();};
      var ag=$('wiz-agree');if(ag)ag.onchange=function(){var b=$('wiz-btn-3');if(b)b.disabled=!ag.checked;};
      var bu=$('wiz-baseurl');if(bu)bu.oninput=function(){var b=$('wiz-btn-custom');if(b)b.disabled=!bu.value.trim();};
      return;
    }
    cm=c.model||cm;cmd=c.chat_mode||cmd;cperm=c.permission_mode||cperm;crea=c.reasoning_effort||crea;
    cCost=c.balance_cny||0;cCache=c.cache_hit_rate||0;
    if(c.language)I18N.setLang(c.language);
    isDark=c.theme==='dark'||(c.theme==='system'&&window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.setAttribute('data-theme',isDark?'dark':'light');
    updateStatus();wcStats();buildQS();
  });
}

// ── projects ──
function loadProjects(){
  apiGet('/api/projects').then(function(d){
    var pl=$('project-list');if(!pl)return;pl.innerHTML='';
    (d&&d.projects||[]).forEach(function(p){
      var el=document.createElement('div');
      el.className='pj-item'+(p.name===cp?' sel':'');
      el.innerHTML='<span class="pj-dot"></span>'+E(p.display_name||p.name);
      el.onclick=function(){if(busy)return;cp=p.name;loadProjects();loadHistory();};
      el.oncontextmenu=function(ev){ev.preventDefault();showProjectMenu(ev,p.name,p.display_name||p.name);};
      pl.appendChild(el);
    });
  });
}
async function newChat(){
  if(busy)return;
  var answer=await Dialogs.show(I18N.t('new_project_prompt'),[{label:I18N.t('new_project_prompt'),value:'chat-'+Date.now().toString(36)}]);
  if(!answer||!answer[0].trim())return;
  var n=answer[0].trim();
  try{var result=await apiRequest('/api/projects','POST',{name:n,display_name:n});cp=result.name;loadProjects();clearMsgs();}
  catch(error){alert(error.message);}
}
function loadHistory(){
  var project=cp;
  apiGet('/api/projects/'+encodeURIComponent(project)+'/history').then(function(d){
    if(cp!==project||busy||!d)return;clearMsgs();
    var lastAI=null;
    (d.messages||[]).forEach(function(m){
      if(m.role==='user'){addBub('user',m.content||'');lastAI=null;}
      if(m.role==='assistant'){var bub=addAI();lastAI=bub;if(m.reasoning_content)appendRsn(bub,m.reasoning_content);if(m.content)appendAI(bub,m.content);(m.tool_calls||[]).forEach(function(call){addTC(bub,call);});markDone(bub);}
      if(m.role==='tool'&&lastAI)endTC(lastAI,{name:m.name,result:m.content});
    });
    if(d.messages&&d.messages.length){var wc=$('welcome-card');if(wc)wc.style.display='none';}
  });
}
function showProjectMenu(ev,name,display){
  var old=$('pj-menu');if(old)old.remove();
  // 先清理残留的 document click 监听器
  if(window._pjMenuHandler){document.removeEventListener('click',window._pjMenuHandler);}
  var m=document.createElement('div');m.id='pj-menu';
  m.style.cssText='position:fixed;left:'+ev.clientX+'px;top:'+ev.clientY+'px;z-index:300;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:4px;box-shadow:0 4px 16px rgba(0,0,0,.12);min-width:120px';
  ['Delete','Pin'].forEach(function(act){
    var btn=document.createElement('div');
    btn.style.cssText='padding:6px 12px;font-size:12px;cursor:pointer;border-radius:6px;color:var(--text-2)';
    btn.textContent=act==='Delete'?I18N.t('context_delete'):I18N.t('context_pin');
    btn.onmouseenter=function(){btn.style.background='var(--surface)';};
    btn.onmouseleave=function(){btn.style.background='transparent';};
    btn.onclick=function(){
      if(act==='Delete'&&!busy){apiRequest('/api/projects/'+encodeURIComponent(name),'DELETE').then(function(){if(cp===name){cp='demo';clearMsgs();}loadProjects();}).catch(function(error){alert(error.message);});}
      if(act==='Pin')alert(I18N.t('provider_coming_soon'));
      m.remove();
    };
    m.appendChild(btn);
  });
  document.body.appendChild(m);
  window._pjMenuHandler=function h(){m.remove();document.removeEventListener('click',window._pjMenuHandler);window._pjMenuHandler=null;};
  setTimeout(function(){document.addEventListener('click',window._pjMenuHandler);},50);
}

// ── stats ──
function modelLabel(){var m=availModels.find(function(m){return m[0]===cm;});return m?m[1]:cm;}
function wcStats(){
  var s=[['wc-model',modelLabel()],['wc-cache',cCache>=0?Math.round(cCache)+'%':'--'],['wc-cost',cCost>=0?'¥'+cCost.toFixed(4):'--'],['wc-balance',cBal>0?'¥'+cBal.toFixed(2):'--']];
  s.forEach(function(p){var e=$(p[0]);if(e)e.textContent=p[1];});
}
function updateStatus(){
  var s=[['st-model',modelLabel()],['st-reason',crea],['st-mode',({normal:'Std',plan:'plan',ultra:'ultra'})[cmd]||cmd],['st-cost',cCost>=0?'¥'+cCost.toFixed(4):'--'],['st-tokens','tokens '+(cTok||0)],['st-cache',cCache>=0?'Cache '+Math.round(cCache)+'%':'--']];
  s.forEach(function(p){var e=$(p[0]);if(e)e.textContent=p[1];});
}

// ── quick settings ──
function openQuickSettings(){buildQS();$('qs-overlay').classList.add('show');}
function closeQuickSettings(){$('qs-overlay').classList.remove('show');}
function buildQS(){
  var s=[
    {id:'qs-model',k:'model',c:cm,o:availModels},
    {id:'qs-reasoning',k:'reasoning_effort',c:crea,o:[['high','high'],['max','max']]},
    {id:'qs-permission',k:'permission_mode',c:cperm,o:[['reviewer','Reviewer'],['coder','Coder'],['full','Full']]},
    {id:'qs-mode',k:'chat_mode',c:cmd,o:[['normal','Standard'],['plan','plan'],['ultra','ultra']]}
  ];
  s.forEach(function(s){
    var c=$(s.id);if(!c)return;c.innerHTML='';
    s.o.forEach(function(o){
      var b=document.createElement('button');b.className='qs-btn'+(o[0]===s.c?' on':'');
      b.textContent=o[1];
      b.onclick=function(){var obj={};obj[s.k]=o[0];apiPut(obj);if(s.k==='model')cm=o[0];else if(s.k==='chat_mode')cmd=o[0];else if(s.k==='permission_mode')cperm=o[0];else crea=o[0];updateStatus();wcStats();buildQS();};
      c.appendChild(b);
    });
  });
}

// ── detail settings ──
function openSettings(){switchDSTab('basic');$('ds-overlay').classList.add('show');
  apiGet('/api/config').then(function(c){if(!c)return;buildDSRows(c);fillDSCfg(c);});}
function closeDetailSettings(){$('ds-overlay').classList.remove('show');}
function fillDSCfg(c){
  if(!c)return;
  var nn=$('ds-nickname');if(nn){if(c.nickname)nn.value=c.nickname;nn.setAttribute('data-saved',nn.value||'');}
  var ak=$('ds-apikey');if(ak){ak.value=c.api_key_exists?'********':'';ak.setAttribute('data-saved',ak.value);}
  var bu=$('ds-baseurl');if(bu){if(c.base_url)bu.value=c.base_url;bu.setAttribute('data-saved',bu.value||'');}
}
function saveDSField(key, value){
  var el=document.getElementById('ds-'+({nickname:'nickname',api_key:'apikey',base_url:'baseurl'}[key]||key));
  // 如果是 api_key 且 value 为空且之前是脱敏状态，不保存
  if(key==='api_key'&&!value&&el&&el.getAttribute('data-saved')==='********')return;
  var saved=(el?el.getAttribute('data-saved'):null)||'';
  if(String(value)===saved)return;
  var o={};o[key]=value;
  apiPut(o).then(function(result){
    if(!result)return;
    if(el)el.setAttribute('data-saved',key==='api_key'?'********':String(value));
    var t=el?el.nextElementSibling:null;
    if(t&&t.classList.contains('ds-saved')){t.textContent=I18N.t('saved_feedback');t.style.opacity='1';setTimeout(function(){t.style.opacity='0';},1500);}
  });
}
function buildDSRows(c){
  if(!c)return;
  buildDSRow('ds-language',[['zh','Chinese'],['en','English']],'',c.language||'zh');
  buildDSRow('ds-theme',[['light','Light'],['dark','Dark'],['system','System']],'',c.theme||'light');
  buildDSRow('ds-model', availModels, 'model', cm);
  buildDSRow('ds-permission',[['reviewer','Reviewer'],['coder','Coder'],['full','Full']],'',c.permission_mode||'full');
}
function buildDSRow(id,opts,k2,curVal){
  var c=$(id);if(!c)return;c.innerHTML='';
  opts.forEach(function(o){
    var b=document.createElement('button');
    b.className='qs-btn'+(o[0]===curVal?' on':'');
    b.textContent=o[1];
    b.onclick=function(){
      var k=k2||(id==='ds-language'?'language':id==='ds-theme'?'theme':id==='ds-permission'?'permission_mode':id==='ds-model'?'model':'');
      var o2={};o2[k]=o[0];apiPut(o2);
      if(k==='language')I18N.setLang(o[0]);
      if(k==='model')cm=o[0];
      if(k==='permission_mode')cperm=o[0];
      if(k==='theme'){isDark=o[0]==='dark'||(o[0]==='system'&&window.matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.setAttribute('data-theme',isDark?'dark':'light');}
      updateStatus();wcStats();
      // 更新高亮
      var btns=c.querySelectorAll('.qs-btn');
      btns.forEach(function(b2){b2.classList.remove('on');});
      b.classList.add('on');
    };
    c.appendChild(b);
  });
}
function switchDSTab(t){
  document.querySelectorAll('.ds-tab').forEach(function(t2){t2.classList.toggle('active',t2.getAttribute('data-tab')===t);});
  ['basic','provider','about'].forEach(function(t2){var p=$('ds-tab-'+t2);if(p)p.style.display=(t2===t)?'block':'none';});
}
function addProvider(){alert(I18N.t('provider_coming_soon'));}

// ── wizard ──
var wizPg=0;
var wizProvider='official';
var wizCustomModels=[{name:'',context1m:true}];
var wizModelCount=1;
var WIZ_SEQ=['wiz-p0','wiz-p-provider','wiz-p-custom','wiz-p1','wiz-p2','wiz-p3'];
function showWiz(i){
  for(var j=0;j<WIZ_SEQ.length;j++){
    var el=$(WIZ_SEQ[j]); if(el)el.style.display=(j===i)?'block':'none';
  }
}
function wizNext(){
  if(wizPg===0){wizPg=1;showWiz(1);return;}
  if(wizPg===1){
    if(wizProvider==='official'){wizPg=3;showWiz(3);return;}
    else{wizPg=2;showWiz(2);return;}
  }
  if(wizPg===2){
    if(!$('wiz-baseurl').value.trim())return;
    wizPg=3;showWiz(3);return;
  }
  if(wizPg===3){
    if(!$('wiz-apikey').value.trim())return;
    wizPg=4;showWiz(4);return;
  }
  if(wizPg===4){
    var n=$('wiz-nick');if(n.value.trim())apiPut({nickname:n.value.trim()});
    wizPg=5;showWiz(5);return;
  }
  if(wizPg===5){
    if(!$('wiz-agree').checked)return;
    var o2={onboarding_completed:true};
    var ak=$('wiz-apikey');if(ak&&ak.value.trim())o2.api_key=ak.value.trim();
    var n2=$('wiz-nick');if(n2&&n2.value.trim())o2.nickname=n2.value.trim();
    if(wizProvider==='custom'){
      o2.base_url=$('wiz-baseurl').value.trim();
      var validModels=wizCustomModels.filter(function(m){return m.name.trim();}).map(function(m){return{name:m.name.trim(),context_1m:m.context1m};});
      if(validModels.length>0){
        o2.model=validModels[0].name.trim();
        o2.custom_models=validModels;
      }
    }
    if(wizProvider==='official'){o2.base_url='https://api.deepseek.com';o2.custom_models=[];}
    if(wizProvider==='custom'&&!o2.custom_models){alert(I18N.t('wiz_model_name_ph'));return;}
    apiRequest('/api/config','PUT',o2).then(function(){$('wiz-overlay').classList.remove('show');loadCfg().then(function(){loadModels();loadBalance();});loadProjects();})
      .catch(function(error){alert(error.message);});
  }
}

// ── wizard helpers ──
function selectProvider(type){
  wizProvider=type;
  var off=$('wiz-prov-official'),cust=$('wiz-prov-custom');
  if(off)off.classList.toggle('selected',type==='official');
  if(cust)cust.classList.toggle('selected',type==='custom');
  var btn=$('wiz-btn-provider');if(btn)btn.disabled=false;
}
function updateCustomModelName(idx,name){
  if(idx===0)wizCustomModels[0].name=name;else if(wizCustomModels[1])wizCustomModels[1].name=name;
}
function updateCustomModelToggle(idx,is1M,btnEl){
  if(idx===0)wizCustomModels[0].context1m=is1M;else if(wizCustomModels[1])wizCustomModels[1].context1m=is1M;
  var group=btnEl.parentElement;
  if(group){
    var btns=group.querySelectorAll('.qs-btn');
    for(var i=0;i<btns.length;i++)btns[i].classList.toggle('on',btns[i]===btnEl);
  }
}
function addCustomModelRow(){
  if(wizModelCount>=2)return;
  wizModelCount++;
  wizCustomModels.push({name:'',context1m:true});
  var idx=wizCustomModels.length-1;
  var container=$('wiz-model-rows');if(!container)return;
  var row=document.createElement('div');row.className='wiz-model-row';row.setAttribute('data-row',idx);
  var inp=document.createElement('input');inp.type='text';inp.className='wiz-model-name';
  inp.setAttribute('data-i18n-placeholder','wiz_model_name_ph');
  inp.placeholder=I18N.t('wiz_model_name_ph');
  inp.oninput=function(){updateCustomModelName(idx,this.value);};
  var tg=document.createElement('div');tg.className='wiz-toggle-group';tg.id='wiz-toggle-'+idx;
  var btn1M=document.createElement('button');btn1M.className='qs-btn on';btn1M.textContent='1M';
  btn1M.onclick=function(){updateCustomModelToggle(idx,true,btn1M);};
  var btnStd=document.createElement('button');btnStd.className='qs-btn';btnStd.textContent='Std';
  btnStd.onclick=function(){updateCustomModelToggle(idx,false,btnStd);};
  tg.appendChild(btn1M);tg.appendChild(btnStd);
  var del=document.createElement('button');del.className='wiz-model-del-btn';del.innerHTML='&#x2715;';
  del.onclick=function(){removeCustomModelRow(idx,row);};
  row.appendChild(inp);row.appendChild(tg);row.appendChild(del);
  container.appendChild(row);
  var addBtn=$('wiz-add-model');if(addBtn&&wizModelCount>=2)addBtn.style.display='none';
}
function removeCustomModelRow(idx,rowEl){
  wizCustomModels.splice(idx,1);wizModelCount--;
  if(rowEl&&rowEl.parentElement)rowEl.parentElement.removeChild(rowEl);
  var rows=document.querySelectorAll('#wiz-model-rows .wiz-model-row');
  for(var i=0;i<rows.length;i++)rows[i].setAttribute('data-row',i);
  var addBtn=$('wiz-add-model');if(addBtn)addBtn.style.display='';
}

// ── 命令菜单 ──
var CMDS=[
  {cmd:'/clear', icon:'🧹', name:'/clear', desc:'清空当前对话', zh:'清空当前对话', en:'Clear chat'},
  {cmd:'/help', icon:'❓', name:'/help', desc:'使用帮助', zh:'使用帮助', en:'Show help'},
  {cmd:'/plan', icon:'📋', name:'/plan', desc:'计划模式', zh:'计划模式', en:'Plan mode'},
  {cmd:'/ultra', icon:'⚡', name:'/ultra', desc:'深度推理模式', zh:'深度推理模式', en:'Ultra mode'},
  {cmd:'/normal', icon:'💬', name:'/normal', desc:'标准对话模式', zh:'标准对话模式', en:'Normal mode'},
  {cmd:'/dark', icon:'🌙', name:'/dark', desc:'切换深色模式', zh:'切换深色模式', en:'Toggle dark mode'},
  {cmd:'/light', icon:'☀', name:'/light', desc:'切换浅色模式', zh:'切换浅色模式', en:'Toggle light mode'},
{cmd:'/undo', icon:'↩', name:'/undo', desc:'撤销上一步操作', zh:'撤销上一步操作', en:'Undo last action'},
];
var _cmdsBaseLen=CMDS.length;
function refreshCMDS(){
  while(CMDS.length>_cmdsBaseLen){CMDS.pop();}
  availModels.forEach(function(m){
    CMDS.push({cmd:'/model '+m[1], icon:'🔀', name:'/model '+m[1],
      desc:'切换 '+m[1], zh:'切换 '+m[1], en:'Switch to '+m[1]});
  });
}
var cmdIdx=-1,cmdList=[];

function showCmdPopup(){
  var box=$('input-box'),v=box?box.value:'';
  if(!v.startsWith('/')||v.length<1){hideCmdPopup();return;}
  var q=v.toLowerCase();cmdList=CMDS.filter(function(c){return c.cmd.toLowerCase().indexOf(q)===0;});
  if(cmdList.length===0){hideCmdPopup();return;}
  var p=$('cmd-popup');if(!p)return;
  p.innerHTML='';cmdIdx=-1;
  var isEn=I18N.current==='en';
  cmdList.forEach(function(c,i){
    var row=document.createElement('div');row.className='cmd-item';
    row.innerHTML='<span class="cmd-icon">'+E(c.icon)+'</span><span class="cmd-name">'+E(c.name)+'</span><span class="cmd-desc">'+E(isEn?c.en:c.zh)+'</span>';
    row.onclick=function(){selectCmd(i);};
    row.onmousedown=function(e){e.preventDefault();};
    p.appendChild(row);
  });
  p.classList.add('show');
}
function hideCmdPopup(){var p=$('cmd-popup');if(p){p.classList.remove('show');p.innerHTML='';}cmdIdx=-1;cmdList=[];}
function moveCmdSel(d){
  cmdIdx+=d;cmdIdx=Math.max(-1,Math.min(cmdList.length-1,cmdIdx));
  var rows=document.querySelectorAll('#cmd-popup .cmd-item');
  rows.forEach(function(r,i){r.classList.toggle('sel',i===cmdIdx);});
}
function selectCmd(idx){
  if(idx<0||idx>=cmdList.length)return;
  handleSlashCmd(cmdList[idx].cmd);
  hideCmdPopup();
}
function handleSlashCmd(cmd){
  if(cmd==='/dscodeisnice'){showEasterEgg();$('input-box').value='';updateSB();return;}
  if(cmd==='/clear'){if(busy)return;clearMsgs();$('input-box').value='/clear';sendMsg();return;}
  if(cmd==='/help'){alert('DS Code v2.0.1\n/clear /plan /ultra /normal /dark /light /model /undo\n/dscodeisnice 🤫');return;}
  if(cmd==='/dark'){isDark=false;toggleTheme();$('input-box').value='';updateSB();return;}
  if(cmd==='/light'){isDark=true;toggleTheme();$('input-box').value='';updateSB();return;}
  // dynamic /model <label> — iterate availModels
  if(cmd.lastIndexOf('/model ',0)===0){
    var target=cmd.slice(7).trim();
    for(var i=0;i<availModels.length;i++){
      var m=availModels[i];
      if(m[0]===target||m[1]===target){
        cm=m[0];apiPut({model:m[0]});updateStatus();wcStats();buildQS();
        $('input-box').value='';updateSB();return;
      }
    }
  }
  $('input-box').value=cmd;updateSB();sendMsg();
}

// ── input ──
function setupInput(){
  var box=$('input-box');if(!box)return;
  box.addEventListener('input',function(){
    box.style.height='auto';box.style.height=Math.min(box.scrollHeight,140)+'px';updateSB();
    var v=box.value;if(v==='/'){showCmdPopup();}else if(v.startsWith('/')){showCmdPopup();}else{hideCmdPopup();}
  });
  box.addEventListener('keydown',function(e){
    var pop=$('cmd-popup');var popShown=pop&&pop.classList.contains('show');
    if(popShown){
      if(e.key==='ArrowDown'){e.preventDefault();moveCmdSel(1);return;}
      if(e.key==='ArrowUp'){e.preventDefault();moveCmdSel(-1);return;}
      if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(cmdIdx>=0)selectCmd(cmdIdx);else{hideCmdPopup();}return;}
      if(e.key==='Escape'){e.preventDefault();hideCmdPopup();return;}
    }
    if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();hideCmdPopup();sendMsg();}
    else if(e.key==='ArrowUp'&&!box.value){e.preventDefault();navHist(-1);}
    else if(e.key==='ArrowDown'&&!box.value){e.preventDefault();navHist(1);}
  });
  document.addEventListener('click',function(e){if(!e.target.closest('#input-area'))hideCmdPopup();});
}
function updateSB(){
  var box=$('input-box');if(!box)return;var has=(box.value||'').trim().length>0;
  var sb=$('btn-send');if(sb)sb.disabled=busy||!has;
  var st=$('btn-stop');if(st)st.style.display=busy?'flex':'none';
}
function navHist(dir){
  if(!H.length)return;
  if(HIST_IDX===-1)HIST_IDX=H.length;
  HIST_IDX=Math.max(0,Math.min(H.length,HIST_IDX+dir));
  var box=$('input-box');if(box)box.value=HIST_IDX<H.length?H[HIST_IDX]:'';updateSB();
}
function resetTS(){sStart=Date.now();sChars=0;updateTS();}
function updateTS(){
  var ts=$('token-stats');if(!ts||!sStart)return;
  var el=(Date.now()-sStart)/1000,cps=el>0?Math.round(sChars/el):0;ts.textContent=cps+' c/s - '+sChars+' chars';
}

function showEasterEgg(){
  var o=$('ee-overlay');if(!o)return;o.classList.add('show');
  setTimeout(function(){o.classList.remove('show');},3000);
}
function sendMsg(){
  var box=$('input-box');if(!box)return;var text=(box.value||'').trim();if(!text)return;
  if(text==='/dscodeisnice'){box.value='';showEasterEgg();return;}
  if(busy||!chatClient)return;
  // history
  if(H.length===0||H[H.length-1]!==text){H.push(text);if(H.length>MAX_HISTORY)H.shift();}
  HIST_IDX=-1;
  addBub('user',text);box.value='';box.style.height='auto';
  var wc=$('welcome-card');if(wc)wc.style.display='none';
  busy=true;updateSB();resetTS();
  var bub=addAI();curAI=bub;
  runChat('/api/chat/send',{project:cp,text:text},bub);
}
function runChat(path,body,bub){
  busy=true;updateSB();
  var pendingPlan=null;
  function finish(){busy=false;Dialogs.close();updateSB();updateTS();loadProjects();}
  function confirmRequest(kind,event){
    var runId=chatClient.active&&chatClient.active.id;
    var payload=event.payload;
    var message=typeof payload==='string'?payload:JSON.stringify(payload,null,2);
    var fields=kind==='answer'?[{label:I18N.current==='en'?'Your answer':'你的回答'}]:[];
    Dialogs.show(message,fields).then(function(answer){
      var value=kind==='answer'?(answer?answer[0]:''):(answer!==null);
      return chatClient.confirm(kind,event.request_id,value,runId);
    }).catch(function(error){alert(error.message);});
  }
  chatClient.start(path,body,{
    started:function(result){cp=result.project;},
    content:function(delta){appendAI(bub,delta);},reasoning:function(delta){appendRsn(bub,delta);},
    tool_call:function(data){addTC(bub,data);},tool_result:function(data){endTC(bub,data);},
    chat_clear:function(){clearMsgs();},
    confirm_command:function(event){confirmRequest('command',event);},
    confirm_edit:function(event){confirmRequest('edit',event);},
    ask_user_questions:function(event){confirmRequest('answer',event);},
    plan_ready:function(plan){pendingPlan=plan;},
    srv_error:function(data){var content=bub.querySelector('.ai-content');if(content)content.textContent='❌ '+data.message;finish();},
    done:function(data){
      cCost=Number(data.cost)||0;cCache=Number(data.cache_hit_rate)||0;cTok=data.total_tokens||0;
      if(data.mode)cmd=data.mode;updateStatus();wcStats();
      markDone(bub);
      finish();
      if(pendingPlan&&!data.stopped){
        var project=cp;
        Dialogs.show(pendingPlan,[]).then(function(answer){
          if(cp!==project||busy)return;
          if(answer===null){apiRequest('/api/chat/plan','POST',{project:project,confirmed:false}).catch(function(error){alert(error.message);});return;}
          runChat('/api/chat/plan',{project:project,confirmed:true},addAI());
        });
      }
    }
  },streamUrl);
}
function stopChat(){if(chatClient)chatClient.stop().catch(function(error){alert(error.message);});Dialogs.close();}

// ── bubbles ──
function markDone(bub){
  var inner=bub.querySelector('.ai-inner');if(!inner)return;inner.classList.add('done');
  var reasoning=bub.querySelector('.rsn-body'),header=bub.querySelector('.rsn-hdr');
  if(reasoning)reasoning.classList.add('hidden');if(header)header.textContent=I18N.t('thinking_done');
  if(inner.querySelector('.ai-actions'))return;
  var actions=document.createElement('div');actions.className='ai-actions';
  var copy=document.createElement('button');copy.textContent=I18N.t('btn_copy');
  copy.onclick=function(){navigator.clipboard.writeText(bub._rawContent||'').catch(function(){});};
  actions.appendChild(copy);inner.appendChild(actions);
}
function addBub(role,text){var ct=$('chat-msgs');if(!ct)return;var el=document.createElement('div');el.className='bubble bubble-'+role;el.textContent=text;ct.appendChild(el);scrollDown();return el;}
function addAI(){var ct=$('chat-msgs');if(!ct)return null;var el=document.createElement('div');el.className='bubble bubble-ai';el.innerHTML='<div class="rsn-block" style="display:none"></div><div class="tools-area"></div><div class="ai-inner"><div class="ai-content"></div><div class="token-stats"></div></div>';ct.appendChild(el);scrollDown();return el;}
function appendAI(bub,delta){sChars+=delta.length;bub._rawContent=(bub._rawContent||'')+delta;var c=bub.querySelector('.ai-content');if(c)c.innerHTML=renderMarkdown(bub._rawContent);updateTS();scrollDown();}
function appendRsn(bub,delta){
  var rsn=bub.querySelector('.rsn-block');if(!rsn){rsn=document.createElement('div');rsn.className='rsn-block';bub.insertBefore(rsn,bub.firstChild);}
  rsn.style.display='block';
  if(!rsn.querySelector('.rsn-hdr')){var hdr=document.createElement('div');hdr.className='rsn-hdr';hdr.innerHTML=I18N.t('thinking_label');hdr.onclick=function(){var b=this.parentElement.querySelector('.rsn-body');if(b){b.classList.toggle('hidden');this.innerHTML=b.classList.contains('hidden')?I18N.t('thinking_label'):I18N.t('thinking_expanded');}};rsn.appendChild(hdr);}
  var body=rsn.querySelector('.rsn-body');if(!body){body=document.createElement('div');body.className='rsn-body';rsn.appendChild(body);}
  body.textContent+=delta;scrollDown();
}
function addTC(bub,data){var name=data.tool||(data.function&&data.function.name)||'tool';var area=bub.querySelector('.tools-area');if(!area)return;var card=document.createElement('div');card.className='tool-card';card.setAttribute('data-name',name);card.innerHTML='<div class="tool-hdr"><span class="tool-dot"></span>'+E(name)+'</div><div class="tool-body"></div>';card.querySelector('.tool-hdr').onclick=function(){var b=card.querySelector('.tool-body');b.style.display=b.style.display==='block'?'none':'block';};area.appendChild(card);scrollDown();}
function endTC(bub,data){var name=data.name||'tool';var area=bub.querySelector('.tools-area');if(!area)return;var cards=Array.from(area.querySelectorAll('.tool-card'));var card=cards.find(function(c){return c.getAttribute('data-name')===name&&!c.querySelector('.tool-dot.ok');});if(!card)return;var dot=card.querySelector('.tool-dot');if(dot)dot.classList.add('ok');var body=card.querySelector('.tool-body');if(body){body.textContent=(data.result||'').slice(0,300);body.style.display='block';}}
function clearMsgs(){var msgs=$('chat-msgs');if(!msgs)return;Array.prototype.slice.call(msgs.children).forEach(function(c){if(c.id!=='welcome-card')c.remove();});var wc=$('welcome-card');if(wc)wc.style.display='';wcStats();curAI=null;}
function scrollDown(){
  var ct=$('chat-container');if(!ct)return;
  var bar=ct;var atBottom=(bar.scrollHeight-bar.scrollTop-bar.clientHeight)<40;
  if(atBottom)requestAnimationFrame(function(){ct.scrollTop=ct.scrollHeight;});
}

// ── todos ──
function addTodoFromBtn(){var inp=$('todo-input');if(!inp||!inp.value.trim())return;todos.push({text:inp.value.trim(),done:false});inp.value='';renderTodos();}
function toggleTodo(idx){todos[idx].done=!todos[idx].done;renderTodos();}
function delTodo(idx){todos.splice(idx,1);renderTodos();}
function renderTodos(){
  var list=$('todos-list');if(!list)return;list.innerHTML='';
  todos.forEach(function(t,i){var row=document.createElement('div');row.className='todo-row';row.innerHTML='<span class="todo-dot'+(t.done?' done':'')+'" onclick="toggleTodo('+i+')"></span><span class="todo-text'+(t.done?' done':'')+'">'+E(t.text)+'</span><span class="todo-del" onclick="delTodo('+i+')">x</span>';list.appendChild(row);});
  var cnt=$('todo-count');if(cnt)cnt.textContent=todos.length;
}

// ── files ──
function openProjectFolder(){alert(I18N.t('project_files_path')+' '+cp+'/');}

// ── utils ──
function openExternal(url){var w=window.open(url,'_blank');if(!w)location.href=url;}

// ── exports ──
window.toggleTheme=toggleTheme;window.toggleSidebar=toggleSidebar;window.newChat=newChat;
window.openSettings=openSettings;window.openQuickSettings=openQuickSettings;
window.closeQuickSettings=closeQuickSettings;window.closeDetailSettings=closeDetailSettings;
window.switchDSTab=switchDSTab;window.addProvider=addProvider;
window.fillDSCfg=fillDSCfg;window.saveDSField=saveDSField;
window.wizNext=wizNext;window.selectProvider=selectProvider;
window.updateCustomModelName=updateCustomModelName;window.updateCustomModelToggle=updateCustomModelToggle;
window.addCustomModelRow=addCustomModelRow;window.removeCustomModelRow=removeCustomModelRow;
window.sendMessage=sendMsg;window.stopChat=stopChat;
window.showEasterEgg=showEasterEgg;
window.addTodoFromBtn=addTodoFromBtn;
window.toggleTodo=toggleTodo;window.delTodo=delTodo;
window.updateSB=updateSB;
window.openProjectFolder=openProjectFolder;window.openExternal=openExternal;
})();
