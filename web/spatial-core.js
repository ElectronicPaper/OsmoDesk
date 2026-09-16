/* Local reference media only. The host remains the sole authored-motion owner. */
(function (global) {
  'use strict';
  const MAX_BYTES=24*1024*1024, MAX_IMAGE=4*1024*1024;
  const clone=x=>JSON.parse(JSON.stringify(x));
  const finite=(n,min,max)=>typeof n==='number'&&Number.isFinite(n)&&n>=min&&n<=max;
  const text=(s,max=128)=>typeof s==='string'&&s.length<=max&&!/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(s);
  const image=s=>typeof s==='string'&&s.length<=MAX_IMAGE&&/^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(s);
  function empty(){return {schema:1,name:'Untitled location',hfov:84,captureAspect:16/9,aspect:16/9,margin:.1,panorama:null,references:[],markers:[],recipes:[]};}
  function validate(raw){
    if(!raw||raw.schema!==1||!text(raw.name)||!finite(raw.hfov,10,150)||!finite(raw.aspect,.3,4)||!finite(raw.margin,0,.4))throw Error('Unsupported or invalid location file.');
    if(JSON.stringify(raw).length>MAX_BYTES)throw Error('Location exceeds the 24 MB limit.');
    const out=empty();for(const key of ['name','hfov','aspect','margin'])out[key]=raw[key];
    out.captureAspect=raw.captureAspect??16/9;
    if(!finite(out.captureAspect,.3,4))throw Error('Invalid capture aspect ratio.');
    const pose=x=>x&&finite(x.pitch,-3600,3600)&&finite(x.yaw,-3600,3600);
    if(raw.panorama){if(!pose(raw.panorama)||!image(raw.panorama.url))throw Error('Invalid panorama image or alignment.');out.panorama={url:raw.panorama.url,pitch:raw.panorama.pitch,yaw:raw.panorama.yaw};}
    if(!Array.isArray(raw.references)||raw.references.length>24)throw Error('Use at most 24 reference stills.');
    out.references=raw.references.map(r=>{
      if(!pose(r)||!text(r.name)||!image(r.url)||!finite(r.hfov,10,150)||!finite(r.aspect,.3,4))throw Error('Invalid reference still.');
      return {name:r.name,url:r.url,pitch:r.pitch,yaw:r.yaw,hfov:r.hfov,aspect:r.aspect};
    });
    if(!Array.isArray(raw.markers)||raw.markers.length>24)throw Error('Use at most 24 framing marks.');
    out.markers=raw.markers.map(m=>{
      if(!pose(m)||!text(m.name)||!m.name.trim()||!['subject','avoid'].includes(m.kind)||!(m.not_before===null||finite(m.not_before,0,86400))||!finite(m.hold,0,3600))throw Error('Invalid framing mark.');
      return {name:m.name,pitch:m.pitch,yaw:m.yaw,kind:m.kind,not_before:m.not_before,hold:m.hold};
    });
    if(!Array.isArray(raw.recipes)||raw.recipes.length>20)throw Error('Use at most 20 recipes per location.');
    out.recipes=raw.recipes.map(r=>{
      if(!text(r.name)||!Array.isArray(r.roles)||r.roles.length<2||r.roles.length>24)throw Error('Invalid recipe.');
      const roles=r.roles.map(p=>{if(!text(p.name)||!finite(p.duration,.1,3600)||!finite(p.dwell,0,3600)||!text(p.easing,40))throw Error('Invalid recipe role.');return {name:p.name,duration:p.duration,dwell:p.dwell,easing:p.easing};});
      return {name:r.name,roles};
    });return out;
  }
  function sampleAt(samples,t){
    if(!samples?.length)return null;
    if(t<=samples[0].time)return {...samples[0]};
    let lo=0,hi=samples.length-1;while(lo<hi){const mid=(lo+hi)>>1;if(samples[mid].time<t)lo=mid+1;else hi=mid;}
    const b=samples[lo],a=samples[Math.max(0,lo-1)],u=b.time===a.time?0:Math.max(0,Math.min(1,(t-a.time)/(b.time-a.time)));
    // Host supplies unwrapped display angles. Never choose a different route.
    return {time:t,pitch:a.pitch+(b.pitch-a.pitch)*u,yaw:a.yaw+(b.yaw-a.yaw)*u};
  }
  // Delivery is a centred crop of the assumed capture, not a wider lens.
  function framing(scene){return {hfov:2*Math.atan(Math.tan(scene.hfov*Math.PI/360)*Math.min(1,scene.aspect/scene.captureAspect))*180/Math.PI,aspect:scene.aspect};}
  function cueEvents(a,b){
    const events=[a,b].flatMap((v,lane)=>(v?.preview?.events||[]).filter(e=>e.type==='cue').map(e=>({...e,key:lane+':'+e.time+':'+e.beat_index,lane:lane?'B':'A'}))).sort((x,y)=>x.time-y.time);
    return [...new Map(events.map(e=>[e.key,e])).values()];
  }
  function ghostSegments(trace){
    if(!Array.isArray(trace)||trace.length>20000)throw Error('Invalid take trace.');
    const result=[];let seg=[],prev=null;
    for(const p of trace){
      if(!Array.isArray(p)||p.length!==3||!p.every(Number.isFinite)||p[0]<0)throw Error('Invalid take trace point.');
      if(prev&&p[0]<=prev[0])throw Error('Unordered take trace.');
      if(prev&&p[0]-prev[0]>1){if(seg.length)result.push(seg);seg=[];}
      seg.push({time:p[0],pitch:p[1],yaw:p[2]});prev=p;
    }if(seg.length)result.push(seg);return result;
  }
  const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function storyboard({name,beats,images=[],includeImages=false,scope=''}){
    const rows=beats.slice(0,200).map((b,i)=>`<article><h2>${escape(b.name||'P'+(i+1))}</h2>${includeImages&&image(images[i])?`<img alt="Approximate rehearsal reference" src="${images[i]}">`:''}<p>Arrival ${Number(b.arrival).toFixed(2)} s · Hold ${Math.max(0,b.dwell_end-b.dwell_start).toFixed(2)} s${b.cue?' · Wait for operator cue':''}</p></article>`).join('');
    return `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'"><title>${escape(name)} · OsmoDesk storyboard</title><style>body{font:16px system-ui;max-width:1000px;margin:32px auto;padding:16px;background:#101820;color:#e1edf2}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}article{border:1px solid #51616d;border-radius:12px;padding:16px}img{width:100%}small,p{color:#b5c9d3}@media print{body{background:white;color:black}article{break-inside:avoid}}</style><h1>${escape(name)}</h1><p>OsmoDesk • Reference storyboard • No camera controls</p><p>${escape(scope)}</p><main>${rows}</main><p>Rotation-only rehearsal. Imagery and field of view are approximate; not proof of captured footage. Times exclude operator cue waits. No external requests or scripts.</p></html>`;
  }
  function openStore(){
    return new Promise((resolve,reject)=>{const req=indexedDB.open('osmodesk-spatial-references',1);req.onupgradeneeded=()=>req.result.createObjectStore('locations',{keyPath:'name'});req.onerror=()=>reject(Error('Browser storage unavailable. Export your location to keep it.'));req.onsuccess=()=>resolve(req.result);});
  }
  async function storage(action,name,value){
    const db=await openStore();return new Promise((resolve,reject)=>{
      const tx=db.transaction('locations',action==='save'||action==='delete'?'readwrite':'readonly'),table=tx.objectStore('locations');let req;
      if(action==='save')req=table.put(validate(value));else if(action==='delete')req=table.delete(name);else if(action==='list')req=table.getAllKeys();else req=table.get(name);
      let result;req.onsuccess=()=>{result=req.result;};tx.oncomplete=()=>{db.close();resolve(result);};tx.onerror=tx.onabort=()=>{db.close();reject(Error('Location was not saved. Browser storage is unavailable or full; export a copy.'));};
    });
  }
  async function decode(file,panorama=false){
    if(!['image/jpeg','image/png','image/webp'].includes(file.type)||file.size>12*1024*1024)throw Error('Choose a PNG, JPEG or WebP up to 12 MB.');
    const url=URL.createObjectURL(file),img=new Image();
    try{await new Promise((resolve,reject)=>{img.onload=resolve;img.onerror=()=>reject(Error('Image could not be decoded.'));img.src=url;});
      if(img.width*img.height>32*1024*1024)throw Error('Image exceeds 32 megapixels. Resize it first.');
      if(panorama&&Math.abs(img.width/img.height-2)>.08)throw Error('A panorama must be a 2:1 equirectangular image.');
      const limit=panorama?4096:1280,scale=Math.min(1,limit/img.width,2048/img.height),c=document.createElement('canvas');c.width=Math.max(1,Math.round(img.width*scale));c.height=Math.max(1,Math.round(img.height*scale));c.getContext('2d').drawImage(img,0,0,c.width,c.height);
      const result={url:c.toDataURL('image/jpeg',.85),aspect:img.width/img.height};if(!image(result.url))throw Error('Compressed image is too large. Resize it first.');return result;
    }finally{URL.revokeObjectURL(url);}
  }
  const api={empty,validate,sampleAt,framing,cueEvents,ghostSegments,storyboard,storage,decode,escape,MAX_BYTES};
  global.OsmoSpatialCore=api;if(typeof module!=='undefined')module.exports=api;
})(typeof window==='undefined'?globalThis:window);
