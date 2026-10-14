import { createIcons, icons, mountScenes } from './vendor.js';
import { mountAppearance } from './studio-ui.js';
import { onLocaleChange, translate } from './locale.mjs';

function paint(root) {
  createIcons({ icons, root, attrs: { 'aria-hidden': 'true', 'stroke-width': 1.7 } });
}

function mountPasswordTools(doc) {
  const password = doc.getElementById('password');
  const toggle = doc.getElementById('password-toggle');
  const caps = doc.getElementById('auth-caps-lock');
  if (!password) return () => {};
  const removers = [];
  const on = (target, type, handler) => {
    target.addEventListener(type, handler);
    removers.push(() => target.removeEventListener(type, handler));
  };
  const updateCaps = event => {
    if (!caps || typeof event.getModifierState !== 'function') return;
    caps.hidden = !event.getModifierState('CapsLock');
  };
  if (toggle) {
    const syncLabel = () => {
      const label = translate(password.type === 'password' ? '显示密码' : '隐藏密码');
      toggle.setAttribute('aria-label', label);
      toggle.title = label;
    };
    removers.push(onLocaleChange(syncLabel));
    on(toggle, 'click', () => {
      const visible = password.type === 'password';
      password.type = visible ? 'text' : 'password';
      toggle.setAttribute('aria-pressed', String(visible));
      syncLabel();
      toggle.innerHTML = `<i data-lucide="${visible ? 'eye-off' : 'eye'}"></i>`;
      paint(toggle);
      password.focus();
    });
  }
  on(password, 'keydown', updateCaps);
  on(password, 'keyup', updateCaps);
  on(password, 'blur', () => { if (caps) caps.hidden = true; });
  return () => removers.forEach(remove => remove());
}

function mountGridHover(doc, appearance) {
  const root = doc.documentElement;
  const target = doc.body;
  const cell = doc.getElementById('login-grid-hover');
  if (!cell || !target) return () => {};
  const coarse = doc.defaultView.matchMedia?.('(pointer: coarse)');
  const hide = () => { cell.hidden = true; };
  const move = event => {
    if (!appearance.motion() || root.dataset.grid === 'hidden' || coarse?.matches) {
      hide();
      return;
    }
    const column = Math.floor(event.clientX / 64);
    const row = Math.floor(event.clientY / 64);
    cell.style.transform = `translate(${column * 64 + 1}px, ${row * 64 + 1}px)`;
    cell.hidden = false;
  };
  target.addEventListener('pointermove', move);
  target.addEventListener('pointerleave', hide);
  return () => {
    target.removeEventListener('pointermove', move);
    target.removeEventListener('pointerleave', hide);
  };
}

function mount(doc) {
  const appearance = mountAppearance(doc);
  const disposeScenes = mountScenes(doc);
  const removers = [mountPasswordTools(doc), mountGridHover(doc, appearance)];
  paint(doc.body);
  doc.defaultView.addEventListener('pagehide', () => {
    removers.forEach(remove => remove());
    appearance.dispose();
    disposeScenes?.();
  }, { once: true });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => mount(document), { once: true });
} else {
  mount(document);
}