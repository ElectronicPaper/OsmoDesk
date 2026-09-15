/* Shared, local-only UI language and contextual help. No camera commands. */
(() => {
  'use strict';
  const paths={settings:'M9 3h6l1 3 3 1v6l-3 1-1 3H9l-1-3-3-1V7l3-1z M15 10a3 3 0 1 1-6 0a3 3 0 1 1 6 0',
    help:'M9 8a3 3 0 0 1 6 0c0 2-3 2-3 5 M12 17v.1 M22 12A10 10 0 1 1 2 12a10 10 0 0 1 20 0',
    compose:'M4 4h16v16H4z M8 9h8M8 14h5',director:'M3 6h18v14H3z M3 6l3-4h5l-3 4m5 0 3-4h5l-3 4 M10 10l5 3-5 3z',
    shoot:'M3 7h13v12H3z M16 11l5-3v10l-5-3',review:'M3 4h18v16H3z M7 12l3 3 7-7',rig:'M3 6h18M3 12h18M3 18h18 M7 3v6M16 9v6M10 15v6',export:'M12 3v12m-4-4 4 4 4-4 M4 15v6h16v-6',close:'M6 6l12 12M6 18 18 6'};
  function icon(name){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');svg.classList.add('osmo-icon');const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',paths[name]||paths.help);svg.append(path);return svg;}
  window.OsmoUI={icon};
  window.createSurfaceUI=({host,post,reload,canArrange=()=>true})=>{
    const utilities=document.createElement('div');utilities.className='osmo-utilities';
    function utility(label,type){const b=document.createElement('button');b.type='button';b.append(icon(type),document.createTextNode(label));b.title=label;b.className='osmo-utility';utilities.append(b);return b;}
    const settingsButton=utility('Settings','settings'),helpButton=utility('Guide','help');
    const lensButton=utility('Lens','shoot');
    lensButton.title='Lens, autofocus and refocus targets';
    (host||document.body).append(utilities);if(!host)utilities.classList.add('osmo-floating');
    function modal(label){const dialog=document.createElement('dialog');dialog.className='osmo-dialog';dialog.setAttribute('aria-label',label);const head=document.createElement('div');head.className='osmo-dialog-head';const title=document.createElement('strong');title.textContent=label;const close=document.createElement('button');close.type='button';close.append(icon('close'));close.setAttribute('aria-label','Close '+label);close.onclick=()=>dialog.close();head.append(title,close);const content=document.createElement('div');dialog.append(head,content);document.body.append(dialog);dialog.addEventListener('keydown',e=>e.stopPropagation());dialog.addEventListener('cancel',e=>e.stopPropagation());return {dialog,content};}
    const settings=modal('Studio settings'),help=modal('OsmoDesk field guide');
    const lens=modal('Lens & Focus');
    lens.dialog.style.width='min(520px,calc(100% - 24px))';
    const lensUI=window.OsmoLens?.mount(lens.content,{post});
    let lensStatus=null;
    lensButton.onclick=()=>{if(lensStatus?.clutch?.engaged||lensStatus?.move?.running||lensStatus?.timelapse?.running)return;lens.dialog.showModal();};
    window.addEventListener('osmo-status',event=>{lensStatus=event.detail;lensUI?.update({...lensStatus,connected:lensStatus?.state==='connected'});lensButton.disabled=!!(lensStatus?.clutch?.engaged||lensStatus?.move?.running||lensStatus?.timelapse?.running);});
    const settingsUI=createStudioSettings(settings.content,{post,reload});
    function openSettings(){if(!canArrange())return;settings.dialog.showModal();settingsUI.activate();}
    settingsButton.onclick=openSettings;window.addEventListener('osmo-open-settings',openSettings);
    const guides=[
      ['compose','Compose','Start with two framing points. Adjust travel time and holds in the point editor. Preview is offline; Capture reads the connected head. Export a draft before replacing unsaved local edits.'],
      ['director','Director & AI','Rehearse rhythm without moving the head. The path is gimbal-angle arithmetic, not a view through the lens. A cue waits for your Continue. AI only proposes names and timing: Review → Send → Preview → Apply. Each cloud request shares the exact disclosed context; it never operates the camera.'],
      ['shoot','Shoot safely','Hold the pad for manual motion; release stops that gesture. Enable motion deliberately before running a shot. Roll requests recording, waits pre-roll, then moves. Stop motion immediately neutralises the head; it does not stop recording. Amber REC means requested, never confirmed.'],
      ['review','Review & hand off','Log each take, circle useful takes, then compare measured traces. Gaps and aborted runs stay visible. An editorial package exports the selected take’s original path and measured CSV/CHAN plus hashes—not the current draft. Export frames are not video timecode. Labels and notes are opt-in.'],
      ['rig','Rig & crew','Save lens assumptions and the physical setup to reproduce framing. Only the local host owner manages API keys, crew permissions and recovery. Crew sessions expire or can be revoked; restart closes them. Use a trusted network: plain HTTP is not encrypted.'],
      ['settings','Your workspace','Arrange panels when motion is inactive. Drag the handles or use arrow keys; Shift + arrows resize. Pin protects panels, Hide sends them to a restore tray. Layouts are separate for desktop, tablet and phone. Reset restores the shipped arrangement; no shot or settings are deleted.'],
    ];
    const heading=document.createElement('p');heading.textContent='A field guide, not a wall of instructions. Open the part you need.';help.content.append(heading);
    for(const [name,title,description] of guides){const details=document.createElement('details');details.className='osmo-guide';const summary=document.createElement('summary');summary.append(icon(name),document.createTextNode(title));const p=document.createElement('p');p.textContent=description;details.append(summary,p);help.content.append(details);}
    helpButton.onclick=()=>{if(canArrange())help.dialog.showModal();};
    // Native titles gain the same visible tooltip on keyboard focus. The Guide
    // remains available to touch users; safety labels never depend on hover.
    const tooltip=document.createElement('div');tooltip.id='osmoTooltip';tooltip.role='tooltip';tooltip.hidden=true;document.body.append(tooltip);
    let described=null,previousDescription=null;
    function hide(){tooltip.hidden=true;if(described){if(previousDescription)described.setAttribute('aria-describedby',previousDescription);else described.removeAttribute('aria-describedby');}described=null;}
    function show(event){const el=event.target.closest?.('[title]');if(!el||!el.title||el.disabled)return;hide();tooltip.textContent=el.title;tooltip.hidden=false;described=el;previousDescription=el.getAttribute('aria-describedby');el.setAttribute('aria-describedby',[previousDescription,'osmoTooltip'].filter(Boolean).join(' '));const r=el.getBoundingClientRect();tooltip.style.left=Math.max(8,Math.min(innerWidth-tooltip.offsetWidth-8,r.left))+'px';tooltip.style.top=Math.max(8,Math.min(innerHeight-tooltip.offsetHeight-8,r.bottom+8))+'px';}
    document.addEventListener('focusin',show);document.addEventListener('focusout',hide);document.addEventListener('pointerover',e=>{if(e.pointerType==='mouse')show(e);});document.addEventListener('pointerout',hide);document.addEventListener('scroll',hide,true);
    document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!tooltip.hidden){hide();}},true);
    window.addEventListener('resize',hide);
    // Tokens are exchanged for HttpOnly cookies by the host; remove query
    // credentials from the address bar/history immediately after page load.
    const url=new URL(location.href);if(url.searchParams.has('t')){url.searchParams.delete('t');history.replaceState(null,'',url.pathname+url.search+url.hash);}
    fetch('/api/access',{signal:AbortSignal.timeout(3000)}).then(r=>r.json()).then(a=>{document.body.dataset.role=a.identity?.role||'unknown';if(a.identity?.role==='viewer'||a.identity?.role==='editor'){const note=document.createElement('div');note.className='osmo-role';note.textContent=a.identity.role==='viewer'?'Viewer · read-only crew access':'Editor · draft tools only; camera control unavailable';utilities.before(note);}}).catch(()=>{});
    return {openSettings,settingsUI};
  };
})();
