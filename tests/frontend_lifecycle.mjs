import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a; reject=b;}); return {promise,resolve,reject}; };
const tick = () => new Promise(r => setImmediate(r));
const response = data => ({ok:true, status:200, json:async()=>data});
function storage() { const m = new Map(); return {getItem:k=>m.get(k),setItem:(k,v)=>m.set(k,v),removeItem:k=>m.delete(k)}; }
for (const file of ['web/app.js','admin-ui/app.js']) {
  for (const phase of ['fetch','json','error']) test(`${file}: ignores previous session during ${phase}`, {timeout:2000}, async () => {
    const src = readFileSync(file,'utf8');
    const gate = deferred();
    const box = vm.createContext({localStorage:storage(), location:{search:''}, URLSearchParams, window:{}, TOKEN_KEY:'test', fetch:async()=> phase==='fetch' ? gate.promise : phase==='error' ? {ok:false,status:401,json:()=>gate.promise} : {ok:true,status:200,json:()=>gate.promise}});
    vm.runInContext(src.slice(src.indexOf('function token()'), src.indexOf('\n// ---------------------------------------------------------------- 小工具',src.indexOf('function token()'))), box);
    box.setToken('same-token');
    const result = box.api('GET','/tasks');
    await tick();
    box.setToken(''); box.setToken('same-token');
    gate.resolve(phase==='fetch' ? response({tasks:[77]}) : {tasks:[77]});
    await assert.rejects(result, e => e.name === 'AbortError');
  });
}
function workflow() {
  const elements = new Map();
  const element = () => ({focus(){},hidden:false,textContent:'',value:'',children:[],dataset:{},style:{},appendChild(c){this.children.push(c);},querySelector(){return element();}});
  const document = {querySelector:s=>{if(!elements.has(s))elements.set(s,element());return elements.get(s);},createElement:element,createTextNode:t=>({textContent:t})};
  const requests = [];
  const box = vm.createContext({document,localStorage:storage(),location:{search:''},window:{},URLSearchParams,setInterval:()=>1,clearInterval(){},setTimeout:()=>1,clearTimeout(){}});
  const src = readFileSync('web/workflow.js','utf8');
  vm.runInContext(src.slice(0,src.indexOf('\n$("#wf-download").addEventListener')),box);
  box.api = (method,path) => {const gate=deferred(); requests.push({path,...gate}); return gate.promise;};
  vm.runInContext('renderHead = renderRows = renderTaskTable = showStep = closeColPick = renderHere = function() {}; stepByState = () => 1;',box);
  return {box,requests,elements,run:s=>vm.runInContext(s,box)};
}
const detail = (id,status='running') => ({workflow:{id,name:`wf${id}`,status,total_rows:2,cursor:1},rows:[],columns:[],packages_count:0});
test('workflow: late detail cannot reopen list',{timeout:2000},async()=>{
  const h=workflow(), opening=h.box.openDetail(1);
  const listing=h.box.loadList();
  h.requests[1].resolve({workflows:[]}); await listing;
  h.requests[0].resolve(detail(1));
  await tick();
  for(const r of h.requests.slice(2))r.resolve({values:{}});
  await opening;
  assert.equal(h.elements.get('#wf-detail').hidden,true);
});
test('workflow: late preview cannot overwrite newer row',{timeout:2000},async()=>{
  const h=workflow(); h.run('wfId=1');
  const first=h.box.loadPreview(0), second=h.box.loadPreview(1);
  h.requests[1].resolve({values:{'':{0:'new'}}}); await second;
  h.requests[0].resolve({values:{'':{0:'old'}}}); await first;
  assert.equal(h.run('curRow'),1);
});
test('workflow: poll updates controls without replacing draft',{timeout:2000},async()=>{
  const h=workflow(); h.run('wfId=1');
  h.box.document.querySelector('#wf-template').value='unsaved';
  const poll=h.box.refreshLive();
  h.requests[0].resolve({...detail(1,'finished'),workflow:{...detail(1,'finished').workflow,result_ready:true}}); await poll;
  assert.equal(h.elements.get('#wf-start').disabled,true);
  assert.equal(h.elements.get('#wf-download').hidden,false);
  assert.equal(h.elements.get('#wf-template').value,'unsaved');
});
test('workflow: no overlapping polls; a new view can poll immediately',{timeout:2000},async()=>{
  const h=workflow(); h.run('wfId=1');
  const first=h.box.refreshLive(); await h.box.refreshLive();
  assert.equal(h.requests.length,1);
  h.run('wfId=2');
  const next=h.box.refreshLive(); assert.equal(h.requests.length,2);
  h.requests[1].resolve(detail(2)); await next;
  h.requests[0].resolve(detail(1)); await first;
  assert.equal(h.run('lastDetail.workflow.id'),2);
});

