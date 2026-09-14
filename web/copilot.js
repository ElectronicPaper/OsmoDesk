/* Optional cloud proposals. No camera routes, raw HTML output, or browser key. */
(() => {
  'use strict';
  window.createCopilot=(root,api)=>{
    const section=document.createElement('section');section.className='ai-studio';
    section.setAttribute('aria-label','AI Shot Copilot');
    section.innerHTML=`
      <div class="ai-heading"><div><div class="d-kicker">Shot Copilot / optional AI</div>
        <h3>Direct the feeling.</h3><p>Your framing. Two fresh rhythms. Describe the intent, compare the changes, and rehearse before committing.</p></div>
        <span class="ai-chip">YOU KEEP CONTROL</span></div>
      <div class="ai-compose"><div><label for="aiBrief">What should this shot feel like?</label>
        <textarea id="aiBrief" rows="3" maxlength="1600" placeholder="Hold the first frame in anticipation, reveal at P2, then settle at P3 with room for an edit."></textarea>
        <div class="ai-prompts" aria-label="Brief starting points"><button data-brief="Build anticipation before the reveal, then leave room to breathe at the final beat.">Quiet reveal</button>
          <button data-brief="A restrained product reveal: deliberate travel, clean arrivals, and useful holds for an editor.">Product study</button>
          <button data-brief="Give the middle beat a decisive arrival, with a calm opening and an editorial tail. Preserve all human cues.">Dramatic arrival</button></div></div>
        <div class="ai-controls"><details id="aiOptions"><summary>Model &amp; sharing</summary>
          <label for="aiModel">OpenAI model</label><select id="aiModel"><option value="gpt-5.6-luna">Luna · economical</option><option value="gpt-5.6-terra">Terra · balanced</option><option value="gpt-5.6-sol">Sol · deeper reasoning</option><option value="gpt-6-astra">Astra · advanced</option></select>
          <label for="aiEffort">Reasoning effort</label><select id="aiEffort"><option value="low">Low · quicker</option><option value="medium">Medium</option><option value="high">High · may take longer</option></select>
          <label class="ai-check"><input id="aiLabels" type="checkbox">Include my framing-point names</label>
          <p class="d-foot">Without names, points are shared as P1, P2… Higher effort can consume the output budget before a treatment finishes. Models are never substituted automatically.</p></details>
          <button id="aiPrepare" class="primary">Review what will be shared →</button>
          <p id="aiBudget" class="d-foot"></p></div></div>
      <p id="aiStatus" class="ai-status" role="status">AI is optional. Offline Director works without a key.</p>
      <div id="aiSetup" class="ai-disclosure" hidden><b>Optional AI, configured on your host</b><p>Open Settings → AI on the computer running OsmoDesk to add a key and set limits. Offline Director needs no key. Shared crew access requires the host’s explicit permission.</p><div class="d-actions"><button id="aiOpenSettings">Open AI settings</button><button id="aiRefresh">Check host configuration</button></div></div>
      <section id="aiDisclosure" class="ai-disclosure" hidden aria-labelledby="aiDisclosureTitle"><h4 id="aiDisclosureTitle">Before this leaves your computer</h4>
        <p id="aiManifest"></p><p>Shared: your brief, relative angular travel, timing, easing and cue/loop flags. Point names are optional. Fixed instructions and a response schema accompany this data.</p>
        <p>Not shared: camera credentials, network details, absolute framing angles, rig notes, takes, photos, live video or files. Do not put secrets or private client details in your brief.</p>
        <p>OpenAI processes this request. <code>store:false</code> is used, but that is not zero retention; abuse-monitoring logs may be retained for up to 30 days under standard terms. <a href="https://developers.openai.com/api/docs/guides/your-data" target="_blank" rel="noopener noreferrer">Data policy ↗</a></p>
        <details><summary>Inspect the exact shared shot context</summary><pre id="aiContext"></pre></details>
        <div class="d-actions"><button id="aiSend" class="primary">Send to OpenAI</button><button id="aiBack">Keep editing</button></div></section>
      <div id="aiPending" class="ai-pending" hidden><div><strong>Exploring two treatments…</strong><p>One bounded request. Your draft and camera stay unchanged.</p></div><div class="d-actions"><button id="aiCheck">Check request</button><button id="aiCancel">Discard request</button></div></div>
      <div id="aiResults" hidden><div class="ai-result-heading"><h4>Two ways to tell this shot</h4><button id="aiOriginal">Rehearse authored path</button></div><p id="aiUsage" class="d-foot"></p><div id="aiTreatments" class="ai-treatments"></div>
        <p class="d-foot">AI explanations are creative suggestions, not observations of your scene. Local checks are sampled gimbal arithmetic, not a physical guarantee. Applying changes only the draft. Restore is available in Shot pace.</p><button id="aiDiscard">Discard treatments</button></div>`;
    root.querySelector('.d-score').before(section);
    const el=id=>section.querySelector('#'+id);
    el('aiOpenSettings').onclick=()=>window.dispatchEvent(new CustomEvent('osmo-open-settings'));
    let status=null,prepared=null,signature=null,jobId=null,result=null,timer=0,ticket=0,working=false;
    let defaultsLoaded=false;
    const message=text=>{el('aiStatus').textContent=text;};
    const dollars=n=>`$${Number(n).toFixed(4)}`;
    function lock(value){working=value;for(const id of ['aiBrief','aiModel','aiEffort','aiLabels','aiPrepare'])el(id).disabled=value;
      el('aiPrepare').disabled=value||status?.available!==true;
      section.querySelectorAll('[data-brief]').forEach(b=>b.disabled=value);}
    async function get(path){try{const response=await fetch(path,{headers:{'X-Osmo-Client':OsmoSession.client},signal:AbortSignal.timeout(5000)});const body=await response.json();return response.ok?body:{ok:false,error:body.error||'Host request failed.'};}catch(_){return {ok:false,error:'Host unavailable. Your draft is unchanged.'};}}
    async function activate(quiet=false){
      const got=await get('/api/assistant/status');status=got;
      if(!defaultsLoaded&&got.defaults&&!working){for(const [key,id] of [['model','aiModel'],['effort','aiEffort']])if(got.defaults[key])el(id).value=got.defaults[key];defaultsLoaded=true;}
      const available=got.ok&&got.available;
      el('aiSetup').hidden=!!available;el('aiPrepare').disabled=working||!available;
      el('aiBudget').textContent=available?`${got.requests_used}/${got.request_limit} requests used · ${dollars(got.remaining_budget_usd)} estimated allowance left this host session.`:'';
      if(quiet!==true&&!working&&!result&&!prepared)message(available?'2–24 existing framing points. Only names, travel time, holds and easing can change.':got.error||'Cloud AI is not enabled on this host. Offline rehearsal remains available.');
    }
    function invalidate(){
      if(signature&&signature!==api.signature()){
        ticket++;clearTimeout(timer);prepared=null;result=null;signature=null;
        el('aiDisclosure').hidden=el('aiResults').hidden=true;
        if(jobId){const obsolete=jobId;jobId=null;api.post('/api/assistant/cancel',{job_id:obsolete});}
        el('aiPending').hidden=true;lock(false);message('Draft changed. Previous AI proposals cannot be applied. Review a fresh request when ready.');
      }
    }
    function fresh(){if(!signature||signature!==api.signature()||!api.ready()){invalidate();message('Wait for your current draft to save, then review a fresh request.');return false;}return true;}
    async function prepare(){
      if(working)return;if(!api.ready()){message('Finish editing and wait for the draft to save before sharing it.');return;}
      if(!el('aiBrief').value.trim()){message('Describe the shot’s intention first.');el('aiBrief').focus();return;}
      prepared=null;result=null;el('aiDisclosure').hidden=el('aiResults').hidden=true;
      const request=++ticket;signature=api.signature();lock(true);message('Preparing a local disclosure. Nothing is being sent to OpenAI yet.');
      const got=await api.post('/api/assistant/prepare',{generation:OsmoSession.generation,brief:el('aiBrief').value,include_labels:el('aiLabels').checked,model:el('aiModel').value,effort:el('aiEffort').value});
      if(request!==ticket)return;lock(false);if(!fresh())return;if(!got.ok){message(got.error);return;}
      prepared=got;el('aiContext').textContent=JSON.stringify(got.context,null,2);
      el('aiManifest').textContent=`OpenAI ${got.model} · ${got.effort} effort · up to ${got.max_output_tokens.toLocaleString()} output tokens. Conservative cost reservation: ${dollars(got.estimated_ceiling_usd)}. This is an estimate using published rates, not a billing guarantee. The confirmation expires in ${got.expires_in_seconds} seconds.`;
      el('aiDisclosure').hidden=false;message('Review the context and cost before choosing Send to OpenAI.');el('aiSend').focus();
    }
    async function send(){
      if(!prepared||working||!fresh())return;const manifest=prepared;prepared=null;lock(true);el('aiDisclosure').hidden=true;
      jobId=manifest.job_id;el('aiPending').hidden=false;
      const request=++ticket;message('Submitting one confirmed request…');
      const got=await api.post('/api/assistant/send',{confirmation:manifest.confirmation,generation:manifest.generation,consent:true});
      if(request!==ticket){if(got.job_id)api.post('/api/assistant/cancel',{job_id:got.job_id});return;}
      if(!got.ok){
        if(got.status){jobId=null;el('aiPending').hidden=true;lock(false);message(got.error+' Review the request again if you choose to retry.');activate(true);}
        else {message('Admission response lost. Checking the same request; no second paid request will be sent.');poll();}
        return;
      }
      jobId=got.job_id;el('aiPending').hidden=false;message('Request sent. You can discard the result; provider processing or billing may already have occurred.');
      poll();
    }
    async function poll(){
      clearTimeout(timer);if(!jobId)return;const currentJob=jobId,request=ticket;
      const got=await get('/api/assistant/job?id='+encodeURIComponent(currentJob));
      if(request!==ticket||currentJob!==jobId)return;
      if(!got.ok){message(got.error+' Use Check request or Discard; no new paid request was sent.');return;}
      if(!fresh())return;
      if(got.state==='running'||got.state==='cancelling'){timer=setTimeout(poll,1000);return;}
      lock(false);el('aiPending').hidden=true;
      if(got.state==='completed'){result=got.result;renderResults();message('Two treatments are ready. Preview one before applying.');}
      else message(got.error||'Request discarded. Your draft is unchanged.');
      activate(true);
    }
    function node(tag,text,cls){const element=document.createElement(tag);if(text!==undefined)element.textContent=text;if(cls)element.className=cls;return element;}
    function renderResults(){
      el('aiTreatments').replaceChildren();el('aiResults').hidden=false;
      const usage=result.usage;el('aiUsage').textContent=`Returned model: ${result.model}. ${usage?`${usage.input_tokens} input + ${usage.output_tokens} output tokens.`:'Token usage unavailable.'} Reservation ${dollars(result.estimated_ceiling_usd)}; allowance is not refunded after a request. Results expire after 20 minutes or a host restart.`;
      result.treatments.forEach((treatment,index)=>{
        const card=node('article',undefined,'ai-treatment');card.append(node('div',`TREATMENT ${String(index+1).padStart(2,'0')}`,'d-kicker'),node('h4',treatment.title),node('p',treatment.intent));
        const assessment=treatment.assessment,passed=assessment.preflight.ok;
        const duration=assessment.timing.programmed_duration??assessment.samples.at(-1)?.time??0;
        card.append(node('div',`${duration.toFixed(2)} s${assessment.timing.loop_uncertain?' / loop':''}${assessment.timing.cue_uncertain?' + your cues':''}`,'ai-duration'));
        card.append(node('p',passed?'✓ Local sampled path check passed':'! Resolve timing or travel before applying',passed?'ai-pass':'ai-warning'));
        const notes=node('ul');for(const caution of treatment.cautions)notes.append(node('li',caution));
        for(const finding of assessment.preflight.findings)notes.append(node('li','Local check: '+finding.detail));card.append(notes);
        const details=node('details'),summary=node('summary',`${treatment.diff.length} exact changes · framing preserved`),table=node('table');
        const caption=node('caption','Authored → treatment');table.append(caption);
        const head=node('tr');for(const name of ['Beat','Control','Authored','Proposed'])head.append(node('th',name));const thead=node('thead');thead.append(head);table.append(thead);
        const tbody=node('tbody');for(const change of treatment.diff){const row=node('tr');for(const value of [`P${change.index+1}`,change.field,change.before,change.after])row.append(node('td',String(value)));tbody.append(row);}table.append(tbody);
        details.append(summary,table);card.append(details);
        const actions=node('div',undefined,'d-actions'),preview=node('button','▶ Rehearse treatment'),apply=node('button','Apply to draft','primary');apply.disabled=true;
        preview.onclick=async()=>{if(!fresh())return;preview.disabled=true;const accepted=await api.preview(treatment);preview.disabled=false;if(accepted&&fresh()){apply.disabled=!passed;message(passed?'Rehearsal started. Apply only when you are ready; the camera has not moved.':'You can rehearse this proposal, but its local check failed. It cannot be applied.');}};
        apply.onclick=async()=>{if(!fresh()||working)return;const selectedJob=jobId;lock(true);apply.disabled=true;
          try{if(await api.apply(treatment,{job_id:selectedJob,index})){result=null;el('aiResults').hidden=true;message('Treatment applied to the draft. Use Restore previous timing in Shot pace to undo. Camera unchanged.');}else message('Treatment was not applied. Your latest draft was kept.');}
          finally{lock(false);activate(true);}};
        actions.append(preview,apply);card.append(actions);el('aiTreatments').append(card);
      });
    }
    async function discard(){
      ++ticket;clearTimeout(timer);prepared=null;result=null;signature=null;const id=jobId;jobId=null;
      el('aiDisclosure').hidden=el('aiPending').hidden=el('aiResults').hidden=true;lock(false);
      if(id)await api.post('/api/assistant/cancel',{job_id:id});
      message('Result discarded. Provider processing or billing may already have occurred. Draft unchanged.');activate(true);
    }
    el('aiPrepare').onclick=prepare;el('aiSend').onclick=send;el('aiBack').onclick=()=>{prepared=null;el('aiDisclosure').hidden=true;el('aiBrief').focus();};
    el('aiRefresh').onclick=activate;el('aiCheck').onclick=poll;el('aiCancel').onclick=el('aiDiscard').onclick=discard;el('aiOriginal').onclick=()=>api.original();
    for(const id of ['aiBrief','aiModel','aiEffort','aiLabels'])el(id).addEventListener('input',()=>{prepared=null;el('aiDisclosure').hidden=true;});
    section.querySelectorAll('[data-brief]').forEach(button=>button.onclick=()=>{el('aiBrief').value=button.dataset.brief;prepared=null;el('aiDisclosure').hidden=true;el('aiBrief').focus();});
    activate();return {activate,invalidate};
  };
})();
