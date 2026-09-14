/* Real browser + real local HTTP; explicit synthetic provider; camera tripwire. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const [base,shots]=process.argv.slice(2);
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(fn,label){for(let i=0;i<80;i++){if(await fn())return;await pause(100);}throw Error('Timed out: '+label);}
async function overflowDetails(p){return p.evaluate(()=>[...document.querySelectorAll('body *')].filter(e=>{const r=e.getBoundingClientRect();return r.width&&r.right>innerWidth+1&&!e.closest('[hidden]');}).slice(-12).map(e=>[e.tagName,e.id,e.className,Math.round(e.getBoundingClientRect().right)]));}
async function main(){
 const browser=await chromium.launch({headless:true,channel:'chrome'});
 const p=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[],posts=[];
 p.on('pageerror',e=>errors.push(e.message));p.on('request',r=>{if(r.method()==='POST')posts.push(new URL(r.url()).pathname);});
 const status=()=>p.request.get(base+'/api/status').then(r=>r.json());
 const fixture={name:'Window reveal',loop:false,ping_pong:false,route_arcs:true,setup:{fov_deg:65,notes:'SET_PRIVATE'},waypoints:[
  {name:'PRIVATE opening',pitch:90,yaw:0,duration:3,dwell:0,easing:'ease-in-out-sine'},
  {name:'PRIVATE reveal',pitch:90,yaw:15,duration:4,dwell:0,easing:'ease-in-out-sine'},
  {name:'PRIVATE settle',pitch:95,yaw:30,duration:4,dwell:0,easing:'ease-in-out-sine'}]};
 try{
  await p.goto(base);await until(()=>p.evaluate(()=>!!OsmoSession.generation),'host generation');
  assert.equal(posts.length,0,'no cloud or mutation requests on startup');
  const authored=await p.evaluate(m=>OsmoSession.post('/api/move',m),fixture);assert.ok(authored.ok,JSON.stringify(authored));
  await p.reload();await until(()=>p.locator('#moveName').inputValue().then(x=>x==='Window reveal'),'draft loaded');
  await p.locator('.worktab[data-work="director"]').click();
  await until(()=>p.locator('#aiPrepare').isEnabled(),'configured AI');
  const before=await status();
  await p.locator('[data-brief]').first().click();await p.locator('#aiPrepare').click();
  await until(()=>p.locator('#aiDisclosure').isVisible(),'data disclosure');
  assert.equal(posts.filter(x=>x==='/api/assistant/send').length,0,'prepare cannot call provider');
  const context=JSON.parse(await p.locator('#aiContext').textContent());
  assert.equal(context.beats[0].name,'P1');
  assert.ok(!JSON.stringify(context).includes('PRIVATE'));assert.ok(!JSON.stringify(context).includes('pitch'));
  assert.match(await p.locator('#aiManifest').textContent(),/cost reservation/);
  for(const width of [1440,768,390]){
   await p.setViewportSize({width,height:1100});
   assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'disclosure overflow '+width+' '+JSON.stringify(await overflowDetails(p)));
   const button=await p.locator('#aiSend').boundingBox();assert.ok(button.width>=44&&button.height>=44);
  }
  await p.setViewportSize({width:1440,height:1100});await p.locator('#aiSend').click();
  await until(()=>p.locator('#aiResults').isVisible(),'two treatments');
  assert.equal(posts.filter(x=>x==='/api/assistant/send').length,1);
  assert.deepEqual((await status()).move,before.move,'cloud request must not mutate');
  const cards=p.locator('.ai-treatment');assert.equal(await cards.count(),2);
  assert.ok(await cards.first().getByRole('button',{name:'Apply to draft',exact:true}).isDisabled(),'preview is required');
  await cards.first().locator('summary').click();
  assert.ok((await cards.first().locator('tbody tr').count())>=2);
  for(const width of [1440,768,390]){
   await p.setViewportSize({width,height:1100});
   assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'treatment overflow '+width+' '+JSON.stringify(await overflowDetails(p)));
   for(const name of ['Rehearse treatment','Apply to draft']){const box=await cards.first().getByRole('button',{name,exact:false}).boundingBox();assert.ok(box.width>=44&&box.height>=44,name+' touch target');}
   if(shots&&width!==768){await p.evaluate(()=>scrollTo(0,0));await p.screenshot({path:path.join(shots,`copilot-${width}.png`),fullPage:true});}
  }
  await p.setViewportSize({width:1440,height:1100});
  await cards.first().getByRole('button',{name:'Rehearse treatment'}).click();
  await until(()=>p.locator('#dViewLabel').textContent().then(x=>x.includes('AI treatment')),'AI rehearsal');
  assert.deepEqual((await status()).move,before.move,'rehearsal must not mutate');
  await cards.first().getByRole('button',{name:'Apply to draft',exact:true}).click();
  await until(async()=>(await status()).move.waypoints[1].duration===8,'apply stored candidate');
  const after=await status();
  assert.deepEqual(after.move.waypoints.map(w=>[w.pitch,w.yaw]),before.move.waypoints.map(w=>[w.pitch,w.yaw]));
  assert.deepEqual(after.setup,before.setup);assert.equal(after.state,'idle');assert.equal(after.armed,false);
  await until(()=>p.locator('#dUndo').isVisible(),'restore action');await p.locator('#dUndo').click();
  await until(async()=>(await status()).move.waypoints[1].duration===4,'restore authored pacing');
  // Stale disclosure is rejected locally after a different tab edits the host.
  await p.locator('#aiPrepare').click();await until(()=>p.locator('#aiDisclosure').isVisible(),'fresh disclosure');
  const other=await browser.newPage();await other.goto(base);await until(()=>other.evaluate(()=>!!OsmoSession.generation),'second tab');
  assert.ok((await other.evaluate(m=>OsmoSession.post('/api/move',m),{...fixture,name:'New host draft'})).ok);
  await until(()=>p.locator('#moveName').inputValue().then(x=>x==='New host draft'),'foreign draft observed');
  assert.ok(await p.locator('#aiDisclosure').isHidden());await other.close();
  // The host admits the send, but the HTTP response is lost. Recover the
  // preassigned job ID through GET, never by making a second paid request.
  await p.locator('#aiPrepare').click();await until(()=>p.locator('#aiDisclosure').isVisible(),'recoverable disclosure');
  let dropped=0;
  await p.route('**/api/assistant/send',async route=>{await route.fetch();dropped++;await route.abort('failed');});
  await p.locator('#aiSend').click();await until(()=>p.locator('#aiResults').isVisible(),'lost response recovered');
  assert.equal(dropped,1,'recovery must not resend');await p.unroute('**/api/assistant/send');
  await p.locator('#aiDiscard').click();
  // Provider failure stays inline; no retry, mutation, or invented treatment.
  await p.locator('#aiBrief').fill('TEST FAILURE show recovery');await p.locator('#aiPrepare').click();
  await until(()=>p.locator('#aiDisclosure').isVisible(),'failure disclosure');await p.locator('#aiSend').click();
  await until(()=>p.locator('#aiStatus').textContent().then(x=>x.includes('Synthetic provider unavailable')),'safe provider failure');
  const sends=posts.filter(x=>x==='/api/assistant/send').length;await pause(1300);
  assert.equal(posts.filter(x=>x==='/api/assistant/send').length,sends,'no automatic paid retry');
  assert.ok(await p.locator('#aiResults').isHidden());
  // In-flight cancellation discards late output and preserves the draft.
  await p.locator('#aiBrief').fill('Try another reveal');await p.locator('#aiPrepare').click();
  await until(()=>p.locator('#aiDisclosure').isVisible(),'cancel disclosure');await p.locator('#aiSend').click();
  await until(()=>p.locator('#aiCancel').isVisible(),'cancel available');await p.locator('#aiCancel').click();
  await pause(1500);assert.ok(await p.locator('#aiResults').isHidden());
  assert.equal((await status()).move.name,'New host draft');
  // A looping round trip has no programmed end, but its preview cycle must
  // include both outbound and return travel, not just forward_duration.
  assert.ok((await p.evaluate(m=>OsmoSession.post('/api/move',m),{...fixture,loop:true,ping_pong:true})).ok);
  await p.reload();await until(()=>p.locator('#aiPrepare').isEnabled(),'looping shot loaded');
  await p.locator('.worktab[data-work="director"]').click();
  await p.locator('[data-brief]').first().click();await p.locator('#aiPrepare').click();
  await until(()=>p.locator('#aiDisclosure').isVisible(),'loop disclosure');await p.locator('#aiSend').click();
  await until(()=>p.locator('#aiResults').isVisible(),'loop treatments');
  const loopDuration=parseFloat(await p.locator('.ai-duration').first().textContent());
  await p.locator('.ai-treatment').first().getByRole('button',{name:'Rehearse treatment'}).click();
  await until(()=>p.locator('#dViewLabel').textContent().then(x=>x.includes('AI treatment')),'loop preview');
  assert.equal(loopDuration,parseFloat(await p.locator('#dEnd').textContent()),'loop treatment must show complete round trip');
  assert.ok((await p.evaluate(m=>OsmoSession.post('/api/move',m),fixture)).ok);
  assert.deepEqual(errors,[],'runtime errors');
  assert.ok(posts.every(x=>['/api/move','/api/director/preview','/api/assistant/prepare','/api/assistant/send','/api/assistant/cancel'].includes(x)),'no hardware requests');
  console.log('PASS: real browser disclosure, bounded request, 2 treatments, local rehearsal/apply/undo, geometry privacy, 3 widths, stale draft, lost-send response recovery without redispatch, provider failure without retry, cancellation; synthetic provider, no hardware');
 }finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
