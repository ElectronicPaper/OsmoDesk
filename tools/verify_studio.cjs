/* Browser behavior, not a substitute for physical-camera verification.
 * Run via verify_studio.py; the host refuses every actuation endpoint. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const [base, shots] = process.argv.slice(2);
const pause = ms => new Promise(r => setTimeout(r, ms));
async function until(fn, label) {
  for (let i=0; i<60; i++) { if (await fn()) return; await pause(100); }
  throw new Error(`Timed out: ${label}`);
}
async function main() {
 const browser = await chromium.launch({headless:true,channel:'chrome'});
 const errors = [];
 const newPage = async width => {
   const p = await browser.newPage({viewport:{width,height:1000},deviceScaleFactor:1});
   p.on('pageerror', e => errors.push(e.message)); return p;
 };
 try {
  const p = await newPage(1440), posts=[];
  p.on('dialog', dialog => dialog.accept());
  p.on('request', r => { if(r.method()==='POST') posts.push(new URL(r.url()).pathname); });
  await p.goto(base);
  await until(()=>p.locator('#saveState').textContent().then(x=>x.includes('host')), 'initial status');
  assert.equal(posts.length,0,'page load must not send control or configuration commands');
  const expectedTooltips={
   compose:'Add or edit positions, then Preview the path — nothing here moves the head.',
   director:'Rehearse the timing offline. Preview and proposals never move the camera.',
   shoot:'Connect, then compose at least two points to enable motion and run or roll.',
   review:'Log a take, then compare it against another to check the match.',
   rig:'Record the rig setup so a saved shot can be recreated later.'
  };
  const tooltips=()=>p.locator('.worktab').evaluateAll(tabs=>Object.fromEntries(tabs.map(tab=>[tab.dataset.work,tab.title])));
  assert.deepEqual(await tooltips(),expectedTooltips,'workspace tooltips contain explicit plain text');
  assert.equal(await p.locator('.worktab svg').count(),5,'workspace icons remain present');
  assert.equal(await p.locator('#nextAction b').textContent(),'Preview','formatted workspace help remains intact');
  const status = () => p.request.get(base+'/api/status').then(r=>r.json());
  await p.locator('#moveName').fill('Window reveal');
  await p.locator('#moveName').press('Tab');
  for(let i=0;i<3;i++) { await p.locator('#btnAddPos').click(); await until(async()=> (await status()).move.waypoints.length===i+1,'add position'); }
  await p.locator('[data-k="name"][data-i="0"]').fill('<svg id="injected" onload="window.labelExecuted=true"></svg>');
  await p.locator('[data-k="name"][data-i="0"]').press('Tab');
  await p.locator('[data-work="rig"]').click();
  await p.locator('#suFov').fill('65'); await p.locator('#suFov').press('Tab');
  await until(async()=>+(await status()).setup.fov_deg===65,'setup save');
  await p.locator('[data-work="compose"]').click();
  await p.locator('[data-edit="2"]').click();
  await p.locator('[data-k="yaw"][data-i="2"]').fill('32');
  await p.locator('[data-k="yaw"][data-i="2"]').press('Tab');
  await until(async()=> (await status()).move.waypoints[2].yaw===32,'position save');
  assert.equal(+(await status()).setup.fov_deg,65,'editing must preserve setup');
  const imported=await status();
  await p.locator('#fileIn').setInputFiles({name:'route.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify({...imported.move,setup:imported.setup,route_arcs:false}))});
  await until(async()=> (await status()).move.route_arcs===false,'import route choice');
  await p.locator('#shotCard details').first().locator('summary').click();
  await p.locator('[data-retime="2"]').click();
  await until(async()=> (await status()).move.total_duration===12,'offline retime');
  await p.locator('#shotCard details').first().locator('summary').click();
  assert.equal((await status()).move.route_arcs,false,'retime preserves route choice');
  assert.equal(await p.locator('#injected').count(),0,'operator labels must stay text');
  assert.equal(await p.evaluate(()=>window.labelExecuted===true),false,'hostile waypoint event handler never executes');
  assert.deepEqual(await tooltips(),expectedTooltips,'waypoint labels do not alter static tooltips');
  assert.equal(await p.locator('[data-goto]:visible,[data-seg]:visible').count(),0,'Compose cannot actuate');
  await p.locator('#previewRange').focus(); await p.keyboard.press('End');
  await until(()=>p.locator('#previewReadout').textContent().then(x=>!x.includes('0.0s')), 'keyboard preview');
  await p.locator('#btnPreflight').click();
  await p.locator('#btnLibSave').click();
  await until(()=>p.locator('[data-lib-load]').count().then(n=>n>0),'library save');
  await p.locator('[data-edit="2"]').click();
  await p.locator('[data-k="yaw"][data-i="2"]').fill('50');
  await p.locator('[data-k="yaw"][data-i="2"]').press('Tab');
  await until(async()=> (await status()).move.waypoints[2].yaw===50,'unsaved library change');
  await p.locator('[data-lib-load]').click();
  await until(async()=> (await status()).move.waypoints[2].yaw===32,'library restore');
  const before=(await status()).move;
  await p.locator('#fileIn').setInputFiles({name:'bad.json',mimeType:'application/json',buffer:Buffer.from('{"waypoints":[{"pitch":"not a number","yaw":0}]}')});
  await until(()=>p.locator('#errBox').isVisible(),'invalid import visible');
  assert.deepEqual((await status()).move,before,'invalid import preserves draft');
  await p.reload(); await until(()=>p.locator('#wps .wp').count().then(n=>n===3),'reload draft');
  assert.equal(await p.locator('#moveName').inputValue(),'Window reveal');
  assert.equal(await p.locator('#injected').count(),0);
  assert.equal(await p.evaluate(()=>window.labelExecuted===true),false,'restored hostile label never executes');
  // Use a human-readable name for the actual UI preview after the escaping check.
  await p.locator('[data-k="name"][data-i="0"]').fill('Window / start');
  await p.locator('[data-k="name"][data-i="0"]').press('Tab');
  await until(async()=> (await status()).move.waypoints[0].name==='Window / start','final draft');
  await p.locator('.worktab[data-work="director"]').click();
  await until(()=>p.locator('#dPlay').isEnabled(),'Director loaded');
  const draftBefore=(await status()).move;
  const draftSetup=(await status()).setup;
  await p.locator('#dPlay').click();await pause(400);
  assert.ok(+(await p.locator('#dScrub').inputValue())>0,'offline preview advances');
  await p.locator('#dPlay').click();
  assert.deepEqual((await status()).move,draftBefore,'preview must not change authored move');
  await p.locator('#dReset').click();await p.locator('#dPlay').click();
  await p.evaluate(async()=>{
   for(let i=0;i<4;i++) await new Promise(resolve=>requestAnimationFrame(()=>{
    const start=performance.now();while(performance.now()-start<150){}resolve();
   }));
  });await pause(40);
  await p.locator('#dPlay').click();
  assert.ok(+(await p.locator('#dScrub').inputValue())>=.55,'slow frames must not stretch the shot clock');
  await p.locator('#dTarget').fill('20');await p.locator('#dFit').click();
  await until(()=>p.locator('#dApply').isEnabled(),'timing proposal');
  assert.equal((await status()).move.total_duration,12,'proposal is not an automatic edit');
  await p.locator('#dPreviewProposal').click();
  await until(()=>p.locator('#dViewLabel').textContent().then(x=>x==='Timing proposal'),'proposal preview');
  assert.equal(await p.locator('#dDuration').textContent(),'20.00 s','pace card follows the visible proposal');
  await p.locator('#dApply').click();
  await until(async()=> (await status()).move.total_duration===20,'apply checked timing');
  await p.locator('#dUndo').click();
  await until(async()=> (await status()).move.total_duration===12,'restore previous timing');
  await until(()=>p.locator('#dScrub').isEnabled(),'restored analysis ready');
  await p.locator('#dScrub').focus();await p.keyboard.press('End');
  assert.ok(+(await p.locator('#dScrub').inputValue())>=12,'keyboard seeks canonical preview');
  // A failed refresh must not leave a previous analysis looking usable.
  await p.route('**/api/director/preview',r=>r.fulfill({status:503,json:{ok:false,error:'Analysis temporarily unavailable'}}));
  await p.locator('#dRefresh').click();
  await until(()=>p.locator('#dState').textContent().then(x=>x.includes('unavailable')),'analysis failure shown');
  assert.equal(await p.locator('#dPlay').isDisabled(),true,'failed analysis disables old rehearsal');
  assert.equal(await p.locator('#dProposal').isVisible(),false,'failed analysis hides old proposal');
  await p.unroute('**/api/director/preview');await p.locator('#dRefresh').click();
  await until(()=>p.locator('#dPlay').isEnabled(),'analysis retry recovers');
  // Hold the successful HTTP response while the same tab makes a newer
  // Compose edit. The proposal response cannot replace that edit on screen.
  await p.locator('#dTarget').fill('20');await p.locator('#dFit').click();
  await until(()=>p.locator('#dApply').isEnabled(),'race proposal ready');
  let releaseApply, heldReply=false, holdNext=true;
  const applyGate=new Promise(resolve=>releaseApply=resolve);
  await p.route('**/api/move',async r=>{
   if(!holdNext)return r.continue();holdNext=false;
   const response=await r.fetch();heldReply=true;await applyGate;await r.fulfill({response});
  });
  await p.locator('#dApply').click();await until(async()=>heldReply,'hold timing reply');
  await p.locator('.worktab[data-work="compose"]').click();
  await p.locator('#moveName').fill('Newer local edit');await p.locator('#moveName').press('Tab');
  releaseApply();
  await until(async()=> (await status()).move.name==='Newer local edit','newer edit reaches host');
  assert.equal(await p.locator('#moveName').inputValue(),'Newer local edit','late timing reply cannot replace newer local edit');
  await p.unroute('**/api/move');
  // Real canonical cue timing, with no camera: pause at P1, require explicit
  // continuation, and retain the chosen motion under reduced-motion media.
  const cueDraft={...draftBefore,setup:draftSetup,waypoints:draftBefore.waypoints.map((w,i)=>({...w,cue:i===0}))};
  const author=move=>p.evaluate(move=>OsmoSession.post('/api/move',move),move);
  assert.ok((await author(cueDraft)).ok);
  await p.reload();await p.locator('.worktab[data-work="director"]').click();
  await until(()=>p.locator('#dPlay').isEnabled(),'cue analysis loaded');
  await p.emulateMedia({reducedMotion:'reduce'});await p.locator('#dPlay').click();
  await until(()=>p.locator('#dCue').isVisible(),'preview holds at cue');
  const cueTime=await p.locator('#dScrub').inputValue();await pause(250);
  assert.equal(await p.locator('#dScrub').inputValue(),cueTime,'cue waits do not advance');
  await p.locator('#dContinue').click();await pause(300);
  assert.ok(+(await p.locator('#dScrub').inputValue())>+cueTime,'explicit cue continuation advances');
  await p.locator('.worktab[data-work="compose"]').click();
  const pausedTime=await p.locator('#dScrub').inputValue();await pause(250);
  assert.equal(await p.locator('#dScrub').inputValue(),pausedTime,'leaving Director pauses animation');
  const seam={...draftBefore,setup:draftSetup,loop:false,ping_pong:false,waypoints:[
   {name:'Before seam',pitch:170,yaw:0,duration:1},
   {name:'After seam',pitch:-170,yaw:0,duration:1}]};
  assert.ok((await author(seam)).ok);await p.reload();
  await p.locator('.worktab[data-work="director"]').click();await until(()=>p.locator('#dPlay').isEnabled(),'seam analysis');
  await p.locator('#dScrub').evaluate(e=>{e.value=.99;e.dispatchEvent(new Event('input'));});
  const seamPose=await p.locator('#dPose').textContent();
  assert.ok(+seamPose.match(/TILT ([\d.-]+)/)[1]>170,'display cannot interpolate through zero at angle seam: '+seamPose);
  assert.ok((await author({...draftBefore,setup:draftSetup})).ok);await p.emulateMedia({reducedMotion:'no-preference'});
  await p.reload();await until(()=>p.locator('#moveName').inputValue().then(x=>x==='Window reveal'),'restore cue fixture');
  // Two real browser documents author the same real host. Unsaved typing
  // survives a foreign revision and no stale write is silently replayed.
  const other=await newPage(390);other.on('dialog',d=>d.accept());
  await other.goto(base);await until(()=>other.locator('#moveName').inputValue().then(x=>x==='Window reveal'),'second author loaded');
  await other.locator('#moveName').fill('Local B');
  await p.locator('.worktab[data-work="compose"]').click();
  await p.locator('#moveName').fill('Host A');await p.locator('#moveName').press('Tab');
  await until(async()=> (await status()).move.name==='Host A','first author saved');
  await until(()=>other.locator('#conflictBanner').isVisible(),'second author conflict');
  assert.equal(await other.locator('#moveName').inputValue(),'Local B','conflict preserves local typing');
  await other.locator('#moveName').press('Tab');await pause(200);
  assert.equal((await status()).move.name,'Host A','stale tab cannot overwrite');
  const download=other.waitForEvent('download');await other.locator('#exportConflict').click();
  assert.ok((await download).suggestedFilename().endsWith('.json'),'local draft remains exportable');
  await other.locator('#reloadConflict').click();
  await until(()=>other.locator('#moveName').inputValue().then(x=>x==='Host A'),'explicit conflict recovery');
  await p.locator('#moveName').fill('Window reveal');await p.locator('#moveName').press('Tab');
  await until(()=>other.locator('#moveName').inputValue().then(x=>x==='Window reveal'),'clean tab receives foreign draft');
  await other.close();
  for(const width of [1440,768,390]) {
   await p.setViewportSize({width,height:1000});
   for(const section of ['compose','director','shoot','review','rig']) {
    await p.locator(`.worktab[data-work="${section}"]`).click();
    await pause(100);
    assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${section} ${width} overflow`);
    for(const id of ['btnStopMotion',...section==='compose'?['btnAddPos']:[]]) {
     const box=await p.locator('#'+id).boundingBox(); assert.ok(box.height>=44&&box.width>=44,`${id} target size`);
    }
    if(shots && width!==768) {
     await p.evaluate(()=>scrollTo(0,0)); await pause(150);
     await p.screenshot({path:path.join(shots,`studio-${section}-${width}.png`),fullPage:true});
    }
   }
  }
  assert.ok(posts.every(x=>['/api/move','/api/moves/save','/api/moves/load','/api/move/retime','/api/director/preview'].includes(x)),`unexpected authoring calls: ${posts}`);
  await p.close();
  // Connected/lost/recovered states are simulated in the browser, never on a radio.
  for(const route of ['/panel','/cine','/mobile']) {
   const q=await newPage(390), commands=[];
   let lost=false;
   const initial=await q.request.get(base+'/api/status').then(r=>r.json());
   const fake={...initial,state:'connected',link_healthy:true,telemetry:true,pitch:90,yaw:10,
    recording:{on:true,reported:false,since:Date.now()/1000},camera:{iso:400,manual:true,zoom:1.5},
    shutter:{speed_label:'1/50',denominator:50,fps_reported:false},preroll:{until:Date.now()/1000+60},live:{have_frame:false}};
   await q.route('**/api/**',async r=>{
    const req=r.request(),url=new URL(req.url());
    if(url.pathname==='/api/status') return lost ? r.abort('failed') : r.fulfill({json:fake});
    if(req.method()==='POST') { commands.push({path:url.pathname,body:req.postDataJSON()});return r.fulfill({status:400,json:{error:'camera refused test command'}}); }
    if(url.pathname==='/api/stream') return r.abort();
    return r.continue();
   });
   await q.goto(base+route); await pause(550);
   assert.equal(commands.length,0,route+' startup posts');
   if(route==='/cine') {
    assert.equal(await q.locator('#fIso').textContent(),'400');
    await q.locator('#fShut_w').click();
    assert.deepEqual(commands.at(-1),{path:'/api/camera',body:{set:'shutter',value:100}});
    assert.equal(await q.locator('#fShut').textContent(),'1/50','failed setting cannot paint a false reading');
    await q.locator('#btnRec').click();
    assert.equal(commands.at(-1).body.name,'record_stop');
    assert.equal(await q.locator('#recWord').textContent(),'REC REQUESTED');
    await q.evaluate(()=>{on.frame=true;on.mirror=true;document.getElementById('frameSqueeze').value='1.33';punch=2;applySqueeze();});
    const transform=await q.locator('#pic').evaluate(e=>e.style.transform);
    assert.ok(transform.includes('-') && transform.includes('2'),transform);
   }
   if(route==='/panel') await q.locator('.worktab[data-work="shoot"]').click();
   const pad=q.locator(route==='/cine'?'#stick':'#pad');
   await pad.scrollIntoViewIfNeeded();
   const area=await pad.boundingBox();
   await q.mouse.move(area.x+area.width*.65,area.y+area.height*.5); await q.mouse.down();
   await pause(150);
   lost=true;
   const lostLabel=q.locator(route==='/panel'?'#hostBanner':route==='/cine'?'#recWord':'#linkState');
   await until(async()=>await lostLabel.isVisible() && (await lostLabel.textContent()).includes('HOST LOST'),'host loss '+route);
   if(route==='/panel') assert.equal(await q.locator('#btnCapture').isDisabled(),true);
   if(route==='/cine') assert.equal(await q.locator('#preroll').evaluate(e=>e.classList.contains('on')),false);
   if(route==='/mobile') assert.equal(await q.locator('#countdown').evaluate(e=>e.classList.contains('on')),false);
   const count=commands.length; lost=false; await pause(700);
   assert.equal(commands.length,count,route+' must not replay on recovery');
   await q.mouse.up();
   if(route==='/mobile') {
    for(const i of [1,2,1,0]) {
     await q.locator(`[data-go="${i}"]`).click(); await pause(300);
     const x=await q.locator('.ws').nth(i).evaluate(e=>e.getBoundingClientRect().x);
     assert.ok(Math.abs(x)<1,'workspace motion settles at correct offset');
    }
    await q.emulateMedia({reducedMotion:'reduce'});
    await q.locator('[data-go="1"]').click();
    assert.equal(await q.locator('#track').evaluate(e=>getComputedStyle(e).transitionDuration),'0s');
   }
   if(shots) await q.screenshot({path:path.join(shots,route.slice(1)+'-simulated-status.png'),fullPage:true});
   await q.close();
  }
  assert.deepEqual(errors,[],'browser runtime errors');
  console.log('PASS: offline authoring, text safety, invalid import, saved setup, restart, Director preview/timing/restore/cues/failure recovery, two-tab conflicts, keyboard, 15 responsive workspaces, tally contracts, 3 host-loss/recovery paths, mobile transition/reversal/reduced motion');
 } finally {await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
