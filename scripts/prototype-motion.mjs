import * as THREE from 'three';

export function mountScenes(doc) {
  const view = doc.defaultView;
  const hosts = [...doc.querySelectorAll('[data-scene]')];
  if (!view.WebGL2RenderingContext || !hosts.length) return () => {};
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
  } catch {
    return () => {};
  }
  renderer.setPixelRatio(Math.min(view.devicePixelRatio || 1, 1.75));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.domElement.setAttribute('aria-hidden', 'true');
  const camera = new THREE.OrthographicCamera(-5, 5, 5, -5, .1, 60);
  camera.position.set(8, 10, 11);
  camera.lookAt(0, 0, 0);
  const scenes = new Map();
  const motionQuery = view.matchMedia('(prefers-reduced-motion: reduce)');
  const pointer = new THREE.Vector2();
  const raycaster = new THREE.Raycaster();
  let active = null;
  let frame = 0;
  let disposed = false;
  let onScreen = true;
  let lost = false;
  let lastTime = 0;
  let elapsed = 0;
  let hovered = -1;
  const motion = () => !motionQuery.matches && doc.documentElement.dataset.motion !== 'reduced';
  const palette = () => {
    const styles = view.getComputedStyle(doc.documentElement);
    return Object.fromEntries(['surface', 'text', 'border', 'border-strong', 'surface-soft', 'text-muted', 'scene-accent', 'scene-secondary'].map(name => [name, styles.getPropertyValue(`--cp-${name}`).trim()]));
  };
  function createScene(kind) {
    const scene = new THREE.Scene();
    const world = new THREE.Group();
    scene.add(world);
    const materials = [];
    const geometries = [];
    const material = (token, opacity = 1) => {
      const result = new THREE.MeshBasicMaterial({ transparent: opacity < 1, opacity, side: THREE.DoubleSide });
      result.userData.token = token; materials.push(result); return result;
    };
    const tones = { paper: material('surface'), ink: material('text'), silver: material('border-strong'), pale: material('surface-soft'), gray: material('text-muted'), accent: material('scene-accent'), secondary: material('scene-secondary') };
    const edgeMaterial = new THREE.LineBasicMaterial();
    edgeMaterial.userData.token = 'text'; materials.push(edgeMaterial);
    const gridMaterial = new THREE.LineBasicMaterial({ transparent: true, opacity: .22 });
    gridMaterial.userData.token = 'border-strong'; materials.push(gridMaterial);
    function box(parent, size, position, face = tones.paper, outline = true) {
      const geometry = new THREE.BoxGeometry(...size); geometries.push(geometry);
      const mesh = new THREE.Mesh(geometry, face); mesh.position.set(...position); parent.add(mesh);
      if (outline) { const edges = new THREE.EdgesGeometry(geometry); geometries.push(edges); mesh.add(new THREE.LineSegments(edges, edgeMaterial)); }
      return mesh;
    }
    function line(parent, from, to, mat = gridMaterial) {
      const geometry = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(...from), new THREE.Vector3(...to)]); geometries.push(geometry);
      parent.add(new THREE.Line(geometry, mat));
    }
    const board = new THREE.Group(); world.add(board);
    const tiles = [];
    let lens = null;
    let lensTarget = null;
    let lensCamera = null;
    if (kind === 'magnifier') {
      box(board, [6.1, .22, 4.2], [0, -.12, 0], [tones.silver, tones.silver, tones.paper, tones.silver, tones.silver, tones.silver]);
      for (let index = -2; index <= 2; index += 1) line(board, [-2.9, .005, index * .75], [2.9, .005, index * .75]);
      for (let index = -3; index <= 3; index += 1) line(board, [index * .8, .005, -1.95], [index * .8, .005, 1.95]);
      const heights = [.5, .72, .38, .85, .56, .92, .44, .7, .36, .61];
      heights.forEach((height, index) => {
        const positionX = -2.55 + index * .56;
        const positionZ = Math.sin(index * 1.1) * .6;
        box(board, [.25, .025, height], [positionX, .025, positionZ], index % 3 === 0 ? tones.secondary : tones.ink, false);
        line(board, [positionX, .04, positionZ - height * .85], [positionX, .04, positionZ + height * .85], edgeMaterial);
      });
      lens = new THREE.Group(); world.add(lens);
      const rim = new THREE.TorusGeometry(1.1, .09, 8, 80); geometries.push(rim);
      const ring = new THREE.Mesh(rim, tones.accent); ring.rotation.x = -Math.PI / 2; lens.add(ring);
      const lowerRing = new THREE.Mesh(rim, tones.secondary); lowerRing.rotation.x = -Math.PI / 2; lowerRing.position.y = -.13; lens.add(lowerRing);
      const tube = new THREE.CylinderGeometry(.11, .11, 1.8, 16); geometries.push(tube);
      const handle = new THREE.Mesh(tube, tones.paper);
      const direction = new THREE.Vector3(-.8, 0, .65).normalize(); handle.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction); handle.position.copy(direction.multiplyScalar(1.9)); lens.add(handle);
      const capGeometry = new THREE.SphereGeometry(.115, 16, 8); geometries.push(capGeometry);
      const cap = new THREE.Mesh(capGeometry, tones.ink); cap.position.copy(handle.position).multiplyScalar(1.46); lens.add(cap);
      lensTarget = new THREE.WebGLRenderTarget(384, 384, { minFilter: THREE.LinearFilter, magFilter: THREE.LinearFilter });
      const glassGeometry = new THREE.CircleGeometry(1.035, 80); geometries.push(glassGeometry);
      const glassMaterial = new THREE.MeshBasicMaterial({ map: lensTarget.texture, side: THREE.DoubleSide }); materials.push(glassMaterial);
      const glass = new THREE.Mesh(glassGeometry, glassMaterial); glass.rotation.x = -Math.PI / 2; glass.position.y = .015; lens.add(glass);
      lensCamera = new THREE.OrthographicCamera(-.78, .78, .78, -.78, .1, 30); lensCamera.up.set(0, 0, -1);
    } else {
      const shape = new THREE.Shape(); const radius = .55; const half = 2.65;
      shape.moveTo(-half + radius, -half); shape.lineTo(half - radius, -half); shape.quadraticCurveTo(half, -half, half, -half + radius); shape.lineTo(half, half - radius); shape.quadraticCurveTo(half, half, half - radius, half); shape.lineTo(-half + radius, half); shape.quadraticCurveTo(-half, half, -half, half - radius); shape.lineTo(-half, -half + radius); shape.quadraticCurveTo(-half, -half, -half + radius, -half);
      const baseGeometry = new THREE.ExtrudeGeometry(shape, { depth: .25, bevelEnabled: false, curveSegments: 12 }); baseGeometry.rotateX(-Math.PI / 2); geometries.push(baseGeometry);
      const base = new THREE.Mesh(baseGeometry, tones.ink); base.position.y = -.48; board.add(base);
      const baseEdges = new THREE.EdgesGeometry(baseGeometry, 20); geometries.push(baseEdges);
      const lightEdges = new THREE.LineBasicMaterial(); lightEdges.userData.token = 'surface'; materials.push(lightEdges); base.add(new THREE.LineSegments(baseEdges, lightEdges));
      for (let index = -2; index <= 2; index += 1) {
        line(board, [-2.5, -.219, index], [2.5, -.219, index], lightEdges);
        line(board, [index, -.219, -2.5], [index, -.219, 2.5], lightEdges);
      }
      const faces = [tones.accent, tones.paper, tones.secondary, tones.gray];
      [[-1.03, -1.03], [1.03, -1.03], [-1.03, 1.03], [1.03, 1.03]].forEach(([positionX, positionZ], index) => {
        const tile = box(world, [1.95, .25, 1.95], [positionX, .15, positionZ], [tones.silver, tones.silver, faces[index], tones.silver, tones.gray, tones.gray]);
        tile.userData.home = [positionX, positionZ]; tiles.push(tile);
      });
      for (let index = -5; index <= 5; index += 1) {
        line(world, [-6, -.55, index], [6, -.55, index]); line(world, [index, -.55, -5], [index, -.55, 5]);
      }
    }
    return { scene, world, tiles, lens, lensTarget, lensCamera, materials, geometries };
  }
  function repaint() {
    const colors = palette();
    for (const state of scenes.values()) for (const mat of state.materials) {
      if (mat.userData.token) mat.color.set(colors[mat.userData.token] || '#777777');
    }
  }
  function resize() {
    if (!active || disposed || lost) return;
    const bounds = active.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return;
    renderer.setSize(bounds.width, bounds.height, false);
    const aspect = bounds.width / bounds.height;
    const halfHeight = Math.max(3.7, 4.5 / aspect);
    camera.left = -halfHeight * aspect; camera.right = halfHeight * aspect;
    camera.top = halfHeight; camera.bottom = -halfHeight; camera.updateProjectionMatrix();
    draw(false);
  }
  function draw(animated) {
    if (!active || lost || disposed) return;
    const state = scenes.get(active.dataset.scene);
    const time = animated ? elapsed : 1.6;
    state.world.rotation.y = animated ? pointer.x * .065 : 0;
    state.world.rotation.x = animated ? pointer.y * .035 : 0;
    if (state.lens) {
      state.lens.position.set(Math.sin(time * .62) * 1.3, 1.55 + Math.sin(time * .8) * .12, Math.cos(time * .62) * .45);
      state.lensCamera.position.set(state.lens.position.x, 10, state.lens.position.z);
      state.lensCamera.lookAt(state.lens.position.x, 0, state.lens.position.z);
      state.lens.visible = false;
      renderer.setRenderTarget(state.lensTarget); renderer.setClearColor(palette().surface, 1); renderer.render(state.scene, state.lensCamera);
      state.lens.visible = true; renderer.setRenderTarget(null);
    }
    state.tiles.forEach((tile, index) => {
      const wave = Math.max(0, Math.sin(time * .85 - index * .8));
      const spread = wave * .1;
      tile.position.set(tile.userData.home[0] * (1 + spread), .14 + wave * .5 + (animated && hovered === index ? .32 : 0), tile.userData.home[1] * (1 + spread));
    });
    renderer.setClearColor(0, 0); renderer.render(state.scene, camera);
  }
  function stop() { view.cancelAnimationFrame(frame); frame = 0; lastTime = 0; }
  function loop(stamp) {
    frame = 0;
    if (disposed || lost || !active || !onScreen || doc.hidden || !motion()) return;
    if (lastTime) elapsed += Math.min((stamp - lastTime) / 1000, .05);
    lastTime = stamp; draw(true); frame = view.requestAnimationFrame(loop);
  }
  function schedule() {
    stop();
    if (active && !lost && !disposed && !doc.hidden && onScreen) {
      draw(motion()); if (motion()) frame = view.requestAnimationFrame(loop);
    }
  }
  const intersection = view.IntersectionObserver ? new view.IntersectionObserver(entries => {
    const entry = entries.find(item => item.target === active);
    if (entry) { onScreen = entry.isIntersecting; schedule(); }
  }) : null;
  function sync() {
    if (disposed || lost) return;
    const target = doc.documentElement.dataset.screen === 'login' ? hosts.find(host => host.dataset.scene === 'magnifier') : doc.documentElement.dataset.home === 'true' ? hosts.find(host => host.dataset.scene === 'platform') : null;
    if (target !== active) {
      if (active) { active.dataset.ready = 'false'; intersection?.unobserve(active); }
      active = target; onScreen = true; pointer.set(0, 0); hovered = -1;
      if (active) {
        if (!scenes.has(active.dataset.scene)) scenes.set(active.dataset.scene, createScene(active.dataset.scene));
        active.append(renderer.domElement); active.dataset.ready = 'true'; intersection?.observe(active);
      } else renderer.domElement.remove();
    }
    repaint(); resize(); schedule();
  }
  function move(event) {
    if (!active || !motion() || event.pointerType === 'touch') return;
    const bounds = active.getBoundingClientRect();
    pointer.set(Math.max(-1, Math.min(1, (event.clientX - bounds.left) / bounds.width * 2 - 1)), Math.max(-1, Math.min(1, 1 - (event.clientY - bounds.top) / bounds.height * 2)));
    const state = scenes.get(active.dataset.scene);
    raycaster.setFromCamera(pointer, camera);
    const match = raycaster.intersectObjects(state.tiles, false)[0]; hovered = match ? state.tiles.indexOf(match.object) : -1;
  }
  function leave() { pointer.set(0, 0); hovered = -1; }
  hosts.forEach(host => { host.addEventListener('pointermove', move); host.addEventListener('pointerleave', leave); });
  const observer = new view.MutationObserver(sync);
  observer.observe(doc.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-palette', 'data-motion', 'data-screen', 'data-home', 'data-auth'] });
  const resizeObserver = view.ResizeObserver ? new view.ResizeObserver(resize) : null;
  hosts.forEach(host => resizeObserver?.observe(host));
  doc.addEventListener('visibilitychange', schedule);
  motionQuery.addEventListener('change', schedule);
  const contextLost = event => { event.preventDefault(); lost = true; stop(); if (active) active.dataset.ready = 'false'; };
  const contextRestored = () => { lost = false; if (active) active.dataset.ready = 'true'; sync(); };
  renderer.domElement.addEventListener('webglcontextlost', contextLost);
  renderer.domElement.addEventListener('webglcontextrestored', contextRestored);
  function dispose() {
    if (disposed) return;
    disposed = true; stop(); observer.disconnect(); resizeObserver?.disconnect(); intersection?.disconnect();
    view.removeEventListener('pagehide', hidePage); view.removeEventListener('pageshow', showPage);
    doc.removeEventListener('visibilitychange', schedule); motionQuery.removeEventListener('change', schedule);
    hosts.forEach(host => { host.removeEventListener('pointermove', move); host.removeEventListener('pointerleave', leave); host.dataset.ready = 'false'; });
    for (const state of scenes.values()) { state.geometries.forEach(geometry => geometry.dispose()); state.materials.forEach(mat => mat.dispose()); state.lensTarget?.dispose(); }
    renderer.domElement.removeEventListener('webglcontextlost', contextLost); renderer.domElement.removeEventListener('webglcontextrestored', contextRestored);
    renderer.dispose(); renderer.domElement.remove();
  }
  function hidePage(event) { if (event.persisted) stop(); else dispose(); }
  function showPage(event) { if (event.persisted && !disposed) sync(); }
  view.addEventListener('pagehide', hidePage); view.addEventListener('pageshow', showPage); sync();
  return dispose;
}