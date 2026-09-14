/* Real Chrome, synthetic keys only; host blocks every camera/provider action. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const [base,shots]=process.argv.slice(2);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn,label){for(let i=0;i<70;i++){if(await fn())return;await sleep(100);}throw Error('Timed out: '+label);}
async function main(){
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),p=await context.newPage(),errors=[],posts=[];
 p.on('pageerror',e=>errors.push(e.message));p.on('dialog',d=>d.accept());
 p.on('request',r=>{if(r.method()==='POST')posts.push(new URL(r.url()).pathname);});
 try{
  await p.goto(base+'/?t=browser-fixture-only');
  await until(()=>p.locator('#saveState').textContent().then(t=>t.includes('host')),'host draft');
  assert(!p.url().includes('fixture'),'query credential removed');assert.equal(posts.length,0);
  await p.evaluate(()=>{window.keepPad=document.querySelector('#pad');window.keepPlot=document.querySelector('#dPlot');});
  const group=p.locator('[data-workspace="compose"].studio-workspace-grid');
  await group.locator('[data-workspace-arrange]').click();
  const move=group.locator('[data-workspace-panel="shotCard"] [data-workspace-action="move"]');
  await move.focus();await p.keyboard.press('Shift+ArrowLeft');await p.keyboard.press('ArrowRight');
  const layout=()=>p.evaluate(()=>JSON.parse(localStorage.getItem('osmo.workspace-layout.studio-compose.desktop')));
  const saved=await layout();assert.equal(saved.version,1);assert(saved.layout.find(v=>v.i==='shotCard').w<6);
  const target=group.locator('[data-workspace-panel="shotCard"]');
  await target.locator('[data-workspace-action="pin"]').click();
  assert((await layout()).layout.find(v=>v.i==='shotCard').static);
  await target.locator('[data-workspace-action="pin"]').click();
  await group.locator('[data-workspace-panel="waypointCard"] [data-workspace-action="hide"]').click();
  assert(await group.locator('[data-workspace-panel="waypointCard"]').isHidden());
  await group.locator('[data-workspace-action="restore"]').click();
  await group.locator('[data-workspace-arrange]').click();
  assert(await p.evaluate(()=>keepPad===document.querySelector('#pad')&&keepPlot===document.querySelector('#dPlot')));
  await p.reload();await until(()=>group.locator('[data-workspace-arrange]').isVisible(),'layout restored');
  assert.equal((await layout()).layout.find(v=>v.i==='shotCard').w,saved.layout.find(v=>v.i==='shotCard').w);
  await group.locator('[data-workspace-arrange]').click();
  const handle=group.locator('[data-workspace-panel="shotCard"] [data-workspace-action="move"]');await handle.scrollIntoViewIfNeeded();
  const box=await handle.boundingBox();await p.mouse.move(box.x+20,box.y+20);await p.mouse.down();await p.mouse.move(box.x+200,box.y+20,{steps:6});await p.mouse.up();
  await group.locator('[data-workspace-action="reset"]').click();await group.locator('[data-workspace-arrange]').click();
  for(const width of [1440,768,390]){
   await p.setViewportSize({width,height:1000});
   for(const workspace of ['compose','director','shoot','review','rig']){
    await p.locator(`.worktab[data-work="${workspace}"]`).click();await sleep(250);
    assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'horizontal page overflow '+workspace+' '+width);
    const overlap=await p.evaluate(()=>{const nodes=[...document.querySelectorAll('.osmo-workspace-panel')].filter(n=>n.getClientRects().length&&getComputedStyle(n).display!=='none');const rects=nodes.map(n=>({id:n.dataset.workspacePanel,r:n.getBoundingClientRect(),parent:n.parentElement}));for(let i=0;i<rects.length;i++)for(let j=i+1;j<rects.length;j++){let a=rects[i],b=rects[j];if(a.parent===b.parent&&a.r.left<b.r.right-1&&a.r.right>b.r.left+1&&a.r.top<b.r.bottom-1&&a.r.bottom>b.r.top+1)return[a.id,b.id];}return null;});
    assert.equal(overlap,null,'overlapping panels '+workspace+' '+width+': '+overlap);
   }
  }
  await p.setViewportSize({width:1440,height:1000});await p.getByRole('button',{name:'Settings',exact:true}).click();
  await until(()=>p.locator('#ssNotice').textContent().then(t=>t.includes('loaded')),'settings loaded');
  const form=p.locator('#ssAiForm');await form.locator('[name="provider_key"]').fill('sk-synthetic-browser-fixture');
  await form.locator('[name="enabled"]').check();await form.locator('[name="model"]').selectOption('gpt-5.6-terra');
  await form.getByRole('button',{name:'Save host preferences'}).click();
  await until(()=>p.locator('#ssNotice').textContent().then(t=>t.includes('saved')),'AI saved');
  assert.equal(await form.locator('[name="provider_key"]').inputValue(),'');
  assert(!JSON.stringify(await p.evaluate(()=>Object.assign({},localStorage))).includes('sk-synthetic'));
  const settings=await (await p.request.get(base+'/api/settings/ai')).json();
  assert(settings.settings.key_configured&&settings.settings.enabled);assert(!JSON.stringify(settings).includes('sk-synthetic'));
  if(shots)await p.screenshot({path:path.join(shots,'settings-ai-1440.png')});
  await p.setViewportSize({width:390,height:900});await sleep(150);
  assert(await p.locator('.osmo-dialog[open]').evaluate(n=>n.scrollWidth<=n.clientWidth+1),'settings phone overflow');
  if(shots)await p.screenshot({path:path.join(shots,'settings-ai-390.png')});
  await form.locator('[name="remove_key"]').check();await form.getByRole('button',{name:'Save host preferences'}).click();
  await until(()=>p.locator('#ssAiState').textContent().then(t=>t.includes('No provider')),'key removed');
  // Verify the copied value in-page, retaining only booleans. Never write a
  // generated crew credential to the real clipboard or assertion output.
  await p.evaluate(()=>{window.crewCopyChecks=[];Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.crewCopyChecks.push(typeof value==='string'&&value.length>0&&value===document.querySelector('#ssToken').value);}}});});
  await p.locator('[data-tab="crew"]').click();await p.locator('#ssCrewForm [name="label"]').fill('Review tablet');await p.locator('#ssIssue').click();
  await until(()=>p.locator('#ssToken').inputValue().then(Boolean),'crew issued');const token=await p.locator('#ssToken').inputValue();
  assert(await p.locator('#ssCopy').isEnabled(),'first crew copy enabled');await p.locator('#ssCopy').click();
  await until(()=>p.locator('#ssIssued').isHidden(),'first copied credential hidden');
  assert.equal(await p.locator('#ssToken').inputValue(),'','first copied credential cleared');
  const viewerContext=await browser.newContext();const viewer=await viewerContext.newPage();await viewer.goto(base+'/?t='+token);
  const identity=await (await viewer.request.get(base+'/api/access')).json();assert.equal(identity.identity.role,'viewer');
  const cookies=await viewerContext.cookies();assert(cookies.find(c=>c.name==='osmo_token').value===token,'crew cookie matches issued credential');
  await p.locator('#ssCrewList button').click();await until(()=>p.locator('#ssCrewList').textContent().then(t=>t.includes('No crew')),'crew revoked');
  assert.equal((await viewer.request.get(base+'/api/status')).status(),401);await viewerContext.close();
  await p.locator('#ssCrewForm [name="label"]').fill('Second review tablet');await p.locator('#ssIssue').click();
  await until(()=>p.locator('#ssToken').inputValue().then(Boolean),'second crew issued');
  assert(await p.locator('#ssIssued').isVisible(),'second credential shown after first copy hid it');
  assert((await p.locator('#ssToken').inputValue())!==token,'second credential is fresh');
  assert(await p.locator('#ssCopy').isEnabled(),'second crew copy enabled');await p.locator('#ssCopy').click();
  await until(()=>p.locator('#ssIssued').isHidden(),'second copied credential hidden');
  assert.equal(await p.locator('#ssToken').inputValue(),'','second copied credential cleared');
  assert.deepEqual(await p.evaluate(()=>window.crewCopyChecks),[true,true],'both copies used the currently displayed credential');
  await p.locator('#ssCrewList button').click();await until(()=>p.locator('#ssCrewList').textContent().then(t=>t.includes('No crew')),'second crew revoked');
  await p.locator('[data-tab="recovery"]').click();assert((await p.locator('#ssRecovery').textContent()).includes('consistent'));
  await p.locator('[data-tab="decoder"]').click();assert((await p.locator('#ssDecoder').textContent()).includes('18.1.0'));
  await p.getByRole('button',{name:'Close Studio settings'}).click();
  await p.getByRole('button',{name:'Guide',exact:true}).click();await p.locator('.osmo-guide').nth(2).locator('summary').click();assert(await p.getByText('Amber REC means requested',{exact:false}).isVisible());
  await p.getByRole('button',{name:'Close OsmoDesk field guide'}).click();
  await p.locator('.worktab[data-work="review"]').click();await p.locator('#btnLogTake').click();
  await p.locator('.osmo-export > summary').click();await until(()=>p.locator('#editorialTakes input').count().then(n=>n===1),'take export list');
  await p.locator('#editorialTakes input').check();const downloaded=p.waitForEvent('download');await p.locator('#editorialDownload').click();assert.equal((await downloaded).suggestedFilename(),'osmodesk-editorial.zip');
  // Existing specialist surfaces share settings/help without replacing their
  // native picture, tactile controls or transition engine.
  for(const route of ['/mobile','/cine']){await p.goto(base+route);await p.getByRole('button',{name:'Settings',exact:true}).click();await until(()=>p.locator('#ssNotice').textContent().then(t=>t.includes('loaded')),'specialist settings '+route);await p.getByRole('button',{name:'Close Studio settings'}).click();}
  assert.deepEqual(errors,[]);assert(!posts.some(v=>v.startsWith('/api/assistant/')),'no paid request');
  console.log('PASS: Snapgrid pointer/keyboard/resize/pin/hide/restore/reset/persistence, 15 overlap-free responsive layouts, stable control DOM, local AI key settings/removal, repeated crew issuance/copy and expiring cookie/revocation, guide/tooltips, editorial download, shared mobile/monitor settings. No provider/hardware call.');
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
