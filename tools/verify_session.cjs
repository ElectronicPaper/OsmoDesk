/* Exercises the shared browser transport without a camera or HTTP listener. */
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const source = fs.readFileSync(require('node:path').join(__dirname,'../web/session.js'),'utf8');
let uid=0;
function tab(insecure=false) {
 const calls=[];
 const win={dispatchEvent(){}};
 const ctx=vm.createContext({window:win,Uint8Array,crypto:insecure?{getRandomValues:a=>{a.fill(++uid);return a;}}:{randomUUID:()=>`tab-${++uid}`},
  CustomEvent:class{},AbortSignal,JSON,Promise,
  fetch:async(path,options)=>{calls.push({path,options});return {ok:true,status:200,json:async()=>({ok:true,workspace:{generation:'epoch:2'}})}}});
 vm.runInContext(source,ctx); return {s:win.OsmoSession,calls,ctx};
}
(async()=>{
 const a=tab(),b=tab();assert.notEqual(a.s.client,b.s.client);
 const phone=tab(true);assert.match(phone.s.client,/^[a-z0-9-]+$/i);
 a.s.observe({workspace:{generation:'epoch:1'}});
 a.s.observe({workspace:{generation:'epoch:2'}},{dirty:true});
 const denied=await a.s.post('/api/move',{name:'stale'});
 assert.equal(denied.conflict,true);assert.equal(a.calls.length,0);
 a.s.reset({workspace:{generation:'epoch:2'}});
 await a.s.post('/api/move',{name:'fresh'});
 assert.equal(a.calls[0].options.headers['X-Osmo-Generation'],'epoch:2');
 assert.equal(a.calls[0].options.headers['X-Osmo-Client'],a.s.client);
 await a.s.post('/api/stop',{});assert.equal(a.calls.at(-1).path,'/api/stop');
 // An old poll response cannot roll the authoring generation backward.
 a.s.observe({workspace:{generation:'epoch:1'}});
 assert.equal(a.s.generation,'epoch:2');
 a.s.observe({workspace:{generation:'restarted:0'}});
 a.s.observe({workspace:{generation:'epoch:3'}});
 assert.equal(a.s.generation,'restarted:0','retired host epoch cannot return');
 const dirty=tab();dirty.s.observe({workspace:{generation:'old:1'}});
 dirty.s.observe({workspace:{generation:'new:0'}},{dirty:true});
 dirty.s.reset({workspace:{generation:'new:0'}});
 dirty.s.observe({workspace:{generation:'old:2'}});
 assert.equal(dirty.s.generation,'new:0','conflict reset retires the prior epoch');
 dirty.s.reset({workspace:{generation:'old:3'}});
 assert.equal(dirty.s.generation,'new:0','reset cannot resurrect a retired epoch');
 const held=tab();await held.s.post('/api/stick',{tilt:.5,pan:0});
 const first=held.calls.at(-1).options.headers['X-Osmo-Gesture'];
 await held.s.post('/api/stop',{});await held.s.post('/api/stick',{tilt:.5,pan:0});
 assert.equal(held.calls.at(-1).options.headers['X-Osmo-Gesture'],first,'STOP cannot invent a fresh hold');
 await held.s.post('/api/stick',{tilt:0,pan:0});await held.s.post('/api/stick',{tilt:.5,pan:0});
 assert.notEqual(held.calls.at(-1).options.headers['X-Osmo-Gesture'],first,'release permits a new deliberate hold');
 console.log('PASS: per-tab identity, conflict latch, explicit reset, no stale generation rollback, universal STOP transport');
})().catch(e=>{console.error(e);process.exitCode=1;});
