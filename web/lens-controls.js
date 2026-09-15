/* Shared operator controls. Requests are not camera readback or focus-distance proof. */
(() => {
  'use strict';
  const FRESH_SECONDS = 3;
  const REQUEST_TIMEOUT_MS = 7000;
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const validPoint = point => point && finite(point.x) && finite(point.y) &&
    point.x >= 0 && point.x <= 1 && point.y >= 0 && point.y <= 1;

  window.OsmoLens = {
    mount(hostElement, {post} = {}) {
      if (!hostElement || typeof post !== 'function') throw new TypeError('Lens controls require a host and post function');
      const card = document.createElement('section');
      card.className = 'ol-card';
      card.setAttribute('aria-label', 'Lens and focus');
      // Static markup only; all camera and request values below use textContent.
      card.innerHTML = `
        <header class="ol-heading"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3"/></svg><div><h3>Lens &amp; Focus</h3><p>Camera readback · deliberate control</p></div></header>
        <p class="ol-gate" data-lens="gate">Connect a camera to use lens controls.</p>
        <div class="ol-readings"><div><span>Reported focus</span><strong data-lens="mode">Unknown</strong></div><div><span>Reported zoom</span><strong data-lens="zoom">Unknown</strong></div></div>
        <fieldset><legend>Autofocus mode</legend><div class="ol-row"><button type="button" data-lens="single" title="Request single autofocus; camera readback is shown above">AF-S · Single</button><button type="button" data-lens="continuous" title="Request continuous autofocus; camera readback is shown above">AF-C · Continuous</button></div></fieldset>
        <fieldset><legend>Manual zoom</legend><label class="ol-zoom-label">Requested factor <output data-lens="zoom-draft">1.0×</output><input data-lens="range" type="range" min="1" max="12" step="0.1" value="1" aria-label="Requested zoom factor" title="Release or finish a keyboard change to request zoom"></label><div class="ol-row"><button type="button" data-lens="minus" title="Request 0.1× less than the camera-reported zoom">− Zoom out</button><button type="button" data-lens="plus" title="Request 0.1× more than the camera-reported zoom">+ Zoom in</button></div><p class="ol-muted" data-lens="zoom-gate">Zoom needs fresh zoom and color readback.</p></fieldset>
        <p class="ol-muted">Manual focus-distance control is not available.</p>
        <details class="ol-refocus"><summary>Refocus A / B <span>Autofocus targets</span></summary><div class="ol-refocus-body">
          <p class="ol-disclosure">Refocus also moves spot exposure metering; it does not control focus distance or pull speed.</p>
          <p class="ol-muted" data-lens="point">Reported target: unknown. A target report does not prove sharpness.</p>
          <div class="ol-row ol-coordinates"><label>X · left → right<input data-lens="x" type="number" min="0" max="1" step="0.001" value="0.5" inputmode="decimal"></label><label>Y · top → bottom<input data-lens="y" type="number" min="0" max="1" step="0.001" value="0.5" inputmode="decimal"></label></div>
          <button type="button" data-lens="use-point" title="Copy the reported target into these fields without sending a command">Use reported target</button>
          <label class="ol-ack"><input data-lens="ack" type="checkbox">I understand that refocus also moves spot exposure metering.</label>
          <button type="button" data-lens="refocus" title="Request autofocus and spot exposure metering at the entered target">Refocus here</button>
          <div class="ol-marks"><div><span data-lens="mark-a">A · not saved</span><div class="ol-row"><button type="button" data-lens="save-a" title="Save these X/Y coordinates in this page session only; no camera command">Save A</button><button type="button" data-lens="recall-a" title="Request autofocus and spot metering at target A">Recall A</button></div></div><div><span data-lens="mark-b">B · not saved</span><div class="ol-row"><button type="button" data-lens="save-b" title="Save these X/Y coordinates in this page session only; no camera command">Save B</button><button type="button" data-lens="recall-b" title="Request autofocus and spot metering at target B">Recall B</button></div></div></div>
          <p class="ol-muted">Marks belong to this framing, not a subject or distance. They clear on disconnect or host restart. Recall does not guarantee focus or timing.</p>
        </div></details>
        <p class="ol-request" data-lens="request" role="status" aria-live="polite">No lens request sent from this card.</p>`;
      hostElement.append(card);
      const refs = {};
      const ref = name => refs[name] || (refs[name] = card.querySelector(`[data-lens="${name}"]`));
      let status = {}, receivedAt = performance.now(), destroyed = false, busy = false;
      let epoch = null, connected = false, session = 0, zoomEditing = false;
      let marks = {a: null, b: null};
      const listeners = [];
      const on = (name, event, callback) => {
        const element = ref(name);
        element.addEventListener(event, callback);
        listeners.push(() => element.removeEventListener(event, callback));
      };
      const pointText = point => `${point.x.toFixed(3)}, ${point.y.toFixed(3)}`;
      const note = text => { if (!destroyed) ref('request').textContent = text; };
      function reading(name) {
        const value = status.camera_feedback?.[name];
        const age = finite(value?.age_s) ? value.age_s + Math.max(0, performance.now() - receivedAt) / 1000 : Infinity;
        return status.connected === true && status.link_healthy === true && value?.reported === true &&
          age >= 0 && age < FRESH_SECONDS ? value.value : null;
      }
      function blocked() {
        if (status.connected !== true) return 'Connect a camera to use lens controls.';
        if (status.link_healthy !== true) return 'Camera link is unhealthy. Lens requests are disabled.';
        if (performance.now() - receivedAt >= FRESH_SECONDS * 1000) return 'Host status is stale. Lens requests are disabled.';
        if (status.move?.running || status.timelapse?.running) return 'Program running. Manual lens requests are disabled.';
        if (busy) return 'Request in flight. Awaiting the host; camera readback remains separate.';
        return '';
      }
      function zoomBlocked() {
        const gate = blocked();
        if (gate) return gate;
        const zoom = reading('zoom'), color = reading('color');
        if (!finite(zoom) || zoom < 1 || zoom > 12 || !['normal', 'hdr', 'd-log', 'd-log2'].includes(color)) {
          return 'Zoom needs fresh zoom and color readback.';
        }
        if (color === 'd-log2') return 'Zoom unavailable in D-Log2. Change color deliberately elsewhere; this card never changes it.';
        return '';
      }
      function enteredPoint() {
        const x = ref('x').value.trim(), y = ref('y').value.trim();
        const point = {x: Number(x), y: Number(y)};
        return x && y && validPoint(point) ? point : null;
      }
      function render() {
        if (destroyed) return;
        const gate = blocked(), zoomGate = zoomBlocked(), mode = reading('focus_mode');
        const zoom = reading('zoom'), point = reading('focus_point');
        ref('gate').textContent = gate || 'Ready · requests require a deliberate action.';
        ref('mode').textContent = mode === 'single' ? 'AF-S · camera reported' : mode === 'continuous' ? 'AF-C · camera reported' : 'Unknown / stale';
        ref('zoom').textContent = finite(zoom) && zoom >= 1 && zoom <= 12 ? `${zoom.toFixed(1)}× · camera reported` : 'Unknown / stale';
        for (const name of ['single', 'continuous']) {
          ref(name).disabled = !!gate;
          ref(name).setAttribute('aria-pressed', String(mode === name));
        }
        for (const name of ['range', 'minus', 'plus']) ref(name).disabled = !!zoomGate;
        if (!zoomGate) {
          ref('minus').disabled = zoom <= 1;
          ref('plus').disabled = zoom >= 12;
          if (!zoomEditing && document.activeElement !== ref('range')) ref('range').value = String(zoom);
        }
        ref('zoom-draft').textContent = `${Number(ref('range').value).toFixed(1)}×`;
        ref('zoom-gate').textContent = zoomGate || '1–12×. Requests never change the color profile.';
        ref('point').textContent = validPoint(point) ? `Reported target: ${pointText(point)}. Target receipt only; sharpness unconfirmed.` : 'Reported target: unknown / stale. A target report does not prove sharpness.';
        ref('use-point').disabled = !!gate || !validPoint(point);
        for (const name of ['x', 'y', 'ack']) ref(name).disabled = !!gate;
        const pointValid = !!enteredPoint();
        ref('refocus').disabled = !!gate || !pointValid || !ref('ack').checked;
        for (const name of ['a', 'b']) {
          ref(`save-${name}`).disabled = !!gate || !pointValid;
          ref(`recall-${name}`).disabled = !!gate || !marks[name] || !ref('ack').checked;
          ref(`mark-${name}`).textContent = marks[name] ? `${name.toUpperCase()} · ${pointText(marks[name])}` : `${name.toUpperCase()} · not saved`;
        }
      }
      async function send(path, body, label) {
        if (destroyed || blocked()) return;
        const ticket = session;
        busy = true;
        note(`${label} requested. Awaiting host response; camera state is not confirmed.`);
        render();
        let timeout;
        try {
          const result = await Promise.race([
            Promise.resolve(post(path, body)),
            new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('timeout')), REQUEST_TIMEOUT_MS); })
          ]);
          if (!result || result.ok === false) throw new Error('request failed');
          if (ticket === session) note(`${label} request sent. Readback above is independent; focus sharpness is not confirmed.`);
        } catch (_) {
          if (ticket === session) note(`${label} request failed or timed out. Outcome unknown; inspect camera readback before trying again.`);
        } finally {
          clearTimeout(timeout);
          busy = false;
          render();
        }
      }
      function requestZoom(value) {
        if (zoomBlocked() || !finite(value) || value < 1 || value > 12) return;
        send('/api/camera', {set: 'zoom', value: Math.round(value * 10) / 10}, `Zoom ${value.toFixed(1)}×`);
      }
      function requestPoint(point) {
        if (blocked() || !ref('ack').checked || !validPoint(point)) return;
        send('/api/camera/focus-target', {x: point.x, y: point.y, acknowledge_exposure: true}, `Refocus target ${pointText(point)}`);
      }
      on('single', 'click', () => send('/api/camera', {set: 'focus_continuous', value: false}, 'AF-S'));
      on('continuous', 'click', () => send('/api/camera', {set: 'focus_continuous', value: true}, 'AF-C'));
      on('range', 'input', () => { zoomEditing = true; ref('zoom-draft').textContent = `${Number(ref('range').value).toFixed(1)}×`; });
      on('range', 'change', () => { const value = Number(ref('range').value); zoomEditing = false; requestZoom(value); render(); });
      on('range', 'blur', () => { zoomEditing = false; render(); });
      on('minus', 'click', () => requestZoom(Math.max(1, reading('zoom') - 0.1)));
      on('plus', 'click', () => requestZoom(Math.min(12, reading('zoom') + 0.1)));
      for (const name of ['x', 'y']) on(name, 'input', render);
      on('ack', 'change', render);
      on('refocus', 'click', () => requestPoint(enteredPoint()));
      on('use-point', 'click', () => {
        const point = reading('focus_point');
        if (blocked() || !validPoint(point)) return;
        ref('x').value = String(point.x); ref('y').value = String(point.y); render();
      });
      for (const name of ['a', 'b']) {
        on(`save-${name}`, 'click', () => {
          const point = enteredPoint();
          if (blocked() || !point) return;
          marks[name] = {...point}; note(`Target ${name.toUpperCase()} saved in this page session. No camera command sent.`); render();
        });
        on(`recall-${name}`, 'click', () => requestPoint(marks[name]));
      }
      const timer = setInterval(render, 500);
      render();
      return {
        update(nextStatus) {
          if (destroyed) return;
          const next = nextStatus && typeof nextStatus === 'object' ? nextStatus : {};
          const nextEpoch = typeof next.workspace?.generation === 'string' ? next.workspace.generation.split(':')[0] : null;
          const nextConnected = next.connected === true;
          if ((connected && !nextConnected) || (epoch !== null && nextEpoch !== null && epoch !== nextEpoch)) {
            session++;
            marks = {a: null, b: null}; ref('ack').checked = false;
            ref('x').value = '0.5'; ref('y').value = '0.5'; zoomEditing = false;
            note('Connection or host changed. Refocus marks and exposure acknowledgement cleared.');
          }
          if (nextEpoch !== null) epoch = nextEpoch;
          connected = nextConnected; status = next; receivedAt = performance.now(); render();
        },
        destroy() {
          destroyed = true; session++;
          clearInterval(timer); listeners.forEach(remove => remove());
          marks = {a: null, b: null}; card.remove();
        }
      };
    }
  };
})();
