// Offline Three.js renderer. Public pitch/yaw values are degrees and are never normalized.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const REF_R = 10;
const PANO_R = 11;
const DPR_CAP = 1.5;
const MAX_ANGLE = 1_000_000;
const rad = THREE.MathUtils.degToRad;
const deg = THREE.MathUtils.radToDeg;

function finite(value, low, high) {
  return Number.isFinite(value) && value >= low && value <= high;
}

export function isValidSpatialPose(pose) {
  return Boolean(pose)
    && finite(pose.pitch, -MAX_ANGLE, MAX_ANGLE)
    && finite(pose.yaw, -MAX_ANGLE, MAX_ANGLE);
}

function isImageDataUrl(url) {
  return typeof url === 'string'
    && /^data:image\/(?:jpeg|png|webp);base64,[a-z0-9+/=\s]+$/i.test(url);
}

export function directionFromPitchYaw(pitch, yaw, out = new THREE.Vector3()) {
  const pitchRad = rad(pitch);
  const yawRad = rad(yaw);
  const cosine = Math.cos(pitchRad);
  return out.set(Math.sin(yawRad) * cosine, Math.sin(pitchRad), -Math.cos(yawRad) * cosine);
}

export function pitchYawFromDirection(direction, near = null) {
  const primary = {
    pitch: deg(Math.asin(THREE.MathUtils.clamp(direction.y, -1, 1))),
    yaw: deg(Math.atan2(direction.x, -direction.z)),
  };
  if (!isValidSpatialPose(near)) return primary;
  // Direction has two Euler representations. Stay near the authored encoder
  // turn (Pocket's normal pitch can be >90), not an arbitrary latitude branch.
  const close = (value, reference) => value + 360 * Math.round((reference - value) / 360);
  return [primary, {pitch: 180 - primary.pitch, yaw: primary.yaw + 180}]
    .map(p => ({pitch: close(p.pitch, near.pitch), yaw: close(p.yaw, near.yaw)}))
    .sort((a,b) => Math.hypot(a.pitch-near.pitch,a.yaw-near.yaw)-Math.hypot(b.pitch-near.pitch,b.yaw-near.yaw))[0];
}

export function hfovToVfovDeg(hfov, aspect) {
  return deg(2 * Math.atan(Math.tan(rad(hfov) / 2) / aspect));
}

export function panoramaDirectionForUv(u, v, out = new THREE.Vector3()) {
  // Center maps to -Z; top maps to +Y; right maps to positive yaw.
  return directionFromPitchYaw((v - 0.5) * 180, (u - 0.5) * 360, out);
}

function poseEuler(pose) {
  return new THREE.Euler(rad(pose.pitch), -rad(pose.yaw), 0, 'YXZ');
}

export function referenceDirectionForScreen(reference, x, y, defaults = {}) {
  const hfov = reference.hfov ?? defaults.hfov ?? 84;
  const aspect = reference.aspect ?? defaults.aspect ?? 16 / 9;
  const vfov = hfovToVfovDeg(hfov, aspect);
  const local = new THREE.Vector3(
    x * Math.tan(rad(hfov) / 2),
    y * Math.tan(rad(vfov) / 2),
    -1,
  ).normalize();
  return local.applyEuler(poseEuler(reference));
}

function setLayerDeep(object, layer) {
  object.traverse((child) => child.layers.set(layer));
}

export function markerColor(kind) {
  return kind === 'avoid' ? 0xff4444 : kind === 'waypoint' ? 0x44ddff : 0xffffff;
}

function disposeObject(object) {
  object.traverse((child) => {
    child.geometry?.dispose();
    const materials = Array.isArray(child.material) ? child.material : [child.material];
    materials.filter(Boolean).forEach((material) => {
      material.map?.dispose();
      material.dispose();
    });
  });
}

export function disposeGroupChildren(group) {
  while (group.children.length) {
    const child = group.children[0];
    group.remove(child);
    disposeObject(child);
  }
}

