(function(){
  'use strict';

  const PAN_COLOR = '#2dd4bf';
  const TILT_COLOR = '#f0b90b';

  const PRESETS = {
    linear: [0.02, 0.02, 0.98, 0.98],
    soft: [0.33, 0, 0.67, 1],
    late: [0.6, 0, 0.85, 1],
    early: [0.15, 0, 0.4, 1]
  };

  function clamp(v, min, max){ return Math.min(max, Math.max(min, v)); }

  function clampCurve(arr){
    let x1 = arr[0], y1 = arr[1], x2 = arr[2], y2 = arr[3];
    x1 = clamp(x1, 0.02, 0.98);
    x2 = clamp(x2, 0.02, 0.98);
    if (x1 > x2){ const m = (x1 + x2) / 2; x1 = m; x2 = m; }
    y1 = clamp(y1, 0, 1);
    y2 = clamp(y2, 0, 1);
    if (y1 > y2){ const m = (y1 + y2) / 2; y1 = m; y2 = m; }
    return [x1, y1, x2, y2];
  }

  function cubicAt(t, a, b, c, d){
    const mt = 1 - t;
    return mt*mt*mt*a + 3*mt*mt*t*b + 3*mt*t*t*c + t*t*t*d;
  }

  function solveBezierY(x, x1, y1, x2, y2){
    if(x<=0)return 0;if(x>=1)return 1;
    let t0 = 0, t1 = 1, t = x;
    for (let i = 0; i < 24; i++){
      t = (t0 + t1) / 2;
      const cx = cubicAt(t, 0, x1, x2, 1);
      if (cx < x) t0 = t; else t1 = t;
    }
    return cubicAt(t, 0, y1, y2, 1);
  }

  function ce(tag, cls, text){
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function legacyShape(name,x){
    if(name==='linear')return x;
    if(name==='ease-in')return x*x;
    if(name==='ease-out')return 1-(1-x)**2;
    if(name==='ease-in-out')return x<.5?2*x*x:1-2*(1-x)**2;
    if(name==='ease-in-out-cubic')return x<.5?4*x**3:1-4*(1-x)**3;
    return (1-Math.cos(Math.PI*x))/2;
  }

  function getFlowFlags(move){
    const flags = [];
    if (move && move.flow) flags.push('move.flow');
    const wps = (move && move.waypoints) || [];
    wps.forEach((wp, i)=>{ if (wp && wp.flow) flags.push('waypoint ' + i); });
    return flags;
  }

  function clearFlow(move){
    if (move && ('flow' in move)) move.flow = false;
    const wps = (move && move.waypoints) || [];
    wps.forEach((wp)=>{ if (wp && ('flow' in wp)) wp.flow = false; });
  }

  function create(root, api){
    let candidate = null;
    let sourceSignature = null;
    let selectedDest = 1;
    let selectedAxis = 'pan';
    let selectedMetric = 'position';
    let stale = false;
    let localFlowCleared = false;
    let previewState = { status: 'idle', data: null, error: null, forSig: null, forDest: null };
    let previewToken = 0;
    let previewTimer = null;
    let applyBusy = false;
    let lastNotice = null;

    const els = {};

    function loadCandidate(){
      clearTimeout(previewTimer);previewToken++;
      const move = api.getMove();
      candidate = JSON.parse(JSON.stringify(move || { waypoints: [] }));
      sourceSignature = JSON.stringify(move || { waypoints: [] });
      stale = false;
      localFlowCleared = false;
      lastNotice = null;
      previewState = { status: 'idle', data: null, error: null, forSig: null, forDest: null };
    }

    function currentSignature(){ return JSON.stringify(candidate); }

    function legCount(){ return Math.max(0, ((candidate && candidate.waypoints) || []).length - 1); }

    function destWaypoint(){
      const wps = (candidate && candidate.waypoints) || [];
      return wps[selectedDest] || null;
    }

    function curveKey(axis){ return axis === 'pan' ? 'yaw_curve' : 'pitch_curve'; }

    function skeleton(){
      root.innerHTML = '';
      const wrap = ce('div', 'axis-curves');
      const header = ce('div', 'ac-header');
      header.appendChild(ce('h2', 'ac-title', 'Axis curves'));
      header.appendChild(ce('p', 'ac-sub', 'Shape pan and tilt easing between waypoints, then preview planned motion before applying to the draft.'));
      wrap.appendChild(header);
      els.body = ce('div', 'ac-body');
      wrap.appendChild(els.body);
      root.appendChild(wrap);
      els.root = wrap;
    }

    function renderEmpty(){
      els.body.innerHTML = '';
      els.body.inert = applyBusy;
      const empty = ce('div', 'ac-empty');
      empty.appendChild(ce('p', null, 'Add at least two waypoints in Compose to edit axis curves.'));
      els.body.appendChild(empty);
    }

    function renderStaleBanner(container){
      if (!stale) return;
      const banner = ce('div', 'ac-notice ac-notice-warn');
      banner.appendChild(ce('span', null, 'The shared draft changed elsewhere. Local edits are kept, but Apply is disabled.'));
      const btn = ce('button', 'ac-btn ac-btn-ghost', 'Refresh from draft');
      btn.type = 'button';
      btn.addEventListener('click', ()=>{ loadCandidate(); render(); schedulePreview(); });
      banner.appendChild(btn);
      container.appendChild(banner);
    }

    function render(){
      if (!els.body) skeleton();
      if (!candidate) loadCandidate();
      if (legCount() < 1){ renderEmpty(); return; }
      if (selectedDest < 1 || selectedDest > legCount()) selectedDest = 1;

      els.body.innerHTML = '';
      renderStaleBanner(els.body);

      const controls = ce('div', 'ac-row');

      const legField = ce('div', 'ac-field');
      legField.appendChild(ce('label', null, 'Segment'));
      const legSelect = document.createElement('select');
      legSelect.className = 'ac-select';
      legSelect.setAttribute('aria-label','Transition');
      const wps = candidate.waypoints;
      for (let i = 1; i <= legCount(); i++){
        const opt = document.createElement('option');
        opt.value = String(i);
        const fromName = (wps[i-1] && wps[i-1].name) || ('WP ' + (i-1));
        const toName = (wps[i] && wps[i].name) || ('WP ' + i);
        opt.textContent = 'P'+i+' → P'+(i+1)+' · '+fromName+' → '+toName;
        if (i === selectedDest) opt.selected = true;
        legSelect.appendChild(opt);
      }
      legSelect.addEventListener('change', ()=>{ selectedDest = parseInt(legSelect.value, 10) || 1; render(); schedulePreview(); });
      legField.appendChild(legSelect);
      controls.appendChild(legField);

      const wp = destWaypoint();

      const linkField = ce('div', 'ac-field');
      linkField.appendChild(ce('label', null, 'Axis link'));
      const linkSelect = document.createElement('select');
      linkSelect.className = 'ac-select';
      linkSelect.setAttribute('aria-label','Axis relationship');
      linkSelect.title='Independent: each axis follows time. Linked: the follower curve maps leader progress to its own progress.';
      [['independent','Independent'],['pan_leads','Pan leads'],['tilt_leads','Tilt leads']].forEach((pair)=>{
        const opt = document.createElement('option');
        opt.value = pair[0]; opt.textContent = pair[1];
        if ((wp.axis_link || 'independent') === pair[0]) opt.selected = true;
        linkSelect.appendChild(opt);
      });
      const flowFlagsPre = getFlowFlags(candidate);
      linkSelect.disabled = !!(flowFlagsPre.length && !localFlowCleared);
      linkSelect.addEventListener('change', ()=>{ wp.axis_link = linkSelect.value; render(); schedulePreview(); });
      linkField.appendChild(linkSelect);
      controls.appendChild(linkField);

      els.body.appendChild(controls);
      const durationField=ce('label','ac-field','Travel time (seconds)');
      const durationInput=ce('input','ac-num');durationInput.type='number';durationInput.min='.05';durationInput.max='86400';durationInput.step='.1';durationInput.value=wp.duration;durationInput.setAttribute('aria-label','Transition duration');
      durationInput.onchange=()=>{const v=Number(durationInput.value);if(!Number.isFinite(v)||v<.05||v>86400){durationInput.value=wp.duration;return;}wp.duration=v;schedulePreview();};durationField.append(durationInput);controls.append(durationField);

      const flowFlags = getFlowFlags(candidate);
      if (flowFlags.length){
        const notice = ce('div', 'ac-notice ac-notice-warn');
        notice.appendChild(ce('span', null, 'This draft uses Flow, which is incompatible with manual curves and axis link. Disable Flow in Compose, or switch to manual curves here (local edit, not saved until Apply).'));
        if (!localFlowCleared){
          const btn = ce('button', 'ac-btn ac-btn-ghost', 'Switch to manual curves');
          btn.type = 'button';
          btn.addEventListener('click', ()=>{ clearFlow(candidate); localFlowCleared = true; render(); schedulePreview(); });
          notice.appendChild(btn);
        } else {
          notice.appendChild(ce('span', 'ac-badge', 'Flow flags cleared locally'));
        }
        els.body.appendChild(notice);
      }

      const editingDisabled = !!(flowFlags.length && !localFlowCleared);
      if(localFlowCleared)els.body.appendChild(ce('p','ac-notice','Flow will be disabled for this draft when you Apply. Other positions and holds are preserved.'));

      const workbench=ce('div','ac-workbench'),editorPane=ce('div','ac-editor-pane'),previewPane=ce('div','ac-preview-pane');
      workbench.append(editorPane,previewPane);els.body.append(workbench);

      const tabs = ce('div', 'ac-tabs');
      [['pan','Pan',PAN_COLOR],['tilt','Tilt',TILT_COLOR]].forEach((t)=>{
        const tab = ce('button', 'ac-tab' + (selectedAxis === t[0] ? ' active' : ''), t[1]);
        tab.type = 'button';
        tab.setAttribute('aria-pressed',String(selectedAxis===t[0]));
        tab.style.setProperty('--ac-tab-color', t[2]);
        tab.addEventListener('click', ()=>{ selectedAxis = t[0]; render(); });
        tabs.appendChild(tab);
      });
      editorPane.appendChild(tabs);

      const graphSection = ce('div', 'ac-graph-section');
      editorPane.appendChild(graphSection);
      renderGraph(graphSection, editingDisabled);

      const metricRow = ce('div', 'ac-row');
      const metricField = ce('div', 'ac-field');
      metricField.appendChild(ce('label', null, 'Planned motion metric'));
      const metricSelect = document.createElement('select');
      metricSelect.className = 'ac-select';
      metricSelect.setAttribute('aria-label','Planned motion metric');
      [['position','Position (deg)'],['speed','Speed (deg/s)'],['accel','Acceleration (deg/s2)']].forEach((pair)=>{
        const opt = document.createElement('option');
        opt.value = pair[0]; opt.textContent = pair[1];
        if (selectedMetric === pair[0]) opt.selected = true;
        metricSelect.appendChild(opt);
      });
      metricSelect.addEventListener('change', ()=>{ selectedMetric = metricSelect.value; renderPreview(); });
      metricField.appendChild(metricSelect);
      metricRow.appendChild(metricField);
      previewPane.appendChild(metricRow);

      els.previewSection = ce('div', 'ac-preview-section');
      previewPane.appendChild(els.previewSection);
      renderPreview();

      const actions = ce('div', 'ac-actions');
      const applyBtn = ce('button', 'ac-btn ac-btn-primary', applyBusy ? 'Applying...' : 'Apply to draft');
      applyBtn.type = 'button';
      applyBtn.disabled = !canApply() || applyBusy;
      applyBtn.addEventListener('click', doApply);
      els.applyBtn = applyBtn;
      actions.appendChild(applyBtn);

      const discardBtn = ce('button', 'ac-btn ac-btn-ghost', 'Discard local edits');
      discardBtn.type = 'button';
      discardBtn.addEventListener('click', ()=>{ loadCandidate(); render(); schedulePreview(); });
      actions.appendChild(discardBtn);

      if (lastNotice){ actions.appendChild(ce('span', 'ac-inline-notice', lastNotice)); }

      els.body.appendChild(actions);
    }

    function canApply(){
      if (stale) return false;
      if(JSON.stringify(api.getMove())!==sourceSignature)return false;
      if(currentSignature()===sourceSignature)return false;
      if (getFlowFlags(candidate).length && !localFlowCleared) return false;
      if (previewState.status !== 'ok') return false;
      if (previewState.forSig !== currentSignature()) return false;
      if (previewState.forDest !== selectedDest) return false;
      return true;
    }

    function doApply(){
      if (!canApply() || applyBusy) return;
      applyBusy = true;
      render();
      api.apply(structuredClone(candidate)).then((ok)=>{
        applyBusy = false;
        if (ok){
          loadCandidate();
          lastNotice = 'Applied to draft. Camera unchanged.';
        } else {
          lastNotice = 'Apply was rejected by the draft guard.';
        }
        render();
        if(ok)schedulePreview();
      }).catch(()=>{
        applyBusy = false;
        lastNotice = 'Apply failed.';
        render();
      });
    }

    function sameCurve(a, b){
      if (!a || !b) return false;
      for (let i = 0; i < 4; i++){ if (Math.abs(a[i] - b[i]) > 0.005) return false; }
      return true;
    }

    function drawAxisCurve(svg, svgNS, curve, color, isActive, inherited){
      const path = document.createElementNS(svgNS, 'path');
      let d;
      if (curve){
        d = 'M 0 100 C ' + (curve[0]*100) + ' ' + (100 - curve[1]*100) + ', ' + (curve[2]*100) + ' ' + (100 - curve[3]*100) + ', 100 0';
      } else {
        d = Array.from({length:101},(_,i)=>(i?'L':'M')+' '+i+' '+(100-100*inherited(i/100))).join(' ');
      }
      path.setAttribute('d', d);
      path.setAttribute('class', 'ac-curve' + (isActive ? ' active' : ''));
      path.setAttribute('stroke', color);
      if (!curve) path.setAttribute('stroke-dasharray', '4 3');
      svg.appendChild(path);

      if (isActive){
        [25,50,75].forEach((pct)=>{
          const x = pct / 100;
          const y = curve ? solveBezierY(x, curve[0], curve[1], curve[2], curve[3]) : inherited(x);
          const dot = document.createElementNS(svgNS, 'circle');
          dot.setAttribute('cx', x*100);
          dot.setAttribute('cy', 100 - y*100);
          dot.setAttribute('r', '1');
          dot.setAttribute('class', 'ac-tick');
          svg.appendChild(dot);
        });
      }
    }

    function addHandles(svg, svgNS, wp, axis){
      const key = curveKey(axis);
      const color = axis === 'pan' ? PAN_COLOR : TILT_COLOR;

      function pt(idx){ return [wp[key][idx*2]*100, 100 - wp[key][idx*2+1]*100]; }

      const anchor1 = document.createElementNS(svgNS, 'line');
      anchor1.setAttribute('x1', 0); anchor1.setAttribute('y1', 100);
      let p1 = pt(0);
      anchor1.setAttribute('x2', p1[0]); anchor1.setAttribute('y2', p1[1]);
      anchor1.setAttribute('class', 'ac-handle-line');
      svg.appendChild(anchor1);

      const anchor2 = document.createElementNS(svgNS, 'line');
      anchor2.setAttribute('x1', 100); anchor2.setAttribute('y1', 0);
      let p2 = pt(1);
      anchor2.setAttribute('x2', p2[0]); anchor2.setAttribute('y2', p2[1]);
      anchor2.setAttribute('class', 'ac-handle-line');
      svg.appendChild(anchor2);

      [0,1].forEach((idx)=>{
        const p = pt(idx);
        const handle = document.createElementNS(svgNS, 'circle');
        handle.setAttribute('cx', p[0]);
        handle.setAttribute('cy', p[1]);
        handle.setAttribute('r', '6');
        handle.setAttribute('class', 'ac-handle');
        handle.setAttribute('fill', 'transparent');
        const dot=document.createElementNS(svgNS,'circle');dot.setAttribute('cx',p[0]);dot.setAttribute('cy',p[1]);dot.setAttribute('r','1.5');dot.setAttribute('fill',color);dot.style.pointerEvents='none';
        handle.setAttribute('tabindex', '0');
        handle.setAttribute('role', 'slider');
        handle.setAttribute('aria-label', (idx === 0 ? 'Control point 1' : 'Control point 2') + ' for ' + axis);
        handle.setAttribute('aria-valuemin','0');handle.setAttribute('aria-valuemax','100');

        let dragging = false;

        function toNorm(clientX, clientY){
          const point=new DOMPoint(clientX,clientY).matrixTransform(svg.getScreenCTM().inverse());
          const nx = clamp(point.x/100, 0, 1);
          const ny = clamp(1-point.y/100, 0, 1);
          return [nx, ny];
        }

        function applyPoint(nx, ny){
          const arr = wp[key].slice();
          arr[idx*2] = clamp(nx,idx===0?.02:arr[0],idx===0?arr[2]:.98);
          arr[idx*2+1] = clamp(ny,idx===0?0:arr[1],idx===0?arr[3]:1);
          wp[key] = arr;
          handle.setAttribute('cx', wp[key][idx*2]*100);
          handle.setAttribute('cy', 100 - wp[key][idx*2+1]*100);
          dot.setAttribute('cx',wp[key][idx*2]*100);dot.setAttribute('cy',100-wp[key][idx*2+1]*100);
          handle.setAttribute('aria-valuenow',String(Math.round(wp[key][idx*2]*100)));
          handle.setAttribute('aria-valuetext',`Input ${Math.round(wp[key][idx*2]*100)}%, output ${Math.round(wp[key][idx*2+1]*100)}%`);
          root.querySelectorAll('.ac-fields input').forEach((input,i)=>input.value=String(Math.round(wp[key][i]*1000)/10));
          const otherLine = idx === 0 ? anchor1 : anchor2;
          otherLine.setAttribute('x2', wp[key][idx*2]*100);
          otherLine.setAttribute('y2', 100 - wp[key][idx*2+1]*100);
          const activePath = svg.querySelector('.ac-curve.active');
          if (activePath){
            const c = wp[key];
            activePath.setAttribute('d', 'M 0 100 C ' + (c[0]*100) + ' ' + (100 - c[1]*100) + ', ' + (c[2]*100) + ' ' + (100 - c[3]*100) + ', 100 0');
          }
        }

        handle.addEventListener('pointerdown', (e)=>{
          dragging = true;
          handle.setPointerCapture(e.pointerId);
          e.preventDefault();
        });
        handle.addEventListener('pointermove', (e)=>{
          if (!dragging) return;
          const norm = toNorm(e.clientX, e.clientY);
          applyPoint(norm[0], norm[1]);
          schedulePreview();
        });
        function endDrag(e){
          if (!dragging) return;
          dragging = false;
          try { handle.releasePointerCapture(e.pointerId); } catch (err){}
          render();
          schedulePreview();
        }
        handle.addEventListener('pointerup', endDrag);
        handle.addEventListener('pointercancel', endDrag);
        handle.addEventListener('lostpointercapture',()=>{dragging=false;});

        handle.addEventListener('keydown', (e)=>{
          const step = e.shiftKey ? 0.002 : 0.02;
          let nx = wp[key][idx*2], ny = wp[key][idx*2+1];
          let handled = true;
          if (e.key === 'ArrowLeft') nx -= step;
          else if (e.key === 'ArrowRight') nx += step;
          else if (e.key === 'ArrowUp') ny += step;
          else if (e.key === 'ArrowDown') ny -= step;
          else handled = false;
          if (handled){
            e.preventDefault();
            applyPoint(clamp(nx, 0, 1), clamp(ny, 0, 1));
            schedulePreview();
          }
        });

        svg.appendChild(handle);
        svg.appendChild(dot);
        requestAnimationFrame(()=>{if(!handle.isConnected)return;handle.setAttribute('r',String(Math.max(4,2728/Math.max(1,Math.min(svg.clientWidth,svg.clientHeight)))));});
      });
    }

    function renderGraph(container, disabled){
      const wp = destWaypoint();
      const link = wp.axis_link || 'independent';
      const isFollower = (link === 'pan_leads' && selectedAxis === 'tilt') || (link === 'tilt_leads' && selectedAxis === 'pan');
      const xLabel = isFollower ? 'X axis: leader progress (coupled, not time)' : 'X axis: time through leg';
      container.appendChild(ce('p', 'ac-axis-label', xLabel));
      container.appendChild(ce('p','ac-hint',isFollower?'Coupling curve · maps the leader’s progress to this axis. Inherit = 1:1.':'Timing curve · maps elapsed time to axis progress. Inherit uses the segment easing.'));

      const wrap = ce('div', 'ac-graph-wrap');
      const svgNS = 'http://www.w3.org/2000/svg';
      const svg = document.createElementNS(svgNS, 'svg');
      svg.setAttribute('viewBox', '-12 -12 124 124');
      svg.setAttribute('class', 'ac-graph');
      svg.setAttribute('preserveAspectRatio', 'none');
      svg.setAttribute('aria-label',selectedAxis+' progress curve. Drag handles or use arrow keys; Shift for fine adjustment.');

      for (let g = 0; g <= 100; g += 25){
        const lv = document.createElementNS(svgNS, 'line');
        lv.setAttribute('x1', g); lv.setAttribute('x2', g);
        lv.setAttribute('y1', 0); lv.setAttribute('y2', 100);
        lv.setAttribute('class', 'ac-grid');
        svg.appendChild(lv);
        const lh = document.createElementNS(svgNS, 'line');
        lh.setAttribute('y1', g); lh.setAttribute('y2', g);
        lh.setAttribute('x1', 0); lh.setAttribute('x2', 100);
        lh.setAttribute('class', 'ac-grid');
        svg.appendChild(lh);
      }

      for(const axis of ['pan','tilt']){
        if(disabled)continue;
        if(link!=='independent'&&axis!==selectedAxis)continue;
        const follower=(link==='pan_leads'&&axis==='tilt')||(link==='tilt_leads'&&axis==='pan');
        drawAxisCurve(svg,svgNS,wp[curveKey(axis)],axis==='pan'?PAN_COLOR:TILT_COLOR,axis===selectedAxis,x=>follower?x:legacyShape(wp.easing,x));
      }

      const activeCurve = wp[curveKey(selectedAxis)];
      if (activeCurve && !disabled){ addHandles(svg, svgNS, wp, selectedAxis); }

      wrap.appendChild(svg);
      container.appendChild(wrap);
      if(disabled)container.append(ce('p','ac-hint','Automatic Flow spline: view Planned motion. Switch to manual curves to edit this graph.'));
      const ticks=ce('div','ac-span-row');for(const t of ['0%','25%','50%','75%','100%'])ticks.append(ce('span',null,t));container.append(ticks);

      const fields = ce('div', 'ac-fields');
      const curveArr = activeCurve || [0.02, 0.02, 0.98, 0.98];
      ['x1','y1','x2','y2'].forEach((name, idx)=>{
        const field = ce('div', 'ac-field ac-field-num');
        field.appendChild(ce('label', null, name + ' %'));
        const input = document.createElement('input');
        input.type = 'number';
        input.min = idx % 2 === 0 ? '2' : '0';
        input.max = idx % 2 === 0 ? '98' : '100';
        input.step = '1';
        input.className = 'ac-num';
        input.setAttribute('aria-label',selectedAxis+' '+name+' percent');
        input.title=(idx<2?'Start':'End')+' handle: '+(idx%2?'axis progress':'time or leader progress')+' in percent';
        input.value = String(Math.round(curveArr[idx]*100));
        input.disabled = disabled || !activeCurve;
        input.addEventListener('change', ()=>{
          const arr = (wp[curveKey(selectedAxis)] || curveArr.slice()).slice();
          arr[idx] = clamp((parseFloat(input.value) || 0) / 100, 0, 1);
          wp[curveKey(selectedAxis)] = clampCurve(arr);
          render(); schedulePreview();
        });
        field.appendChild(input);
        fields.appendChild(field);
      });
      container.appendChild(fields);

      const presets = ce('div', 'ac-presets');
      const presetDefs = [['inherit','Inherit'],['linear','Linear'],['soft','Soft'],['late','Late'],['early','Early']];
      presetDefs.forEach((pd)=>{
        const key = pd[0], label = pd[1];
        const btn = ce('button', 'ac-preset', label);
        btn.type = 'button';
        btn.title=key==='inherit'?'Remove the custom curve; use inherited easing or 1:1 coupling':key==='soft'?'Zero speed at both ends; smooth stopped keyframe':key==='linear'?'Constant progress; nonzero speed at the endpoints':key==='late'?'Build motion later in the transition':'Complete most motion early in the transition';
        btn.disabled = disabled;
        const isActive = (key === 'inherit' && !activeCurve) || (activeCurve && PRESETS[key] && sameCurve(activeCurve, PRESETS[key]));
        if (isActive) btn.classList.add('active');
        btn.addEventListener('click', ()=>{
          if (key === 'inherit'){ wp[curveKey(selectedAxis)] = null; }
          else { wp[curveKey(selectedAxis)] = PRESETS[key].slice(); }
          render(); schedulePreview();
        });
        presets.appendChild(btn);
      });
      container.appendChild(presets);
    }

    function schedulePreview(){
      previewToken++;lastNotice=null;
      if (legCount() < 1) return;
      previewState = Object.assign({}, previewState, { status: 'pending' });
      if (els.previewSection) renderPreview();
      updateApplyButton();
      clearTimeout(previewTimer);
      previewTimer = setTimeout(runPreview, 400);
    }

    function runPreview(){
      const token = ++previewToken;
      const sig = currentSignature();
      const dest = selectedDest;
      api.post('/api/move/curves/preview', { move: structuredClone(candidate), leg: selectedDest }).then((resp)=>{
        if (token !== previewToken || sig!==currentSignature() || dest!==selectedDest || stale) return;
        if (resp && resp.ok){
          previewState = { status: 'ok', data: resp.curves, error: null, forSig: sig, forDest: dest };
        } else {
          previewState = { status: 'error', data: null, error: (resp && resp.error) || 'Preview failed', forSig: sig, forDest: dest };
        }
        renderPreview();
        updateApplyButton();
      }).catch(()=>{
        if (token !== previewToken) return;
        previewState = { status: 'error', data: null, error: 'Preview request failed', forSig: sig, forDest: dest };
        renderPreview();
        updateApplyButton();
      });
    }

    function updateApplyButton(){
      if (els.applyBtn){ els.applyBtn.disabled = !canApply() || applyBusy; }
    }

    function renderPreview(){
      if (!els.previewSection) return;
      const section = els.previewSection;
      section.innerHTML = '';
      section.appendChild(ce('p', 'ac-axis-label', 'Planned motion (from candidate curves, not measured hardware telemetry)'));

      if (previewState.status === 'pending'){
        section.appendChild(ce('p', 'ac-hint', 'Calculating preview...'));
        return;
      }
      if (previewState.status === 'error'){
        section.appendChild(ce('p', 'ac-notice ac-notice-error', previewState.error || 'Preview failed.'));
        return;
      }
      if (previewState.status !== 'ok' || !previewState.data){
        section.appendChild(ce('p', 'ac-hint', 'Adjust a curve to generate a preview.'));
        return;
      }

      const curves = previewState.data;
      const samples = Array.isArray(curves.samples) ? curves.samples : [];
      const duration = curves.duration > 0 ? curves.duration : 0;

      if (!samples.length || !duration){
        section.appendChild(ce('p', 'ac-hint', 'No preview samples available.'));
        return;
      }

      const fieldMap = {
        position: ['yaw','pitch'],
        speed: ['yaw_speed','pitch_speed'],
        accel: ['yaw_accel','pitch_accel']
      };
      const metricFields = fieldMap[selectedMetric];

      const svgNS = 'http://www.w3.org/2000/svg';
      const svg = document.createElementNS(svgNS, 'svg');
      svg.setAttribute('viewBox', '0 0 200 100');
      svg.setAttribute('class', 'ac-preview-svg');
      svg.setAttribute('preserveAspectRatio', 'none');

      let minV = Infinity, maxV = -Infinity;
      samples.forEach((s)=>{
        metricFields.forEach((f)=>{
          const v = s[f];
          if (typeof v === 'number' && isFinite(v)){ if (v < minV) minV = v; if (v > maxV) maxV = v; }
        });
      });
      if (!isFinite(minV) || !isFinite(maxV)){
        section.appendChild(ce('p', 'ac-hint', 'No finite samples to plot.'));
        return;
      }
      if (minV === maxV){ minV -= 1; maxV += 1; }

      for (let g = 0; g <= 100; g += 25){
        const gl = document.createElementNS(svgNS, 'line');
        gl.setAttribute('x1', g*2); gl.setAttribute('x2', g*2);
        gl.setAttribute('y1', 0); gl.setAttribute('y2', 100);
        gl.setAttribute('class', 'ac-grid');
        svg.appendChild(gl);
      }

      function buildPath(field, color){
        let d = '';
        let started = false;
        samples.forEach((s)=>{
          const t = s.t, v = s[field];
          if (typeof t !== 'number' || !isFinite(t) || typeof v !== 'number' || !isFinite(v)) return;
          const x = clamp(t/duration, 0, 1) * 200;
          const y = 100 - ((v - minV) / (maxV - minV)) * 100;
          d += (started ? ' L ' : 'M ') + x + ' ' + y;
          started = true;
        });
        if (!d) return;
        const path = document.createElementNS(svgNS, 'path');
        path.setAttribute('d', d);
        path.setAttribute('class', 'ac-preview-line');
        path.setAttribute('stroke', color);
        svg.appendChild(path);
      }

      buildPath(metricFields[0], PAN_COLOR);
      buildPath(metricFields[1], TILT_COLOR);

      section.appendChild(svg);
      const units={position:'°',speed:'°/s',accel:'°/s²'}[selectedMetric];
      section.appendChild(ce('p','ac-axis-label',`${minV.toFixed(2)} to ${maxV.toFixed(2)} ${units} · 0 to ${duration.toFixed(2)} seconds · shared vertical scale`));

      const legend = ce('div', 'ac-legend');
      const l1 = ce('span', 'ac-legend-item'); l1.style.setProperty('--ac-legend-color', PAN_COLOR); l1.textContent = 'Pan';
      const l2 = ce('span', 'ac-legend-item'); l2.style.setProperty('--ac-legend-color', TILT_COLOR); l2.textContent = 'Tilt';
      legend.appendChild(l1); legend.appendChild(l2);
      section.appendChild(legend);

      const spanRow = ce('div', 'ac-span-row');
      spanRow.appendChild(ce('span', null, '0%'));
      spanRow.appendChild(ce('span', null, '25%'));
      spanRow.appendChild(ce('span', null, '50%'));
      spanRow.appendChild(ce('span', null, '75%'));
      spanRow.appendChild(ce('span', null, '100%'));
      section.appendChild(spanRow);

      if (curves.preflight && curves.preflight.ok === false && Array.isArray(curves.preflight.findings) && curves.preflight.findings.length){
        const box = ce('div', 'ac-notice ac-notice-warn');
        box.appendChild(ce('p', null, 'Preflight blockers (draft can still be saved for later retiming):'));
        const list = document.createElement('ul');
        curves.preflight.findings.forEach((f)=>{
          const li = document.createElement('li');
          li.textContent = (f && f.detail) || String(f);
          list.appendChild(li);
        });
        box.appendChild(list);
        section.appendChild(box);
      }

      if (Array.isArray(curves.warnings) && curves.warnings.length){
        const help=ce('details','ac-help');help.append(ce('summary',null,'Curve behavior & limits'));
        curves.warnings.forEach(w=>{
          if(w.startsWith('This transition has nonzero'))section.append(ce('p','ac-notice ac-notice-warn',w));
          else help.append(ce('p','ac-hint',w));
        });
        section.append(help);
      }
    }

    loadCandidate();
    render();
    schedulePreview();

    return {
      selectLeg(index){
        if(!applyBusy && JSON.stringify(candidate)===sourceSignature)loadCandidate();
        if (!candidate) loadCandidate();
        const max = legCount();
        selectedDest = clamp(parseInt(index, 10) || 1, 1, Math.max(1, max));
        render();
        schedulePreview();
      },
      invalidate(){
        if (!candidate || applyBusy) return;
        let live;
        try { live = JSON.stringify(api.getMove()); } catch (e){ return; }
        if (live !== sourceSignature && !stale){
          if(currentSignature()===sourceSignature){loadCandidate();render();schedulePreview();return;}
          stale = true;
          previewToken++;clearTimeout(previewTimer);previewState={status:'idle'};
          render();
        }
      },
      refresh(){
        loadCandidate();
        render();
        schedulePreview();
      }
    };
  }

  window.OsmoAxisCurves = { create: create };
})();
