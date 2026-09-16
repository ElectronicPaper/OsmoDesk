// Explicit isolated-host browser gate. Never point this at an operator's host.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir, readFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.OSMO_PLAYWRIGHT_MODULE||'playwright');
let base=process.env.OSMO_SPATIAL_TEST_URL,host=null;
if(!process.env.OSMO_SPATIAL_TEST_OUTPUT)throw Error('Explicit test output directory required.');
if(!base){
  if(!process.env.OSMO_PYTHON)throw Error('Set OSMO_PYTHON to the project Python runtime.');
  host=spawn(process.env.OSMO_PYTHON,['-B','-m','tests.spatial_browser_host'],{cwd:fileURLToPath(new URL('..',import.meta.url)),windowsHide:true});
  try{base=await new Promise((resolve,reject)=>{let buffer='',err='';const timeout=setTimeout(()=>reject(Error('Fixture startup timeout: '+err)),10000);host.stderr.on('data',b=>err+=b);host.on('error',e=>{clearTimeout(timeout);reject(e);});host.on('exit',code=>{clearTimeout(timeout);reject(Error('Fixture exited '+code+': '+err));});host.stdout.on('data',b=>{buffer+=b;if(buffer.includes('\n')){clearTimeout(timeout);try{resolve(JSON.parse(buffer.split('\n')[0]).url);}catch(e){reject(e);}}});});}catch(e){host.kill();throw e;}
}
if(new URL(base).hostname!=='127.0.0.1')throw Error('Only an isolated loopback host is allowed.');
const output=process.env.OSMO_SPATIAL_TEST_OUTPUT;
await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true,channel:'chrome',args:['--enable-webgl','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const context=await browser.newContext({viewport:{width:1440,height:1080},acceptDownloads:true});
const page=await context.newPage(),errors=[],posts=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('dialog',d=>d.accept());
await page.route('**/api/**',async route=>{
  const req=route.request();
  if(req.method()==='POST'){
    const path=new URL(req.url()).pathname;posts.push(path);
    if(!['/api/move','/api/director/preview','/api/spatial/preview','/api/spatial/settle','/api/move/curves/preview'].includes(path)){
      errors.push('Unexpected POST '+path);return route.abort();
    }
  }
  return route.continue();
});
const shot={name:'Spatial offline acceptance',loop:false,ping_pong:false,waypoints:[
  {name:'Detail',pitch:120,yaw:10,duration:1,dwell:.3,easing:'linear',zoom:.2,cue:false},
  {name:'Reveal',pitch:123,yaw:25,duration:3,dwell:.5,easing:'ease-in-out',zoom:.6,cue:false},
  {name:'Hero',pitch:121,yaw:32,duration:2,dwell:.4,easing:'ease-in-out',zoom:.8,cue:false}
]};
const studio=()=>page.locator('.sp-studio');
const action=async name=>{await studio().locator(`[data-action="${name}"]`).click();await page.waitForFunction(()=>document.querySelector('.sp-studio').getAttribute('aria-busy')!=='true');};
const tab=async name=>studio().locator(`[data-tab="${name}"]`).click();
const note=()=>page.locator('#spStatus').textContent();
try{
  await page.goto(base+'/#director');
  await page.waitForFunction(()=>window.spatialStudio&&typeof OsmoSession!=='undefined'&&OsmoSession.generation);
  await action('refresh');assert.ok(!errors.length,'empty draft renders without exceptions');
  const seed=await page.evaluate(async shot=>OsmoSession.post('/api/move',shot),shot);
  assert.equal(seed.ok,true,JSON.stringify(seed));
  await page.reload();await studio().waitFor();
  await action('refresh');assert.equal(await page.evaluate(()=>spatialStudio.getState().analysis),true,await note());
  await action('enable');assert.equal(await page.evaluate(()=>spatialStudio.getState().view),true,await note());
  await page.locator('#spViewport').scrollIntoViewIfNeeded();
  await page.screenshot({path:output+'/desktop-initial.png'});
  // Synthetic colour/card fixture: no user media or network image services.
  const pano=await page.evaluate(()=>{const c=document.createElement('canvas');c.width=1024;c.height=512;const g=c.getContext('2d');g.fillStyle='#164563';g.fillRect(0,0,1024,256);g.fillStyle='#cf954f';g.fillRect(0,256,1024,256);g.fillStyle='white';g.font='bold 44px sans-serif';g.textAlign='center';g.fillText('NORTH · STUDIO',512,220);g.fillText('SOUTH',20,220);return c.toDataURL('image/png').split(',')[1];});
  await page.locator('#spPano').setInputFiles({name:'synthetic-studio.png',mimeType:'image/png',buffer:Buffer.from(pano,'base64')});
  await page.waitForFunction(()=>!!spatialStudio.getState().scene.panorama);await action('save');
  await page.locator('#spStill').setInputFiles({name:'synthetic-reference.png',mimeType:'image/png',buffer:Buffer.from(pano,'base64')});
  await page.waitForFunction(()=>spatialStudio.getState().scene.references.length===1);await action('save');
  // Private local persistence survives navigation, not a host upload.
  await page.reload();await studio().waitFor();await page.locator('#spLocations').selectOption('Untitled location');await action('load');await action('enable');
  assert.equal(await page.evaluate(()=>spatialStudio.getState().scene.references.length),1);
  await page.locator('#spAspect').selectOption('0.5625');await page.waitForFunction(()=>!document.querySelector('.sp-studio').inert);
  await page.locator('#spViewport').scrollIntoViewIfNeeded();await page.screenshot({path:output+'/portrait-frame.png'});
  await tab('marks');await page.locator('#spMarkName').fill('Hero product');await action('mark-pose');await action('add-mark');
  assert.match(await page.locator('#spChecks').textContent(),/Hero product/);
  await page.locator('.sp-studio summary').click();await page.locator('#spRevealPoint').selectOption('2');await action('reveal');
  assert.equal(await page.evaluate(()=>spatialStudio.getState().variant),true,await note());
  await action('discard');await page.locator('#spTempo').fill('1.5');await action('treatment');
  assert.equal(await page.locator('#spApply').isEnabled(),true,await note());
  await page.locator('#spViewport').scrollIntoViewIfNeeded();await page.waitForTimeout(200);await page.screenshot({path:output+'/compare.png'});
  await action('apply');assert.equal(await page.locator('#spUndo').isEnabled(),true,await note());
  await action('undo');const current=await page.evaluate(async()=> (await(await fetch('/api/status')).json()).move);
  assert.deepEqual(current.waypoints.map(p=>p.duration),[1,3,2]);
  if(host){await tab('takes');await action('takes');await action('ghost');await action('settle');assert.match(await page.locator('#spSettle').textContent(),/observed angular quiet window/);await action('clear-ghost');}
  await tab('recipes');await page.locator('#spRecipeName').fill('Product rhythm');await action('save-recipe');await action('recipe');
  assert.equal(await page.evaluate(()=>spatialStudio.getState().variant),true,await note());
  await tab('share');await page.locator('#spIncludeImages').check();
  const downloading=page.waitForEvent('download');await action('storyboard');const file=await downloading;
  const exported=await readFile(await file.path(),'utf8');assert.ok(exported.includes('data:image/png;base64,'),'rendered reference frames included');assert.ok(!exported.includes('<script'),'scriptless storyboard');
  const locationDownload=page.waitForEvent('download');await action('export');const location=JSON.parse(await readFile(await(await locationDownload).path(),'utf8'));assert.equal(location.schema,1);assert.equal(location.recipes.length,1);
  await page.locator('#spImport').setInputFiles({name:'roundtrip.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(location))});await page.waitForFunction(()=>!document.querySelector('.sp-studio').inert);
  await action('refresh');
  await page.locator('#spView').selectOption('flat');await page.waitForFunction(()=>!document.querySelector('.sp-studio').inert);await action('play');await page.waitForTimeout(220);assert.equal(await page.evaluate(()=>spatialStudio.getState().playing),true);await action('play');
  // No overflow or undersized buttons in the new panel at phone width.
  await page.setViewportSize({width:390,height:844});await tab('location');await studio().scrollIntoViewIfNeeded();await page.screenshot({path:output+'/mobile-location.png',fullPage:true});
  const locationHeight=await studio().evaluate(root=>root.clientHeight);
  for(const name of ['location','marks','feel','takes','recipes','share']){
    await tab(name);const bad=await studio().evaluate(root=>[...root.querySelectorAll('button')].filter(e=>e.getBoundingClientRect().width&&e.getBoundingClientRect().height<43).map(e=>e.textContent));assert.deepEqual(bad,[],name+' minimum touch target');
    const overflow=await studio().evaluate(root=>root.scrollWidth>root.clientWidth+1);assert.equal(overflow,false,name+' panel overflow');
  }
  assert.ok(await studio().evaluate(root=>root.clientHeight)<locationHeight,'compact tab shrinks auto-height panel');
  await page.setViewportSize({width:1440,height:1080});await page.locator('#spView').selectOption('overview');await page.waitForFunction(()=>!document.querySelector('.sp-studio').inert);await page.locator('#spViewport').scrollIntoViewIfNeeded();
  await page.waitForTimeout(200);await page.screenshot({path:output+'/overview.png'});
  await page.evaluate(()=>document.querySelector('#spViewport canvas').getContext('webgl2').getExtension('WEBGL_lose_context').loseContext());
  await page.waitForFunction(()=>!spatialStudio.getState().view);assert.equal(await page.locator('#spFlat').isVisible(),true);
  // GPU resources are bounded across rebuilds; hiding and stale loads are explicit.
  const lifecycle=await page.evaluate(async url=>{
    const node=document.createElement('div');Object.assign(node.style,{position:'fixed',top:'0',left:'0',width:'400px',height:'300px',zIndex:'99999'});document.body.append(node);
    const view=createSpatialView(node),data={hfov:84,aspect:16/9,panorama:{url:'data:image/png;base64,'+url,pitch:0,yaw:0},samples:[{pitch:0,yaw:0},{pitch:10,yaw:5}],ghostSegments:[[{pitch:0,yaw:1},{pitch:10,yaw:6}]]};
    try{
      let baseline;
      for(let i=0;i<12;i++){view.setData(data);await view.ready();view.setPose({pitch:0,yaw:0});await new Promise(r=>requestAnimationFrame(r));const m=view.stats().memory;if(!baseline)baseline=m;else if(m.geometries!==baseline.geometries||m.textures!==baseline.textures)throw Error('GPU resource count grew');}
      view.setActive(false);let refused=false;try{view.capture();}catch{refused=true;}
      view.setActive(true);view.setData({});await view.ready();const cleared=view.stats().memory;
      const bad={...data,panorama:{...data.panorama,url:'data:image/png;base64,YmFk'}};view.setData(bad);let badRejected=false;try{await view.ready();}catch{badRejected=true;}
      view.setData(data);const old=view.ready();view.setData({});let staleRejected=false;try{await old;}catch{staleRejected=true;}
      return {baseline,cleared,refused,badRejected,staleRejected};
    }finally{view.dispose();node.remove();}
  },pano);
  assert.equal(lifecycle.refused,true);assert.equal(lifecycle.badRejected,true);assert.equal(lifecycle.staleRejected,true);assert.equal(lifecycle.cleared.textures,0);assert.ok(lifecycle.cleared.geometries<=2);
  // A and B have distinct cue times; both must pause, including reduced motion.
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.evaluate(async shot=>{shot.waypoints[1].cue=true;shot.waypoints[0].dwell=0;shot.waypoints[1].duration=.3;shot.waypoints[1].dwell=0;await OsmoSession.post('/api/move',shot);},structuredClone(shot));
  await page.reload();await studio().waitFor();await action('refresh');await tab('feel');await page.locator('#spTempo').fill('2');await action('treatment');await action('play');
  await page.waitForFunction(()=>document.querySelector('#spStatus').textContent.includes('cue in A'));await action('play');await page.waitForFunction(()=>document.querySelector('#spStatus').textContent.includes('cue in B'));
  assert.equal(await page.evaluate(()=>spatialStudio.getState().playing),false);
  await page.locator('[data-work=compose]').click();assert.equal(await page.evaluate(()=>spatialStudio.getState().playing),false);
  assert.deepEqual(errors,[]);console.log(JSON.stringify({ok:true,workflows:['3D','references','local persistence','crop','marks','reveal','A/B','apply+undo','recipes','storyboard','location export+import','playback','mobile tabs','context-loss fallback'],postRoutes:[...new Set(posts)],output}));
}finally{await browser.close();if(host)host.kill();}
