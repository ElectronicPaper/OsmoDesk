/* Standalone, opt-in workspace layout. It moves panel wrappers with left/top;
 * registered children (including live view/video/canvas) keep
 * their DOM identity and are never reparented. */
(function (global) {
  'use strict';

  var PREFIX = 'osmo.workspace-layout.';
  var BREAKPOINTS = { desktop: 900, tablet: 600 };
  var COLS = { desktop: 12, tablet: 8, mobile: 1 };
  var ROW = 72;
  var GAP = 10;

  function breakpointFor(width) {
    return width >= BREAKPOINTS.desktop ? 'desktop' : width >= BREAKPOINTS.tablet ? 'tablet' : 'mobile';
  }
  function number(value, fallback, minimum, maximum) {
    value = Number(value);
    return Number.isFinite(value) ? Math.min(maximum, Math.max(minimum, Math.round(value))) : fallback;
  }
  function keyFor(id, breakpoint) { return PREFIX + id + '.' + breakpoint; }
  function clone(item) { return { i: item.i, x: item.x, y: item.y, w: item.w, h: item.h, minW: item.minW, minH: item.minH, static: !!item.static }; }
  function defaults(panels, cols) {
    var y = 0, x = 0, rowHeight = 0;
    return panels.map(function (panel, index) {
      var w = cols === 1 ? 1 : Math.min(cols, number(panel.w, Math.min(cols, 4), 1, cols));
      if(x+w>cols){y+=rowHeight;x=0;rowHeight=0;}
      var item = { i: panel.id, x:x, y:y, w:w, h:number(panel.h,4,1,100), minW:Math.min(cols,number(panel.minW,1,1,cols)),minH:number(panel.minH,1,1,100),static:!!panel.pinned };
      x+=w;rowHeight=Math.max(rowHeight,item.h);
      return item;
    });
  }
  function normalizeLayout(value, panels, cols) {
    var byId = {};
    if (Array.isArray(value)) value.forEach(function (raw) {
      if (!raw || typeof raw.i !== 'string') return;
      byId[raw.i] = raw;
    });
    var shipped = defaults(panels, cols);
    return shipped.map(function (base) {
      var raw = byId[base.i];
      if (!raw) return base;
      var w = number(raw.w, base.w, Math.min(base.minW, cols), cols);
      var h = number(raw.h, base.h, base.minH, 100);
      return { i: base.i, x: number(raw.x, base.x, 0, Math.max(0, cols - w)), y: number(raw.y, base.y, 0, 1000), w: w, h: h, minW: base.minW, minH: base.minH, static: !!raw.static };
    });
  }
  function readLayout(id, breakpoint, panels, cols) {
    try {
      var raw = global.localStorage && global.localStorage.getItem(keyFor(id, breakpoint));
      if (!raw) return null;
      var value = JSON.parse(raw);
      return value && value.version === 1 ? normalizeLayout(value.layout, panels, cols) : null;
    } catch (_) { return null; }
  }
  function writeLayout(id, breakpoint, layout, hidden) {
    try {
      if (!global.localStorage) return;
      global.localStorage.setItem(keyFor(id, breakpoint), JSON.stringify({ version: 1, layout: layout.map(clone), hidden: hidden.slice() }));
    } catch (_) { /* private mode/quota must not break the desk */ }
  }
  function readHidden(id, breakpoint, panelIds) {
    try {
      var raw = global.localStorage && global.localStorage.getItem(keyFor(id, breakpoint));
      var value = raw && JSON.parse(raw);
      if (!value || value.version !== 1 || !Array.isArray(value.hidden)) return [];
      return value.hidden.filter(function (item) { return typeof item === 'string' && panelIds.indexOf(item) !== -1; });
    } catch (_) { return []; }
  }
  function coreMove(api, layout, item, x, y, cols) {
    if (api && api.moveItemWithCompactor && api.verticalCompactor) {
      try { return api.moveItemWithCompactor(layout, item, x, y, { compactor: api.verticalCompactor, cols: cols, isUserAction: true }); } catch (_) { /* use safe fallback */ }
    }
    return layout.map(function (entry) { return entry.i === item.i ? Object.assign({}, entry, { x: x, y: y }) : entry; });
  }
  function coreResize(api, layout, item, next, cols) {
    if (api && api.resizeItemWithCompactor && api.verticalCompactor) {
      try { return api.resizeItemWithCompactor(layout, item, next, { compactor: api.verticalCompactor, cols: cols }); } catch (_) { /* use safe fallback */ }
    }
    return layout.map(function (entry) { return entry.i === item.i ? Object.assign({}, entry, next) : entry; });
  }
  var helpers = { breakpointFor: breakpointFor, normalizeLayout: normalizeLayout, defaults: defaults, readLayout: readLayout, keyFor: keyFor };
  global.OsmoWorkspaceLayoutHelpers = helpers;

  global.createWorkspaceLayout = function (root, options) {
    if (!root || !options || !options.id || !Array.isArray(options.panels)) throw new Error('A root, stable workspace id and panels are required.');
    var panels = options.panels.filter(function (panel) { return panel && typeof panel.id === 'string' && panel.element && panel.element.nodeType === 1; });
    var byId = {};
    panels.forEach(function (panel) { if (byId[panel.id]) throw new Error('Duplicate workspace panel id: ' + panel.id); byId[panel.id] = panel; });
    var active = false, arranging = false, breakpoint = '', layout = [], hidden = [], toolbar, observer, drag, originals = new Map();
    var panelIds = panels.map(function (panel) { return panel.id; });
    var api = global.OsmoSnapgrid;
    var hasCore = !!(api && typeof api.moveItemWithCompactor === 'function' && typeof api.resizeItemWithCompactor === 'function');

    function currentCols() { return COLS[breakpoint] || 1; }
    function item(id) { return layout.filter(function (entry) { return entry.i === id; })[0]; }
    function allowed() { return typeof options.canArrange === 'function' ? options.canArrange() : options.canArrange !== false; }
    function editable() { return hasCore && allowed() && arranging; }
    function save() { writeLayout(options.id, breakpoint, layout, hidden); }
    function announce(text) { if (toolbar) { var note = toolbar.querySelector('[data-workspace-note]'); if (note) note.textContent = text; } }
    function setBreakpoint() {
      var next = breakpointFor(root.clientWidth || 0);
      if (next === breakpoint) return false;
      breakpoint = next;
      var cols = currentCols();
      layout = readLayout(options.id, breakpoint, panels, cols) || defaults(panels, cols);
      hidden = readHidden(options.id, breakpoint, panelIds);
      return true;
    }
    function ensureContentHeight(entry, panel) {
      // Auto-height tools may change tabs. Measure content, not last tab's
      // imposed minimum, while ordinary resizable panels keep their height.
      if(panel.fitContent) panel.element.style.minHeight='0';
      var needed = panel.element.scrollHeight || 0;
      var minimum = Math.max(entry.minH, Math.ceil((needed + GAP) / (ROW + GAP)));
      if (panel.fitContent || entry.h < minimum) entry.h = minimum;
    }
    function render() {
      if (!active || !root.clientWidth || !hasCore) return;
      setBreakpoint();
      if(!allowed()) arranging=false;
      var cols = currentCols(), width = Math.max(1, root.clientWidth), cell = (width - GAP * (cols - 1)) / cols;
      var toolbarHeight = toolbar ? toolbar.offsetHeight + 16 : 0;
      root.classList.add('osmo-workspace-layout');
      root.setAttribute('data-workspace-breakpoint', breakpoint);
      panels.forEach(function (panel) {
        var entry = item(panel.id);
        if (!entry) return;
        var el = panel.element, isHidden = hidden.indexOf(panel.id) !== -1;
        el.classList.add('osmo-workspace-panel');
        el.setAttribute('data-workspace-panel', panel.id);
        el.style.display = isHidden ? 'none' : '';
        // Percent-based geometry adapts in the same style/layout pass as a
        // viewport resize, before JS receives its resize notification.
        el.style.width = 'calc('+(entry.w/cols*100)+'% - '+(GAP*(cols-entry.w)/cols)+'px)';
        el.style.minHeight = '0';
        if(!isHidden) ensureContentHeight(entry,panel);
        el.classList.toggle('is-workspace-pinned', !!entry.static);
        el.classList.toggle('is-workspace-arranging', editable());
        attachControls(panel);
      });
      var visible=layout.filter(function(entry){return hidden.indexOf(entry.i)<0;});
      // Reconcile dynamic content growth before positioning. Pin freezes user
      // placement, not permission to overlap another panel's expanded form.
      var compact=api.verticalCompactor.compact(visible.map(function(e){return Object.assign({},e,{static:false});}),cols);
      compact.forEach(function(e){var entry=item(e.i);entry.x=e.x;entry.y=e.y;});
      panels.forEach(function(panel){var entry=item(panel.id),el=panel.element;
        el.style.left='calc('+(entry.x/cols*100)+'% + '+(GAP*entry.x/cols)+'px)';
        el.style.top=(toolbarHeight+entry.y*(ROW+GAP))+'px';
        el.style.minHeight=Math.max(ROW,entry.h*(ROW+GAP)-GAP)+'px';
      });
      root.style.minHeight = (toolbarHeight+Math.max(ROW,visible.reduce(function(max,e){return Math.max(max,e.y+e.h);},0)*(ROW+GAP)))+'px';
      root.classList.toggle('is-arranging',editable());
      updateToolbar();
    }
    function button(label, action, title) {
      var el = document.createElement('button'); el.type = 'button'; el.className = 'osmo-workspace-button'; el.textContent = label; el.setAttribute('data-workspace-action', action); el.title = title; el.setAttribute('aria-label', title); return el;
    }
    function attachControls(panel) {
      var el = panel.element, controls = el.querySelector(':scope > [data-workspace-controls]');
      if (!controls) {
        controls = document.createElement('div'); controls.className = 'osmo-workspace-controls'; controls.setAttribute('data-workspace-controls', '');
        var move = button('Move', 'move', 'Move ' + (panel.title || panel.id) + '. Arrow keys move; Shift plus Arrow resizes; Escape cancels.');
        var resize = button('Resize', 'resize', 'Resize ' + (panel.title || panel.id) + '. Drag the handle.');
        var pin = button('Pin', 'pin', 'Pin or unpin ' + (panel.title || panel.id));
        var hide = button('Hide', 'hide', 'Hide ' + (panel.title || panel.id));
        controls.append(move, resize, pin, hide); el.appendChild(controls);
        move.addEventListener('keydown', function (event) { keyboard(panel.id, event); });
        move.addEventListener('pointerdown', function (event) { beginPointer(panel.id, 'move', event); });
        resize.addEventListener('pointerdown', function (event) { beginPointer(panel.id, 'resize', event); });
        pin.addEventListener('click', function () { if (editable()) togglePin(panel.id); });
        hide.addEventListener('click', function () { if (editable()) toggleHidden(panel.id, true); });
      }
      controls.hidden = !editable();
      var entry = item(panel.id);
      controls.querySelector('[data-workspace-action="pin"]').textContent = entry && entry.static ? 'Unpin' : 'Pin';
    }
    function updateToolbar() {
      if (!toolbar) return;
      var arrange = toolbar.querySelector('[data-workspace-arrange]');
      arrange.disabled = !hasCore || !allowed(); arrange.setAttribute('aria-pressed', String(arranging));
      arrange.textContent = arranging ? 'Done arranging' : 'Arrange panels';
      var tray = toolbar.querySelector('[data-workspace-tray]'); tray.textContent = '';
      hidden.forEach(function (id) { var panel = byId[id]; var restore = button('Restore ' + (panel.title || id), 'restore', 'Restore ' + (panel.title || id)); restore.addEventListener('click', function () { toggleHidden(id, false); }); tray.appendChild(restore); });
    }
    function togglePin(id) { var entry = item(id); if (!entry) return; entry.static = !entry.static; save(); render(); announce((byId[id].title || id) + (entry.static ? ' pinned.' : ' unpinned.')); }
    function toggleHidden(id, shouldHide) { if (!editable() && shouldHide) return; hidden = hidden.filter(function (value) { return value !== id; }); if (shouldHide) hidden.push(id); save(); render(); announce((byId[id].title || id) + (shouldHide ? ' moved to the restore tray.' : ' restored.')); }
    function keyboard(id, event) {
      if (!editable()) return;
      var steps = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
      if (event.key === 'Escape') { event.stopPropagation(); if (drag) { layout = drag.before; drag = null; render(); announce('Arrangement cancelled.'); } else {arranging=false;render();} return; }
      if (!steps) return;
      event.preventDefault(); event.stopPropagation(); var entry = item(id); if (!entry || entry.static) return;
      var cols = currentCols();
      if (event.shiftKey) {
        layout = coreResize(api, layout, entry, { x: entry.x, y: entry.y, w: number(entry.w + steps[0], entry.w, entry.minW, cols), h: number(entry.h + steps[1], entry.h, entry.minH, 100) }, cols);
      } else {
        layout = coreMove(api, layout, entry, number(entry.x + steps[0], entry.x, 0, cols - entry.w), number(entry.y + steps[1], entry.y, 0, 1000), cols);
      }
      save(); render(); announce((byId[id].title || id) + ' arranged.');
    }
    function beginPointer(id, kind, event) {
      if (!editable() || event.button !== 0) return; var entry = item(id); if (!entry || entry.static) return;
      event.preventDefault(); var rect = root.getBoundingClientRect(), panelRect = byId[id].element.getBoundingClientRect();
      drag = { id: id, kind: kind, pointer: { x: event.clientX, y: event.clientY }, before: layout.map(clone), start: clone(entry), rootRect: rect, panelRect: panelRect };
      event.currentTarget.setPointerCapture(event.pointerId);
      event.currentTarget.addEventListener('pointermove', pointerMove); event.currentTarget.addEventListener('pointerup', pointerEnd, { once: true }); event.currentTarget.addEventListener('pointercancel', pointerCancel, { once: true });
    }
    function pointerMove(event) {
      if(!editable()){pointerCancel(event);return;}
      if (!drag) return; var entry = item(drag.id), cols = currentCols(), cell = Math.max(1, (root.clientWidth - GAP * cols) / cols);
      var dx = Math.round((event.clientX - drag.pointer.x) / (cell + GAP)), dy = Math.round((event.clientY - drag.pointer.y) / (ROW + GAP));
      if (drag.kind === 'resize') layout = coreResize(api, drag.before, drag.start, { x: drag.start.x, y: drag.start.y, w: number(drag.start.w + dx, drag.start.w, drag.start.minW, cols), h: number(drag.start.h + dy, drag.start.h, drag.start.minH, 100) }, cols);
      else layout = coreMove(api, drag.before, drag.start, number(drag.start.x + dx, drag.start.x, 0, cols - drag.start.w), number(drag.start.y + dy, drag.start.y, 0, 1000), cols);
      render();
    }
    function pointerEnd(event) { if (!drag) return; event.currentTarget.removeEventListener('pointermove', pointerMove); drag = null; save(); render(); announce('Arrangement saved.'); }
    function pointerCancel(event) { if (!drag) return; event.currentTarget.removeEventListener('pointermove', pointerMove); layout = drag.before; drag = null; render(); announce('Arrangement cancelled.'); }
    function activate() {
      if (active) return api;
      active = true;
      // A missing vendor bundle is not a reason to leave a page half-docked.
      // Keep the page's authored, ordinary-flow presentation until the bundle
      // is available on a later page load.
      if (!hasCore) { root.setAttribute('data-workspace-layout-fallback', 'static'); return null; }
      panels.forEach(function (panel) { originals.set(panel.element, { style: panel.element.getAttribute('style'), className: panel.element.className }); });
      toolbar = document.createElement('div'); toolbar.className = 'osmo-workspace-toolbar'; toolbar.setAttribute('data-workspace-toolbar', '');
      var arrange = button('Arrange panels', 'arrange', 'Arrange panels. Move and resize with accessible handles.'); arrange.setAttribute('data-workspace-arrange', ''); arrange.addEventListener('click', function () { if (allowed()) { arranging = !arranging; render(); announce(arranging ? 'Arrange mode enabled. Panel controls are paused.' : 'Arrange mode disabled.'); } });
      var reset = button('Reset arrangement', 'reset', 'Restore the shipped panel arrangement.'); reset.addEventListener('click', function () { if (!editable()) return; try { global.localStorage.removeItem(keyFor(options.id, breakpoint)); } catch (_) {} layout = defaults(panels, currentCols()); hidden = []; render(); announce('Shipped arrangement restored.'); });
      var help = document.createElement('details'); help.className = 'osmo-workspace-help'; help.innerHTML = '<summary>How to arrange</summary><p>Drag Move or Resize. On Move, arrows reposition and Shift + arrows resize. Escape cancels. Pin protects a panel from accidental edits; Hide moves it to the restore tray. Live controls pause while arranging.</p>';
      var tray = document.createElement('span'); tray.setAttribute('data-workspace-tray', ''); tray.className = 'osmo-workspace-tray';
      var note = document.createElement('span'); note.setAttribute('data-workspace-note', ''); note.className = 'osmo-workspace-note'; note.setAttribute('aria-live', 'polite');
      toolbar.append(arrange, reset, tray, help, note); root.appendChild(toolbar);
      // Viewport changes need immediate geometry. Observer batching is only
      // for content expansion; waiting a frame can expose stale panel offsets.
      global.addEventListener('resize', render);
      if (typeof global.ResizeObserver === 'function') {
        var queued=false;
        observer = new global.ResizeObserver(function () { if(!queued){queued=true;requestAnimationFrame(function(){queued=false;render();});} }); observer.observe(root); panels.forEach(function (panel) { observer.observe(panel.element); });
      } else {
        observer = { disconnect: function () { global.removeEventListener('resize', render); } };
        global.addEventListener('resize', render);
      }
      setBreakpoint(); render(); return api;
    }
    function refresh() { if (active) render(); }
    root.addEventListener('pointerdown',function(event){if(editable()&&event.target.closest('[data-workspace-panel]')&&!event.target.closest('[data-workspace-controls]')){event.preventDefault();event.stopImmediatePropagation();}},true);
    root.addEventListener('click',function(event){if(editable()&&event.target.closest('[data-workspace-panel]')&&!event.target.closest('[data-workspace-controls]')){event.preventDefault();event.stopImmediatePropagation();}},true);
    function destroy() {
      global.removeEventListener('resize',render);
      if (!active) return; if (observer) observer.disconnect(); if (toolbar) toolbar.remove(); panels.forEach(function (panel) { var original = originals.get(panel.element); panel.element.querySelector(':scope > [data-workspace-controls]')?.remove(); panel.element.classList.remove('osmo-workspace-panel', 'is-workspace-pinned', 'is-workspace-arranging'); panel.element.removeAttribute('data-workspace-panel'); if (original) { if (original.style === null) panel.element.removeAttribute('style'); else panel.element.setAttribute('style', original.style); } }); root.classList.remove('osmo-workspace-layout'); root.removeAttribute('data-workspace-breakpoint'); root.removeAttribute('data-workspace-layout-fallback'); root.style.minHeight = ''; active = false;
    }
    return { activate: activate, refresh: refresh, destroy: destroy };
  };
})(typeof window !== 'undefined' ? window : globalThis);
