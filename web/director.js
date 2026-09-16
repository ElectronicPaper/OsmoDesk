/* Data-only rehearsal. The host's Move.sample owns every pose; this module
   interpolates only the display cursor between returned visual samples. */
(() => {
  'use strict';
  window.createDirector = (root, api) => {
    root.innerHTML=`
      <div class="d-heading"><div><div class="d-kicker">Director / motion rehearsal</div>
        <h2>Give every move a moment.</h2><p>Read the rhythm, rehearse the beats, then choose the timing. Your camera stays still here.</p></div>
        <span class="d-badge">PREVIEW ONLY · NO CAMERA MOVEMENT</span></div>
      <div class="d-layout"><section class="d-stage" aria-label="Offline shot preview">
        <div class="d-stagebar"><strong id="dName">No shot loaded</strong><span id="dViewLabel">Authored path</span></div>
        <div class="d-plot"><canvas id="dPlot" role="img" aria-label="Gimbal angle path. Text positions and timing are available in the beat buttons below."></canvas>
          <div class="d-legend"><span>GIMBAL PATH · PAN × TILT</span><span id="dPose">No positions</span></div></div>
        <div class="d-transport"><div class="d-time"><output id="dTime">00.00 s</output><span id="dEnd">—</span></div>
          <input id="dScrub" type="range" min="0" max="1" step="0.01" value="0" aria-label="Offline rehearsal time" disabled>
          <div class="d-actions"><button id="dPlay" class="primary" disabled>▶ Preview shot</button><button id="dReset" disabled>↤ Start</button><button id="dNext" disabled>Next beat →</button></div>
          <div id="dCue" class="d-cue" hidden><span id="dCueText"></span><div class="d-actions"><button id="dContinue">Continue preview →</button></div></div>
          <p id="dState" class="d-foot" role="status">Add at least two positions in Compose to rehearse a shot.</p></div>
      </section><aside class="d-pace" aria-labelledby="dPaceTitle"><div class="d-kicker">Shot pace</div><h3 id="dPaceTitle">Timing, with intent.</h3>
        <div id="dDuration" class="d-duration">—</div><p id="dTimingNote">Programmed motion time, excluding human cue waits.</p>
        <label for="dTarget">Target motion duration</label><div class="d-input"><input id="dTarget" type="number" min="0.1" max="86400" step="0.1" placeholder="e.g. 12"><span>s</span></div>
        <div class="d-actions"><button id="dFit" disabled>Check timing</button><button id="dAuto" disabled>Find workable timing</button></div>
        <div id="dProposal" class="d-proposal" hidden><strong id="dProposalTitle"></strong><p id="dProposalText"></p><ul id="dChanges"></ul>
          <div class="d-actions"><button id="dPreviewProposal" disabled>Preview proposal</button><button id="dApply" class="primary" disabled>Apply timing</button></div></div>
        <div class="d-actions"><button id="dUndo" hidden>↶ Restore previous timing</button></div>
        <p class="d-foot">Timing scales travel and fixed holds together. Cues still wait for you. No automatic arming, motion, or recording.</p>
      </aside></div>
      <section class="d-score"><div class="d-scorehead"><div><div class="d-kicker">Shot score</div><h3>Your framing beats</h3></div><div class="d-actions"><button id="dEdit" disabled>Edit selected beat ↗</button><button id="dRefresh">Refresh analysis</button></div></div>
        <div id="dBeats" class="d-beats" aria-label="Select a framing beat"></div>
        <div class="d-evidence"><h3 id="dCheckTitle">Path check</h3><ul id="dFindings" class="d-findings"></ul><p id="dLimits" class="d-foot"></p></div>
        <p class="d-foot">This is sampled gimbal-angle arithmetic, not a view through the lens or a physical guarantee. Zoom tracks execute when camera readback permits; this preview does not prove lens response. Verify actual footage before relying on a shot.</p>
      </section>`;
    const el=id=>root.querySelector('#'+id);
    let data=null, shown=null, candidate=null, original=null, undo=null, requestId=0, displayPoints=[];
    let t=0, playing=false, frame=0, last=0, selected=0, waiting=null, consumed=new Set(), applying=false;
    const reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)');
    let lastDraw=0;
    const end=()=>shown?.samples?.at(-1)?.time||0;
    const seconds=n=>`${Number(n).toFixed(2)} s`;
    function pause(){playing=false;cancelAnimationFrame(frame);el('dPlay').textContent='▶ Preview shot';}
    function clearAnalysis(message){pause();data=shown=candidate=original=null;displayPoints=[];waiting=null;t=0;
      el('dCue').hidden=true;el('dProposal').hidden=true;
      for(const id of ['dPlay','dReset','dNext','dScrub','dFit','dAuto','dEdit','dApply','dPreviewProposal'])el(id).disabled=true;
      el('dBeats').replaceChildren();el('dFindings').replaceChildren();
      el('dCheckTitle').textContent='Path not assessed';el('dLimits').textContent='';
      el('dDuration').textContent=el('dEnd').textContent='—';
      el('dState').textContent=message;display();
    }
    function invalidate(){requestId++;undo=null;el('dUndo').hidden=true;
      clearAnalysis('Draft changed. Refresh analysis to rehearse the current shot.');
      window.copilot?.invalidate();
      window.spatialStudio?.invalidate();
    }
    function pose(){
      const points=displayPoints;if(!points.length)return null;
      let hi=points.findIndex(p=>p.time>=t);if(hi<0)hi=points.length-1;
      const b=points[hi],a=points[Math.max(0,hi-1)],mix=b.time===a.time?0:(t-a.time)/(b.time-a.time);
      return {pitch:a.pitch+(b.pitch-a.pitch)*mix,yaw:a.yaw+(b.yaw-a.yaw)*mix};
    }
    function draw(){
      const canvas=el('dPlot'),rect=canvas.getBoundingClientRect();if(rect.width<1)return;
      const dpr=Math.min(2,window.devicePixelRatio||1),w=rect.width,h=rect.height;
      canvas.width=w*dpr;canvas.height=h*dpr;const c=canvas.getContext('2d');c.scale(dpr,dpr);
      c.clearRect(0,0,w,h);const pts=displayPoints;
      c.strokeStyle='#24333e';c.lineWidth=1;
      for(let x=30;x<w;x+=45){c.beginPath();c.moveTo(x,16);c.lineTo(x,h-26);c.stroke();}
      for(let y=22;y<h-20;y+=40){c.beginPath();c.moveTo(22,y);c.lineTo(w-22,y);c.stroke();}
      if(!pts.length){c.fillStyle='#9aaebc';c.font='13px sans-serif';c.textAlign='center';c.fillText('Your path will appear here',w/2,h/2);return;}
      const ys=pts.map(p=>p.yaw),ps=pts.map(p=>p.pitch);
      const ym=(Math.max(...ys)+Math.min(...ys))/2,pm=(Math.max(...ps)+Math.min(...ps))/2;
      const span=Math.max(Math.max(...ys)-Math.min(...ys),Math.max(...ps)-Math.min(...ps),8)*1.4;
      const scale=Math.min(w-80,h-70)/span;
      const xy=p=>[w/2+(p.yaw-ym)*scale,h/2-(p.pitch-pm)*scale];
      c.strokeStyle='#6ca1ae';c.lineWidth=2;c.beginPath();pts.forEach((p,i)=>{const q=xy(p);i?c.lineTo(...q):c.moveTo(...q);});c.stroke();
      const beats=shown.beats.filter(b=>b.direction==='forward');
      beats.forEach(b=>{const p=pts.reduce((best,p)=>Math.abs(p.time-b.arrival)<Math.abs(best.time-b.arrival)?p:best,pts[0]),q=xy(p);
        c.fillStyle='#172832';c.strokeStyle='#a7cbd1';c.beginPath();c.arc(...q,11,0,Math.PI*2);c.fill();c.stroke();
        c.fillStyle='#deeaee';c.font='10px ui-monospace,monospace';c.textAlign='center';c.fillText(String(b.index+1),q[0],q[1]+3);});
      const p=pose();if(p){const [x,y]=xy(p);c.strokeStyle='#9be7e9';c.fillStyle='#a3e2e4';c.lineWidth=1.5;
        c.beginPath();c.arc(x,y,5,0,Math.PI*2);c.fill();c.beginPath();c.arc(x,y,18,0,Math.PI*2);c.stroke();
        c.strokeStyle='#728c9a';c.beginPath();c.moveTo(24,h-25);c.lineTo(84,h-25);c.stroke();
        c.fillStyle='#9bafbd';c.font='11px ui-monospace,monospace';c.textAlign='left';c.fillText(`${(60/scale).toFixed(1)}°`,24,h-8);}
    }
    function display(force=true){
      el('dScrub').value=t;el('dTime').textContent=seconds(t);
      const p=pose();el('dPose').textContent=p?`PAN ${p.yaw.toFixed(1)}° · TILT ${p.pitch.toFixed(1)}°`:'No positions';
      const beat=(shown?.beats||[]).filter(b=>b.arrival<=t+.0001).at(-1);if(beat)selected=beat.index;
      el('dBeats').querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(+b.dataset.index===selected)));
      // Keep user-requested playback and its clock accurate, but use a quiet
      // stepped cursor instead of continuous canvas motion when requested.
      const now=performance.now();
      if(force||!reducedMotion.matches||now-lastDraw>=500){draw();lastDraw=now;}
    }
    function seek(value){pause();waiting=null;el('dCue').hidden=true;t=Math.max(0,Math.min(end(),value));
      consumed=new Set((shown?.events||[]).map((e,i)=>e.type==='cue'&&e.time<t?i:-1));display();}
    function tick(now){
      if(!playing)return;const next=Math.min(end(),t+Math.max(0,(now-last)/1000));last=now;
      const cueIndex=(shown.events||[]).findIndex((e,i)=>e.type==='cue'&&!consumed.has(i)&&e.time>=t-.0001&&e.time<=next);
      if(cueIndex>=0){const cue=shown.events[cueIndex];t=cue.time;waiting=cueIndex;pause();
        el('dCueText').textContent=`Cue at P${cue.beat_index+1} · ${cue.name||'Untitled beat'}. Live duration depends on your GO.`;
        el('dCue').hidden=false;display();return;}
      t=next;display(false);if(t>=end()){pause();display();el('dState').textContent='Preview complete. Camera unchanged.';return;}frame=requestAnimationFrame(tick);
    }
    function play(){if(!shown?.samples.length)return;if(playing){pause();return;}if(waiting!==null)return;window.spatialStudio?.pause();
      if(t>=end())seek(0);playing=true;last=performance.now();el('dPlay').textContent='Ⅱ Pause preview';frame=requestAnimationFrame(tick);}
    function renderShown(){
      // Equivalent angle turns keep the chart continuous across +/-180.
      // This is display normalization only, never a new commanded path.
      displayPoints=[];
      for(const point of shown.samples){const previous=displayPoints.at(-1),p={...point};
        if(previous)for(const axis of ['pitch','yaw'])p[axis]=previous[axis]+((p[axis]-previous[axis]+180)%360+360)%360-180;
        displayPoints.push(p);}
      el('dDuration').textContent=seconds(shown.timing.programmed_duration??end());
      el('dTimingNote').textContent=(shown.timing.loop_uncertain?'One loop cycle; live duration is open-ended. ':shown.timing.forward_duration!==shown.timing.programmed_duration?'Round trip, including reverse. ':'Programmed travel and fixed holds. ')+(shown.timing.cue_uncertain?'Plus live cue waits—total shoot time is unknown.':'No cue waits.');
      el('dScrub').max=end();el('dEnd').textContent=seconds(end());el('dBeats').replaceChildren();
      for(const b of shown.beats){if(b.direction!=='forward')continue;const button=document.createElement('button');button.className='d-beat';button.dataset.index=b.index;
        const title=document.createElement('b');title.textContent=`P${b.index+1} · ${b.name||'Untitled beat'}`;
        const timing=document.createElement('span');timing.textContent=`${seconds(b.arrival)}${b.dwell_end>b.dwell_start?' · hold '+seconds(b.dwell_end-b.dwell_start):''}${b.cue?' · YOUR CUE':''}`;
        const w=(candidate&&el('dViewLabel').textContent==='Timing proposal'?candidate:original)?.waypoints?.[b.index];
        const angle=document.createElement('span');angle.textContent=w?`Pan ${w.yaw.toFixed(1)}° · Tilt ${w.pitch.toFixed(1)}°`:'';
        button.append(title,timing,angle);button.onclick=()=>{seek(b.arrival);selected=b.index;display();};el('dBeats').append(button);}
      const findings=shown.preflight.findings;el('dFindings').replaceChildren();
      el('dCheckTitle').textContent=shown.preflight.ok?'Sampled path check passed':'Resolve before shooting';
      for(const finding of findings){const item=document.createElement('li');
        const advice=finding.kind==='travel'?'Edit the framing or route; timing alone cannot fix travel.':finding.kind==='too fast'?'Allow more motion time or reduce angular travel.':finding.kind==='dead band'?'Try less time or more angular travel.':'Simplify the path for a bounded assessment.';
        item.textContent=`${finding.detail}. ${advice}`;el('dFindings').append(item);}
      el('dLimits').textContent=`Peaks: tilt ${shown.preflight.peak_pitch_dps.toFixed(2)}°/s · pan ${shown.preflight.peak_yaw_dps.toFixed(2)}°/s. ${shown.rig?.max_dps??data?.rig?.max_dps??'—'}°/s selected cap. ${shown.preflight.samples} assessment samples.`;
      for(const id of ['dPlay','dReset','dNext','dScrub','dEdit'])el(id).disabled=!shown.samples.length;
      seek(0);
    }
    async function analyse(target=null){
      const ticket=++requestId;clearAnalysis('Checking the authored path…');
      const draft=api.getMove();const result=await api.post('/api/director/preview',{move:draft,...target!==null?{target_duration:target}:{}});
      if(ticket!==requestId)return false;
      if(!result.ok){el('dState').textContent=result.error;return false;}
      data=shown=result.director;original=structuredClone(draft);candidate=data.proposal.move;
      el('dName').textContent=draft.name||'Untitled shot';el('dViewLabel').textContent='Authored path';
      el('dFit').disabled=el('dAuto').disabled=!data.samples.length;
      el('dState').textContent=data.samples.length?'Ready to preview. These controls never move the camera.':'Add at least two positions in Compose.';
      el('dProposal').hidden=data.proposal.status==='none';el('dChanges').replaceChildren();
      el('dProposalTitle').textContent=candidate?`${seconds(data.proposal.assessed_duration)} proposal`:'No checked timing available';
      el('dProposalText').textContent=candidate?(data.proposal.status==='suggested'?`Your requested timing did not pass. This alternative passed the sampled check. ${data.proposal.reason||''}`:'This timing passed the sampled check. Preview it before applying.'):(data.proposal.reason||'');
      if(candidate){for(let i=1;i<candidate.waypoints.length;i++){const li=document.createElement('li');li.textContent=`P${i} → P${i+1}: ${seconds(original.waypoints[i].duration)} → ${seconds(candidate.waypoints[i].duration)}`;el('dChanges').append(li);}}
      el('dApply').disabled=el('dPreviewProposal').disabled=!candidate;renderShown();return true;
    }
    el('dPlay').onclick=play;el('dReset').onclick=()=>seek(0);el('dScrub').oninput=()=>seek(+el('dScrub').value);
    el('dContinue').onclick=()=>{if(waiting!==null)consumed.add(waiting);waiting=null;el('dCue').hidden=true;play();};
    el('dNext').onclick=()=>{const next=shown?.beats.find(b=>b.arrival>t+.01);seek(next?next.arrival:end());};
    el('dRefresh').onclick=()=>analyse();el('dAuto').onclick=()=>analyse();
    el('dFit').onclick=()=>{const n=+el('dTarget').value;if(!el('dTarget').value||!Number.isFinite(n)||n<=0||n>86400){el('dState').textContent='Enter a duration between 0 and 86400 seconds.';el('dTarget').focus();return;}analyse(n);};
    el('dPreviewProposal').onclick=async()=>{if(!candidate)return;pause();const ticket=++requestId;
      const result=await api.post('/api/director/preview',{move:candidate});if(ticket!==requestId)return;
      if(!result.ok){el('dState').textContent=result.error;return;}shown=result.director;el('dViewLabel').textContent='Timing proposal';renderShown();play();};
    async function applyCandidate(proposed,selection=null){
      if(!proposed||applying)return false;pause();applying=true;const before=structuredClone(api.getMove());el('dApply').disabled=true;
      try{if(await api.apply(structuredClone(proposed),selection)){undo=before;if(await analyse()){el('dUndo').hidden=false;el('dState').textContent='Treatment applied to the draft. Camera unchanged.';}return true;}return false;}
      finally{applying=false;el('dApply').disabled=!candidate;}}
    el('dApply').onclick=()=>applyCandidate(candidate);
    el('dUndo').onclick=async()=>{if(!undo||applying)return;const previous=undo;pause();applying=true;
      try{if(await api.apply(previous)){undo=null;await analyse();el('dUndo').hidden=true;}}finally{applying=false;}};
    el('dEdit').onclick=()=>api.edit(selected);
    const resize=new ResizeObserver(()=>draw());resize.observe(el('dPlot'));
    document.addEventListener('visibilitychange',()=>{if(document.hidden)pause();});
    async function previewTreatment(treatment){
      const ticket=++requestId;clearAnalysis('Checking this AI treatment locally…');
      const result=await api.post('/api/director/preview',{move:treatment.move});
      if(ticket!==requestId||!result.ok)return false;
      data=shown=result.director;original=structuredClone(treatment.move);
      el('dName').textContent=treatment.title;el('dViewLabel').textContent='AI treatment · not applied';
      renderShown();el('dState').textContent='Rehearsing an AI timing treatment. Authored draft and camera unchanged.';
      el('dPlot').scrollIntoView({block:'center',behavior:'auto'});play();return true;
    }
    return {activate:()=>analyse(),pause,invalidate,previewTreatment,applyTreatment:applyCandidate};
  };
})();
