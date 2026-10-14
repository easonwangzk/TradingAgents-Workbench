import { mountLocale } from './locale.mjs';

const mount = () => { document.__interfaceLocale ||= mountLocale(document); };
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount, { once: true });
else mount();