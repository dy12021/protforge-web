import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {smoke} from './frontend_smoke.mjs';
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const response=(data,status=200)=>({ok:status<400,status,json:async()=>data});
async function setup(file='web/app.js') {
  const h=await smoke(file);assert.deepEqual(h.errors,[]);
  h.run=code=>vm.runInContext(code,h.context);
  h.element=id=>h.sandbox.document.getElementById(id);
  return h;
}
test('workspace: onboarding differs from filtered no-results, and reset restores data',async()=>{
  const h=await setup();
  h.run('workspaceUI.tasksReady = workspaceUI.workflowsReady = true; state.items=[]; state.workflows=[]; renderFilterNote();');
  assert.equal(h.element('workspace-empty').hidden,false);
  assert.equal(h.element('empty-title').textContent,'从第一个计算任务开始');
  assert.equal(h.element('empty-create').hidden,false);
  h.run('state.items=[{id:9,title:"test",status:"queued"}]; state.filter.q="missing"; renderFilterNote();');
  assert.equal(h.element('empty-title').textContent,'没有找到匹配的结果');
  assert.equal(h.element('empty-reset').hidden,false);
  h.element('empty-reset').onclick();
  assert.equal(h.sandbox.state.filter.q,'');
  assert.equal(h.element('workspace-empty').hidden,true);
  assert.equal(h.element('filter-reset').disabled,true);
});
test('workspace: incomplete loading does not claim there is no data',async()=>{
  const h=await setup();
  h.run('workspaceUI.workflowsReady=false; state.items=[]; state.workflows=[]; renderWorkspaceEmpty();');
  assert.equal(h.element('workspace-empty').hidden,true);
});
test('workspace: manual refresh prevents duplicates, retains data on failure and offers retry',async()=>{
  const h=await setup(), gate=deferred();
  const before=h.sandbox.state.items.length;
  const original=h.sandbox.fetch;let count=0;
  h.sandbox.fetch=(url,opt)=>String(url).endsWith('/api/tasks')?(count++,gate.promise):original(url,opt);
  const first=h.run('refreshWorkspace()');
  await h.run('refreshWorkspace()');
  assert.equal(count,1);assert.equal(h.element('refresh-btn').disabled,true);
  gate.resolve(response({detail:'临时不可用'},503));await first;
  assert.equal(h.sandbox.state.items.length,before);
  assert.equal(h.element('refresh-btn').disabled,false);
  assert.equal(h.element('refresh-btn').textContent,'重试');
  assert.match(h.element('sync-state').textContent,/保留/);
});
test('market: visible during load, can close before a response without reopening',async()=>{
  const h=await setup(),gate=deferred();h.sandbox.fetch=()=>gate.promise;
  const loading=h.run('openMarket()');
  assert.equal(h.element('market').open,true);
  assert.match(h.element('mt-msg').textContent,/读取/);
  h.element('market-close').onclick();
  gate.resolve(response({tools:[]}));await loading;
  assert.equal(h.element('market').open,false);
});
test('market: failed load exposes retry and successful retry removes the error',async()=>{
  const h=await setup();h.sandbox.fetch=async()=>response({detail:'离线'},503);
  await h.run('openMarket()');assert.equal(h.element('mt-retry').hidden,false);
  h.sandbox.fetch=async()=>response({tools:[]});
  await h.element('mt-retry').onclick();
  assert.equal(h.element('mt-retry').hidden,true);
  assert.equal(h.element('mt-msg').hidden,true);
});
test('drawer: an obsolete failure cannot overwrite the newly opened task',async()=>{
  const h=await setup(),first=deferred(),second=deferred();let n=0;
  h.sandbox.fetch=()=>++n===1?first.promise:second.promise;
  h.sandbox.drawDrawer=(id)=>{h.element('drawer-body').textContent='task '+id;};
  h.sandbox.state.streams.set(2,{});
  const old=h.sandbox.openDrawer(1),current=h.sandbox.openDrawer(2);
  second.resolve(response({task:{id:2,title:'new',status:'done'}}));await current;
  first.resolve(response({detail:'old error'},500));await old;
  assert.equal(h.element('drawer-body').textContent,'task 2');
  assert.equal(h.element('drawer').getAttribute('aria-busy'),'false');
});
for(const file of ['web/app.js','admin-ui/app.js'])test(`${file}: initial login is neutral and password visibility is announced`,async()=>{
  const h=await setup(file);h.sandbox.signedOut('请登录');
  assert.equal(h.element('login-error').hidden,true);
  h.element('l-pass').type='password';h.element('password-toggle').onclick();
  assert.equal(h.element('l-pass').type,'text');
  assert.equal(h.element('password-toggle').getAttribute('aria-pressed'),'true');
  h.element('password-toggle').onclick();assert.equal(h.element('l-pass').type,'password');
});
test('admin: refresh reloads the visible users pane as well as task counts',async()=>{
  const h=await setup('admin-ui/app.js');h.sandbox.state.tab='users';
  const urls=[];h.sandbox.fetch=async url=>{urls.push(String(url));return response(String(url).endsWith('/users')?{users:[]}:{tasks:[]});};
  await h.run('refreshCurrentView()');
  assert.ok(urls.some(url=>url.endsWith('/users')));
  assert.ok(urls.some(url=>url.endsWith('/tasks')));
  assert.equal(h.element('refresh-btn').disabled,false);
  assert.equal(h.element('sync-state').textContent,'当前视图已更新');
});
test('admin: tab arrows move focus and selected state together',async()=>{
  const h=await setup('admin-ui/app.js'),doc=h.sandbox.document;
  const tabs=['live','pending','tasks','audit','workflows','users'].map(name=>{const node=doc.createElement('button');node.dataset.tab=name;return node;});
  doc.querySelectorAll=selector=>selector==='#tabs .tab'?tabs:[];
  h.sandbox.showTab('live');
  await Promise.all(h.element('tabs').fire('keydown',{target:tabs[0],key:'End'}));
  assert.equal(h.sandbox.state.tab,'users');assert.equal(doc.activeElement,tabs[5]);
  assert.equal(tabs[5].getAttribute('aria-selected'),'true');
  assert.equal(tabs[0].getAttribute('aria-selected'),'false');assert.equal(tabs[0].tabIndex,-1);
});
test('workflow: upload restores its action and status after a network failure',async()=>{
  const h=await setup('web/workflow.js'),gate=deferred();
  h.element('wf-file').files=[{name:'input.xlsx'}];
  h.sandbox.FormData=class {append(){}};
  let calls=0;h.sandbox.fetch=()=>{calls++;return gate.promise;};
  const first=h.element('wf-upload').fire('click')[0];
  await Promise.all(h.element('wf-upload').fire('click'));
  assert.equal(calls,1);assert.equal(h.element('wf-upload').disabled,true);
  gate.reject(new Error('offline'));await first;
  assert.equal(h.element('wf-upload').disabled,false);
  assert.match(h.element('wf-upload-state').textContent,/失败/);
  assert.equal(h.element('wf-retry').hidden,false);
});