test('workflow: reopening the same ID invalidates its previous poll', {timeout:2000}, async()=>{
  const h=workflow(); h.run('wfId=1');
  const old=h.box.refreshLive();
  const opening=h.box.openDetail(1);
  h.requests[1].resolve(detail(1)); await tick();
  h.requests[2].resolve({values:{}}); await opening;
  h.requests[0].resolve({...detail(1),workflow:{...detail(1).workflow,name:'stale'}}); await old;
  assert.equal(h.run('lastDetail.workflow.name'),'wf1');
});
test('workflow: failed poll releases the active request', {timeout:2000}, async()=>{
  const h=workflow(); h.run('wfId=1');
  const first=h.box.refreshLive(); h.requests[0].reject(new Error('offline')); await first;
  const second=h.box.refreshLive(); assert.equal(h.requests.length,2);
  h.requests[1].resolve(detail(1)); await second;
});
test('workflow: a closed task detail stays closed when pending refresh completes', {timeout:2000}, async()=>{
  const h=workflow(); h.run('wfId=1');
  const first=h.box.showTaskDetail(1); h.requests[0].resolve({task:{id:1,title:'first'}}); await first;
  const refresh=h.box.showTaskDetail(1);
  const host=h.elements.get('#wf-task-detail');
  host.children[0].onclick();
  h.requests[1].resolve({task:{id:1,title:'late'}}); await refresh;
  assert.equal(host.hidden,true); assert.equal(h.run('taskDetailId'),null);
});
test('workflow: obsolete task detail cannot replace a newer task', {timeout:2000}, async()=>{
  const h=workflow(); h.run('wfId=1');
  const first=h.box.showTaskDetail(1), second=h.box.showTaskDetail(2);
  h.requests[1].resolve({task:{id:2,title:'new'}}); await second;
  h.requests[0].resolve({task:{id:1,title:'old'}}); await first;
  assert.equal(h.run('taskDetailId'),2);
});
test('admin list keeps the newest response when requests finish out of order', {timeout:2000}, async()=>{
  const src=readFileSync('admin-ui/app.js','utf8'), requests=[];
  const box=vm.createContext({state:{tasks:[],pending:new Map()},document:{getElementById:()=>({textContent:""})},scopeQuery:()=>'',api:()=>{const r=deferred();requests.push(r);return r.promise;}});
  for(const name of ['renderLive','renderPending','renderTasks','syncStreams','syncAuditFilters'])box[name]=()=>{};
  vm.runInContext(src.slice(src.indexOf('let loadSeq ='),src.indexOf('\nasync function loadUsers()')),box);
  const first=box.load(),second=box.load();
  requests[1].resolve({tasks:[{id:2}]});await second;
  requests[0].resolve({tasks:[{id:1}]});await first;
  assert.equal(box.state.tasks[0].id,2);
});
for(const file of ['web/app.js','admin-ui/app.js'])test(`${file}: workflow list keeps the newest response`,{timeout:2000},async()=>{
  const src=readFileSync(file,'utf8'), requests=[];
  const start=src.indexOf('async function loadWorkflows()'), end=src.indexOf('\n}',start)+2;
  const box=vm.createContext({state:{},workspaceUI:{},renderWorkflows(){},renderFilterNote(){},api:()=>{const r=deferred();requests.push(r);return r.promise;}});
  vm.runInContext('let workflowLoadSeq = 0;\n'+src.slice(start,end),box);
  const first=box.loadWorkflows(),second=box.loadWorkflows();
  requests[1].resolve({workflows:[{id:2}]});await second;
  requests[0].resolve({workflows:[{id:1}]});await first;
  assert.equal(box.state.workflows[0].id,2);
});
for(const file of ['web/app.js','admin-ui/app.js']) {
  test(`${file}: obsolete stream 401 does not sign out the new session`,{timeout:2000},async()=>{
    const src=readFileSync(file,'utf8'), gate=deferred();
    const box=vm.createContext({localStorage:storage(),TOKEN_KEY:'test',location:{search:''},URLSearchParams,window:{},AbortController,state:{streams:new Map()},fetch:()=>gate.promise,signedOut(){throw new Error('new session signed out');}});
    vm.runInContext(src.slice(src.indexOf('function token()'),src.indexOf('\n// ---------------------------------------------------------------- 小工具',src.indexOf('function token()'))),box);
    const start=src.indexOf('async function startStream(id)'),end=src.indexOf('\n}',start)+2;
    vm.runInContext(src.slice(start,end),box);
    box.setToken('old'); const stream=box.startStream(1);
    box.setToken('new'); gate.resolve({status:401}); await stream;
    assert.equal(box.token(),'new');
  });
  test(`${file}: obsolete network failure is cancellation`,{timeout:2000},async()=>{
    const src=readFileSync(file,'utf8'), gate=deferred();
    const box=vm.createContext({localStorage:storage(),TOKEN_KEY:'test',location:{search:''},URLSearchParams,window:{},fetch:()=>gate.promise});
    vm.runInContext(src.slice(src.indexOf('function token()'),src.indexOf('\n// ---------------------------------------------------------------- 小工具',src.indexOf('function token()'))),box);
    box.setToken('old'); const request=box.api('GET','/tasks');
    box.setToken('new'); gate.reject(new Error('offline'));
    await assert.rejects(request,e=>e.name==='AbortError');
  });
}
