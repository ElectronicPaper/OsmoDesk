/* One transport contract for all browser surfaces. No persisted identity,
 * hardware reconnection, command replay, or competing draft store. */
(() => {
  'use strict';
  const authoring = new Set(['/api/move','/api/move/retime','/api/move/offset',
    '/api/move/reference','/api/moves/save','/api/moves/load','/api/moves/delete',
    '/api/waypoint','/api/setup','/api/slate','/api/take','/api/take/circle','/api/workspace/recovery']);
  const newId = () => typeof crypto.randomUUID==='function' ? crypto.randomUUID()
    : Array.from(crypto.getRandomValues(new Uint8Array(16)),n=>n.toString(16).padStart(2,'0')).join('');
  const client = newId(); // Also works on the documented plain-HTTP LAN path.
  const retiredEpochs=new Set();
  let motionSequence=0, gesture=null;
  let generation=null, conflict=false, pending=0, queue=Promise.resolve();
  const notify = () => window.dispatchEvent(new CustomEvent('osmo-conflict'));
  const conflictResult = () => ({ok:false,conflict:true,error:'Workspace changed in another tab. Export your edits or reload the host draft.'});
  function observe(status,{dirty=false}={}) {
    const incoming=status?.workspace?.generation;
    if (!incoming || pending || conflict || incoming===generation) return false;
    if(retiredEpochs.has(incoming.split(':')[0]))return false;
    if (generation) {
      const [epoch,rev]=generation.split(':'), [nextEpoch,nextRev]=incoming.split(':');
      if (epoch===nextEpoch && +nextRev < +rev) return false;
      if (dirty) {conflict=true;notify();return false;}
      if(epoch!==nextEpoch)retiredEpochs.add(epoch);
    }
    const changed=generation!==null;generation=incoming;return changed;
  }
  async function request(path,body) {
    const edit=authoring.has(path);
    if (edit && conflict) return conflictResult();
    if (edit && generation===null) return {ok:false,error:'Wait for the workspace to load before editing.'};
    const headers={'Content-Type':'application/json','X-Osmo-Client':client};
    if(['/api/stick','/api/grab','/api/release'].includes(path)){
      const moving=path==='/api/grab'||(path==='/api/stick'&&(body?.tilt||body?.pan));
      if(moving&&!gesture)gesture=newId();
      headers['X-Osmo-Sequence']=String(++motionSequence);
      headers['X-Osmo-Gesture']=gesture||'idle';
      if(!moving)gesture=null;
    }
    // STOP closes this gesture at the host. Keep its identity until a real
    // neutral/release packet, so a still-held pad cannot manufacture a retry.
    if (edit) headers['X-Osmo-Generation']=generation;
    if (edit) pending++;
    try {
      const response=await fetch(path,{method:'POST',headers,body:JSON.stringify(body||{}),signal:AbortSignal.timeout(5000)});
      const result=await response.json().catch(()=>({}));
      if (response.status===409 && result.conflict) {conflict=true;notify();}
      if (!response.ok || result.ok===false) return {...result,ok:false,status:response.status,error:result.error||`HTTP ${response.status}`};
      if (edit && result.workspace?.generation) generation=result.workspace.generation;
      return result;
    } catch (error) {return {ok:false,error:'Host unreachable: '+error.message};}
    finally {if(edit) pending--;}
  }
  window.OsmoSession={client,observe,
    isRetired:status=>retiredEpochs.has(status?.workspace?.generation?.split(':')[0]),
    get generation(){return generation;},get conflicted(){return conflict;},get pending(){return pending;},
    reset(status){
      const incoming=status?.workspace?.generation;
      if(!incoming||retiredEpochs.has(incoming.split(':')[0]))return false;
      if(generation&&generation.split(':')[0]!==incoming.split(':')[0])retiredEpochs.add(generation.split(':')[0]);
      generation=incoming;conflict=false;return true;
    },
    post(path,body){
      if (!authoring.has(path)) return request(path,body);
      // Serialize authoring only. STOP and live input never wait for disk work.
      const result=queue.then(()=>request(path,body));queue=result.catch(()=>{});return result;
    }};
})();