test('composer: server errors stay visible inside the open form, and submission unlocks',async()=>{
  const h=await setup();h.sandbox.openComposer();h.sandbox.composerMode=()=> 'task';
  h.element('c-title').value='test';h.element('c-brief').value='brief';
  h.sandbox.fetch=async()=>response({detail:'计算资源暂时不可用'},503);
  await Promise.all(h.element('composer-form').fire('submit'));
  assert.equal(h.element('composer').open,true);
  assert.equal(h.element('c-error').hidden,false);
  assert.match(h.element('c-error').textContent,/计算资源/);
  assert.equal(h.element('c-save').disabled,false);
  assert.equal(h.element('composer').getAttribute('aria-busy'),'false');
});
test('workflow: every source row can be previewed before any tasks exist',async()=>{
  const h=await setup('web/workflow.js'),paths=[];
  h.run('wfId=1; wfSheet="S1"; lastDetail={workflow:{id:1,total_rows:2},rows:[]};');
  h.sandbox.fetch=async url=>{paths.push(String(url));return response({values:{S1:{1:'second sequence'}}});};
  await h.run('previewRow(1)');
  assert.ok(paths[0].endsWith('/preview?row=1'));
  assert.equal(h.element('wf-preview-index').value,'2');
  assert.equal(h.element('wf-preview-next').disabled,true);
  await h.run('previewRow(2)');
  assert.equal(paths.length,1);
  assert.match(h.element('wf-msg').textContent,/1 到 2/);
});
test('admin: active total includes queued and packaging tasks',async()=>{
  const h=await setup('admin-ui/app.js');
  h.sandbox.fetch=async url=>response(String(url).endsWith('/overview')?{tasks:{total:8,queued:2,running:3,packaging:1,done:2}}:{});
  await h.sandbox.loadStatus();
  const metric=h.element('sp-metrics').children.find(n=>n.children.some(c=>c.textContent==='进行中'));
  assert.equal(metric.children.find(n=>n.className==='sp-m-value').textContent,'6');
});
test('workspace: initial successful loading finishes the sync indicator immediately',async()=>{
  const h=await setup();
  assert.equal(h.element('sync-state').textContent,'数据已同步');
});
test('workspace: signing out during a load cancels the remaining boot chain',async()=>{
  const h=await setup(),gate=deferred();h.sandbox.fetch=()=>gate.promise;
  const loading=h.sandbox.load();
  h.sandbox.signedOut('请登录');
  gate.resolve(response({tasks:[{id:99,title:'old',status:'queued'}]}));
  await assert.rejects(loading,error=>error.name==='AbortError');
  assert.equal(h.sandbox.state.items.length,0);
  assert.equal(h.element('workspace-empty').hidden,true);
});