function cleanPoints(points, maximum) {
  return Array.isArray(points) && points.length <= maximum && points.every(isValidSpatialPose)
    ? points
    : [];
}

export function cleanSpatialData(input) {
  if (!input || typeof input !== 'object') return null;
  const hfov = input.hfov ?? 84;
  const aspect = input.aspect ?? 16 / 9;
  if (!finite(hfov, 1, 179) || !finite(aspect, 0.1, 10)) return null;
  const imageReference = (reference) => isValidSpatialPose(reference)
    && finite(reference.hfov ?? hfov, 1, 179)
    && finite(reference.aspect ?? aspect, 0.1, 10)
    && isImageDataUrl(reference.url);
  const pointsValid = (value, maximum) => value === undefined ||
    (Array.isArray(value) && value.length <= maximum && value.every(isValidSpatialPose));
  if (!pointsValid(input.samples,10_000) || !pointsValid(input.waypoints,200) ||
      !pointsValid(input.markers,24)) return null;
  if (input.ghostSegments !== undefined && (!Array.isArray(input.ghostSegments) ||
      input.ghostSegments.length > 20_000 ||
      !input.ghostSegments.every(segment => Array.isArray(segment) && pointsValid(segment,20_000)) ||
      input.ghostSegments.reduce((n,segment) => n + segment.length,0) > 20_000)) return null;
  if (input.references !== undefined && (!Array.isArray(input.references) ||
      input.references.length > 24 || !input.references.every(imageReference))) return null;
  if (input.panorama && (!isValidSpatialPose(input.panorama) || !isImageDataUrl(input.panorama.url))) return null;
  return {
    hfov,
    aspect,
    samples: cleanPoints(input.samples, 10_000),
    waypoints: cleanPoints(input.waypoints, 1_000),
    markers: cleanPoints(input.markers, 1_000),
    ghostSegments: Array.isArray(input.ghostSegments)
      ? input.ghostSegments.map((segment) => cleanPoints(segment, 20_000))
      : [],
    references: Array.isArray(input.references) && input.references.length <= 64
      ? input.references.filter(imageReference)
      : [],
    panorama: isValidSpatialPose(input.panorama) && isImageDataUrl(input.panorama.url)
      ? input.panorama
      : null,
  };
}

function dataFailure(input) {
  if (!input || typeof input !== 'object') return 'Invalid spatial data.';
  const images = [...(Array.isArray(input.references) ? input.references : []), input.panorama]
    .filter(Boolean);
  return images.some((image) => !isImageDataUrl(image.url))
    ? 'Spatial images must be JPEG, PNG, or WebP data URLs.'
    : 'Invalid spatial data.';
}

function createDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  // A caller may request ready() later; suppress host-level unhandled warnings meanwhile.
  promise.catch(() => {});
  return { promise, resolve, reject };
}

function loadTexture(url, material, isCurrent, redraw) {
  return new Promise((resolve, reject) => {
    new THREE.TextureLoader().load(url, (texture) => {
      if (!isCurrent()) {
        texture.dispose();
        reject(new Error('Spatial image load became stale.'));
        return;
      }
      if (texture.image.width > 8192 || texture.image.height > 8192 || texture.image.width * texture.image.height > 16 * 1024 * 1024) {
        texture.dispose();
        reject(new Error('Reference exceeds the texture size limit. Import a resized image.'));
        return;
      }
      texture.colorSpace = THREE.SRGBColorSpace;
      material.map = texture;
      material.color.set(0xffffff);
      material.needsUpdate = true;
      redraw();
      resolve();
    }, undefined, () => reject(new Error('Spatial image failed to load.')));
  });
}

