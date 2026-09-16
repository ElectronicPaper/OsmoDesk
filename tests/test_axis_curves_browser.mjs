// Isolated offline browser fixture only; all hardware operations fail closed.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {mkdir} from 'node:fs/promises';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.OSMO_PLAYWRIGHT_MODULE||'playwright');
if(!process.env.OSMO_PYTHON||!process.env.OSMO_SPATIAL_TEST_OUTPUT)throw Error('Explicit Python runtime and output directory required');
const host=spawn(process.env.OSMO_PYTHON,['-B','-m','tests.spatial_browser_host'],{cwd:fileURLToPath(new URL('..',import.meta.url)),windowsHide:true});
let browser;
try{
  const base=await new Promise((resolve,reject)=>{let buf='',err='';const timer=setTimeout(()=>reject(Error('Fixture timeout '+err)),10000);host.stderr.on('data',b=>err+=b);host.on('error',reject);host.stdout.on('data',b=>{buf+=b;if(buf.includes('\n')){clearTimeout(timer);try{resolve(JSON.parse(buf.split('\n')[0]).url);}catch(e){reject(e);}}});});
  browser=await chromium.launch({channel:'chrome',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[],posts=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/**',route=>{
    const req=route.request(),path=new URL(req.url()).pathname;
    if(req.method()==='POST'){
      posts.push(path);
      if(!['/api/move','/api/director/preview','/api/spatial/preview','/api/move/curves/preview'].includes(path)){errors.push('Unexpected POST '+path);return route.abort();}
    }
    return route.continue();
  });
  await page.goto(base);await page.waitForFunction(()=>window.OsmoSession?.generation!==null&&window.axisCurves);
  const shot={name:'Curve proof',waypoints:[
    {name:'Start',pitch:120,yaw:10,duration:1,dwell:.2,easing:'linear'},
    {name:'Reveal',pitch:130,yaw:40,duration:8,dwell:.3,easing:'ease-in-out-sine'},
    {name:'Hero',pitch:125,yaw:50,duration:5,dwell:.2,easing:'ease-in-out-sine'}]};
  await page.evaluate(async shot=>{await OsmoSession.post('/api/move',shot);},shot);
  await page.reload();await page.locator('[data-work=director]').click();
  const panel=page.locator('#axisCurvesPanel');await panel.scrollIntoViewIfNeeded();
  const preset=name=>panel.getByRole('button',{name,exact:true});
  const apply=()=>panel.getByRole('button',{name:'Apply to draft',exact:true});
  await expectReady();
  async function expectReady(){await panel.locator('.ac-preview-svg').waitFor();}
  await preset('Soft').click();await expectReady();
  await panel.getByLabel('Axis relationship').selectOption('pan_leads');
  await preset('Tilt').click();await preset('Late').click();await expectReady();
  assert.match(await panel.locator('.ac-axis-label').first().textContent(),/leader progress/);
  // Numeric edits persist, and keyboard handles retain focus and update fields.
  await panel.getByLabel('tilt x1 percent').fill('55');await panel.getByLabel('tilt x1 percent').press('Tab');await expectReady();
  const handle=panel.getByRole('slider',{name:'Control point 1 for tilt'});
  await handle.focus();await handle.press('ArrowRight');await handle.press('ArrowRight');
  assert.equal(await panel.getByLabel('tilt x1 percent').inputValue(),'59');
  await expectReady();
  // A real pointer drag changes canonical handles, not only the SVG path.
  const before=await panel.getByLabel('tilt x1 percent').inputValue();
  await handle.scrollIntoViewIfNeeded();const box=await handle.boundingBox();await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();await page.mouse.move(box.x+box.width/2-30,box.y+box.height/2-15,{steps:4});await page.mouse.up();await expectReady();
  assert.notEqual(await panel.getByLabel('tilt x1 percent').inputValue(),before);
  assert.equal(await apply().isEnabled(),true);
  await apply().click();await expectReady();
  const saved=await page.evaluate(async()=> (await(await fetch('/api/status')).json()).move);
  assert.equal(saved.waypoints[1].axis_link,'pan_leads');assert.equal(saved.waypoints[1].yaw_curve[1],0);assert.ok(saved.waypoints[1].pitch_curve);
  await page.reload();await page.locator('[data-work=director]').click();await panel.scrollIntoViewIfNeeded();await expectReady();
  assert.equal(await panel.getByLabel('Axis relationship').inputValue(),'pan_leads');
  for(const metric of ['position','speed','accel']){await panel.getByLabel('Planned motion metric').selectOption(metric);assert.equal(await panel.locator('.ac-preview-line').count(),2);}
  await preset('Tilt').click();await preset('Early').click();await expectReady();
  // No invalid stale candidate may replace a new shared draft.
  await page.evaluate(()=>{move.name='Newer local edit';axisCurves.invalidate();});
  assert.equal(await apply().isDisabled(),true);
  await preset('Refresh from draft').click();await expectReady();
  assert.equal(await apply().isDisabled(),true);
  // Flow conversion is explicit and remains local until Apply.
  await page.evaluate(()=>{delete move.waypoints[1].pitch_curve;delete move.waypoints[1].yaw_curve;delete move.waypoints[1].axis_link;move.waypoints[1].flow=true;axisCurves.refresh();});await expectReady();
  assert.equal(await preset('Soft').isDisabled(),true);
  await preset('Switch to manual curves').click();await preset('Soft').click();await expectReady();
  assert.equal(await page.evaluate(()=>move.waypoints[1].flow),true,'Flow not changed until Apply');
  await preset('Discard local edits').click();await expectReady();assert.equal(await preset('Soft').isDisabled(),true);
  await page.evaluate(()=>{move.waypoints[1].flow=false;axisCurves.refresh();});await preset('Soft').click();await expectReady();
  // Phone controls fit and retain >=44px targets; no horizontal panel overflow.
  await page.setViewportSize({width:390,height:844});await panel.scrollIntoViewIfNeeded();
  const layout=await panel.evaluate(root=>({overflow:root.scrollWidth>root.clientWidth+1,small:[...root.querySelectorAll('button,input,select')].filter(e=>!e.hidden&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height<43).map(e=>e.textContent)}));
  assert.equal(layout.overflow,false);assert.deepEqual(layout.small,[]);
  const output=process.env.OSMO_SPATIAL_TEST_OUTPUT;await mkdir(output,{recursive:true});await panel.screenshot({path:output+'/axis-curves-mobile.png'});
  await page.setViewportSize({width:1440,height:1080});await panel.screenshot({path:output+'/axis-curves-desktop.png'});
  assert.deepEqual(errors,[]);console.log(JSON.stringify({ok:true,workflows:['independent','linked coupling','numeric/keyboard/pointer','apply/reload','three metrics','stale draft','explicit Flow conversion','mobile'],posts:[...new Set(posts)]}));
}finally{if(browser)await browser.close();host.kill();}