function meshForReference(reference, data, isCurrent, redraw, loads) {
  const divisions = 12;
  const positions = new Float32Array((divisions + 1) ** 2 * 3);
  const uvs = new Float32Array((divisions + 1) ** 2 * 2);
  let positionAt = 0;
  let uvAt = 0;
  for (let y = 0; y <= divisions; y += 1) {
    for (let x = 0; x <= divisions; x += 1) {
      const point = referenceDirectionForScreen(
        reference, (x / divisions - 0.5) * 2, (0.5 - y / divisions) * 2, data,
      ).multiplyScalar(REF_R);
      positions.set(point.toArray(), positionAt); positionAt += 3;
      uvs.set([x / divisions, 1 - y / divisions], uvAt); uvAt += 2;
    }
  }
  const indices = [];
  for (let y = 0; y < divisions; y += 1) for (let x = 0; x < divisions; x += 1) {
    const a = y * (divisions + 1) + x;
    const b = a + 1;
    const c = a + divisions + 1;
    indices.push(a, c, b, b, c, c + 1);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
  geometry.setIndex(indices);
  const material = new THREE.MeshBasicMaterial({ color: 0x222222, side: THREE.DoubleSide, toneMapped: false });
  loads.push(loadTexture(reference.url, material, isCurrent, redraw));
  return new THREE.Mesh(geometry, material);
}

function meshForPanorama(panorama, isCurrent, redraw, loads) {
  const rows = 32;
  const columns = 48;
  const positions = new Float32Array((rows + 1) * (columns + 1) * 3);
  const uvs = new Float32Array((rows + 1) * (columns + 1) * 2);
  let positionAt = 0;
  let uvAt = 0;
  for (let y = 0; y <= rows; y += 1) for (let x = 0; x <= columns; x += 1) {
    const point = panoramaDirectionForUv(x / columns, 1 - y / rows).multiplyScalar(PANO_R);
    positions.set(point.toArray(), positionAt); positionAt += 3;
    uvs.set([x / columns, 1 - y / rows], uvAt); uvAt += 2;
  }
  const indices = [];
  for (let y = 0; y < rows; y += 1) for (let x = 0; x < columns; x += 1) {
    const a = y * (columns + 1) + x;
    const b = a + 1;
    const c = a + columns + 1;
    indices.push(a, c, b, b, c, c + 1);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
  geometry.setIndex(indices);
  const material = new THREE.MeshBasicMaterial({ color: 0, side: THREE.DoubleSide, toneMapped: false });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.quaternion.setFromEuler(poseEuler(panorama));
  loads.push(loadTexture(panorama.url, material, isCurrent, redraw));
  return mesh;
}

function pathLine(points, radius, color, opacity) {
  const positions = new Float32Array(points.length * 3);
  const point = new THREE.Vector3();
  points.forEach((sample, index) => {
    directionFromPitchYaw(sample.pitch, sample.yaw, point).multiplyScalar(radius);
    positions.set(point.toArray(), index * 3);
  });
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  return new THREE.Line(geometry, new THREE.LineBasicMaterial({
    color, transparent: opacity < 1, opacity, toneMapped: false,
  }));
}

function ghostLines(segments) {
  // One draw call, with explicitly disconnected pairs; outages never gain a
  // fictional joining line and thousands of gaps do not make thousands of meshes.
  const vertices=[];
  for(const segment of segments) for(let i=1;i<segment.length;i++) {
    for(const point of [segment[i-1],segment[i]]) vertices.push(...directionFromPitchYaw(point.pitch,point.yaw).multiplyScalar(REF_R-.2).toArray());
  }
  if(!vertices.length) return null;
  const geometry=new THREE.BufferGeometry();
  geometry.setAttribute('position',new THREE.Float32BufferAttribute(vertices,3));
  return new THREE.LineSegments(geometry,new THREE.LineBasicMaterial({color:0xffaa00,transparent:true,opacity:.9,toneMapped:false}));
}

function layoutRect(width, height, aspect) {
  let w = width;
  let h = w / aspect;
  if (h > height) { h = height; w = height * aspect; }
  return { x: Math.round((width - w) / 2), y: Math.round((height - h) / 2), w: Math.round(w), h: Math.round(h) };
}

export function createSpatialView(container, { onPick = () => {}, onError = () => {} } = {}) {
  if (!container?.appendChild) throw new Error('Spatial view needs a DOM container.');
  let renderer;
  try { renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: false, alpha: false }); }
  catch (error) { throw new Error(`WebGL unavailable: ${error.message}`); }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, DPR_CAP));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.setClearColor(0);
  container.style.position ||= 'relative';
  Object.assign(renderer.domElement.style, { display: 'block', width: '100%', height: '100%' });
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const frameLayer = 0;
  const overviewLayer = 1;
  const frameCamera = new THREE.PerspectiveCamera(70, 1, 0.05, 50);
  frameCamera.layers.set(frameLayer);
  const overviewCamera = new THREE.PerspectiveCamera(50, 1, 0.1, 200);
  overviewCamera.layers.set(overviewLayer);
  overviewCamera.position.set(0, 8, 26);
  const controls = new OrbitControls(overviewCamera, renderer.domElement);
  controls.enableDamping = false;
  controls.enabled = false;

  const wire = new THREE.Mesh(new THREE.SphereGeometry(REF_R, 24, 16), new THREE.MeshBasicMaterial({
    color: 0x334455, wireframe: true, transparent: true, opacity: 0.35, toneMapped: false,
  }));
  setLayerDeep(wire, overviewLayer);
  scene.add(wire);
  const groups = Object.fromEntries(['references', 'panorama', 'markers', 'waypoints', 'ghosts', 'ribbon']
    .map((name) => [name, new THREE.Group()]));
  Object.values(groups).forEach((group) => scene.add(group));
  [groups.references, groups.panorama, groups.markers].forEach((group) => setLayerDeep(group, frameLayer));
  [groups.waypoints, groups.ghosts, groups.ribbon].forEach((group) => setLayerDeep(group, overviewLayer));
  const frustumGeometry = new THREE.BufferGeometry();
  frustumGeometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(24), 3));
  const frustum = new THREE.LineSegments(frustumGeometry, new THREE.LineBasicMaterial({ color: 0x00ffff }));
  setLayerDeep(frustum, overviewLayer);
  scene.add(frustum);

  let data = cleanSpatialData({});
  let pose = { pitch: 0, yaw: 0 };
  let comparePose = null;
  let mode = 'frame';
  let active = true;
  let intersecting = true;
  let documentVisible = document.visibilityState !== 'hidden';
  let contextLost = false;
  let disposed = false;
  let epoch = 0;
  let readyState = createDeferred();
  let lastWidth = 0;
  let lastHeight = 0;
  const canRender = () => !disposed && !contextLost && active && intersecting && documentVisible;

  function updateFrustum() {
    const horizontal = rad(data.hfov) / 2;
    const vertical = rad(hfovToVfovDeg(data.hfov, data.aspect)) / 2;
    const values = frustumGeometry.attributes.position.array;
    let at = 0;
    for (const [x, y] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
      const point = new THREE.Vector3(x * Math.tan(horizontal), y * Math.tan(vertical), -1)
        .normalize().applyEuler(poseEuler(pose)).multiplyScalar(REF_R);
      values.set([0, 0, 0, point.x, point.y, point.z], at);
      at += 6;
    }
    frustumGeometry.attributes.position.needsUpdate = true;
  }

  function rebuild() {
    Object.values(groups).forEach(disposeGroupChildren);
    readyState.reject(new Error('Spatial image load became stale.'));
    readyState = createDeferred();
    const ownedEpoch = ++epoch;
    const isCurrent = () => !disposed && !contextLost && ownedEpoch === epoch;
    const loads = [];
    data.references.forEach((reference) => {
      const mesh = meshForReference(reference, data, isCurrent, render, loads);
      setLayerDeep(mesh, frameLayer);
      groups.references.add(mesh);
    });
    if (data.panorama) {
      const mesh = meshForPanorama(data.panorama, isCurrent, render, loads);
      setLayerDeep(mesh, frameLayer);
      groups.panorama.add(mesh);
    }
    const sphere = (point, radius, color, layer) => {
      const mesh = new THREE.Mesh(new THREE.SphereGeometry(radius, 10, 8), new THREE.MeshBasicMaterial({ color }));
      directionFromPitchYaw(point.pitch, point.yaw, mesh.position).multiplyScalar(REF_R - radius);
      setLayerDeep(mesh, layer);
      return mesh;
    };
    data.markers.forEach((marker) => groups.markers.add(sphere(
      marker,
      0.18,
      markerColor(marker.kind),
      frameLayer,
    )));
    data.waypoints.forEach((waypoint) => groups.waypoints.add(sphere(waypoint, 0.22, 0xffee55, overviewLayer)));
    const ghost=ghostLines(data.ghostSegments);
    if(ghost){setLayerDeep(ghost,overviewLayer);groups.ghosts.add(ghost);}
    if (data.samples.length > 1) {
      const path = pathLine(data.samples, REF_R - 0.1, 0x00ffff, 0.8);
      setLayerDeep(path, overviewLayer);
      groups.ribbon.add(path);
    }
    updateFrustum();
    Promise.all(loads).then(() => { if (isCurrent()) readyState.resolve(); }, (error) => {
      if (isCurrent()) {readyState.reject(error); onError(error.message);}
    });
  }

  function renderFrame(x, y, width, height) {
    if (width < 1 || height < 1) return;
    renderer.setViewport(x, y, width, height);
    renderer.setScissor(x, y, width, height);
    frameCamera.aspect = width / height;
    frameCamera.fov = hfovToVfovDeg(data.hfov, data.aspect);
    frameCamera.updateProjectionMatrix();
    renderer.render(scene, frameCamera);
  }

  function render() {
    if (!canRender()) return;
    const width = container.clientWidth || 1;
    const height = container.clientHeight || 1;
    if (width !== lastWidth || height !== lastHeight) {
      renderer.setSize(width, height, false);
      lastWidth = width;
      lastHeight = height;
    }
    renderer.setScissorTest(true);
    try {
      renderer.setViewport(0, 0, width, height);
      renderer.setScissor(0, 0, width, height);
      renderer.clear();
      if (mode === 'overview') {
        controls.enabled = true;
        overviewCamera.aspect = width / height;
        overviewCamera.updateProjectionMatrix();
        renderer.render(scene, overviewCamera);
      } else {
        controls.enabled = false;
        const leftWidth = mode === 'compare' ? Math.floor(width / 2) : width;
        const left = layoutRect(leftWidth, height, data.aspect);
        frameCamera.quaternion.setFromEuler(poseEuler(pose));
        renderFrame(left.x, left.y, left.w, left.h);
        if (mode === 'compare') {
          const right = layoutRect(width - leftWidth, height, data.aspect);
          frameCamera.quaternion.setFromEuler(poseEuler(comparePose || pose));
          renderFrame(leftWidth + right.x, right.y, right.w, right.h);
        }
      }
    } finally {
      renderer.setScissorTest(false);
    }
  }

  function controlsChanged() { render(); }
  controls.addEventListener('change', controlsChanged);
  let down = null;
  function clearPointer(event) {
    if (down?.id === event.pointerId && renderer.domElement.hasPointerCapture?.(event.pointerId)) {
      renderer.domElement.releasePointerCapture(event.pointerId);
    }
    down = null;
  }
  function pointerDown(event) {
    if (mode !== 'overview') return;
    down = { id: event.pointerId, x: event.clientX, y: event.clientY };
    renderer.domElement.setPointerCapture?.(event.pointerId);
  }
  function pointerUp(event) {
    const start = down;
    clearPointer(event);
    if (!start || Math.hypot(event.clientX - start.x, event.clientY - start.y) > 6 || !canRender()) return;
    const box = renderer.domElement.getBoundingClientRect();
    if (!box.width || !box.height) return;
    const raycaster = new THREE.Raycaster();
    raycaster.layers.set(overviewLayer);
    raycaster.setFromCamera(new THREE.Vector2((event.clientX - box.left) / box.width * 2 - 1, -(event.clientY - box.top) / box.height * 2 + 1), overviewCamera);
    const hit = raycaster.intersectObject(wire, false)[0];
    if (hit) onPick(pitchYawFromDirection(hit.point.normalize(), pose));
  }
  function pointerOut(event) {
    if (!event.relatedTarget) clearPointer(event);
  }
  renderer.domElement.addEventListener('pointerdown', pointerDown);
  renderer.domElement.addEventListener('pointerup', pointerUp);
  renderer.domElement.addEventListener('pointercancel', clearPointer);
  renderer.domElement.addEventListener('pointerout', pointerOut);
  function onLost(event) { event.preventDefault(); contextLost = true; readyState.reject(new Error('WebGL context lost.')); onError('WebGL context lost. Please reload the spatial view.'); }
  function onRestored() { onError('WebGL context restored. Please re-open the spatial view to resume.'); }
  renderer.domElement.addEventListener('webglcontextlost', onLost, false);
  renderer.domElement.addEventListener('webglcontextrestored', onRestored, false);
  const resizeObserver = new ResizeObserver(render);
  resizeObserver.observe(container);
  const intersectionObserver = new IntersectionObserver((entries) => { intersecting = entries.some((entry) => entry.isIntersecting); if (intersecting) render(); });
  intersectionObserver.observe(container);
  function visibilityChanged() { documentVisible = document.visibilityState !== 'hidden'; if (documentVisible) render(); }
  document.addEventListener('visibilitychange', visibilityChanged);

  rebuild();
  return {
    setData(input) {
      const cleaned = cleanSpatialData(input);
      if (!cleaned || dataFailure(input) !== 'Invalid spatial data.') {
        readyState.reject(new Error(dataFailure(input)));
        readyState = createDeferred();
        readyState.reject(new Error(dataFailure(input)));
        onError(dataFailure(input));
        return;
      }
      data = cleaned;
      rebuild();
      render();
    },
    setPose(next, comparison = null) {
      if (!isValidSpatialPose(next) || (comparison !== null && !isValidSpatialPose(comparison))) { onError('Invalid spatial pose ignored.'); return; }
      pose = { pitch: next.pitch, yaw: next.yaw };
      comparePose = comparison && { pitch: comparison.pitch, yaw: comparison.yaw };
      updateFrustum();
      render();
    },
    setMode(next) {
      if (!['frame', 'overview', 'compare'].includes(next)) { onError('Invalid spatial mode ignored.'); return; }
      mode = next;
      render();
    },
    setActive(next) { active = Boolean(next); if (active) render(); },
    ready() { return readyState.promise; },
    capture() {
      if (!canRender()) throw new Error('Capture unavailable while spatial view is inactive, hidden, lost, or disposed.');
      render();
      return renderer.domElement.toDataURL('image/png');
    },
    stats() { return { render: { ...renderer.info.render }, memory: { ...renderer.info.memory } }; },
    dispose() {
      if (disposed) return;
      disposed = true;
      epoch += 1;
      readyState.reject(new Error('Spatial view disposed.'));
      resizeObserver.disconnect();
      intersectionObserver.disconnect();
      document.removeEventListener('visibilitychange', visibilityChanged);
      controls.removeEventListener('change', controlsChanged);
      controls.dispose();
      renderer.domElement.removeEventListener('pointerdown', pointerDown);
      renderer.domElement.removeEventListener('pointerup', pointerUp);
      renderer.domElement.removeEventListener('pointercancel', clearPointer);
      renderer.domElement.removeEventListener('pointerout', pointerOut);
      renderer.domElement.removeEventListener('webglcontextlost', onLost);
      renderer.domElement.removeEventListener('webglcontextrestored', onRestored);
      Object.values(groups).forEach(disposeGroupChildren);
      disposeObject(wire);
      frustumGeometry.dispose();
      frustum.material.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      if (renderer.domElement.parentNode === container) container.removeChild(renderer.domElement);
    },
  };
}

if (typeof window !== 'undefined') window.createSpatialView = createSpatialView;
