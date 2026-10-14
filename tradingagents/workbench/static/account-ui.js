import { bindLocale, locale, onLocaleChange, translate } from './locale.mjs';

function node(doc, tag, text, className) {
  const element = doc.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function clearInputs(...inputs) {
  for (const input of inputs) {
    if (input) input.value = '';
  }
}

async function readError(response, fallback) {
  return {
    400: '请求内容无效，请检查后重试。',
    401: '登录状态已失效，请重新登录。',
    403: '当前操作不被允许。',
    409: '当前状态已变化，请刷新后重试。',
    429: '请求过于频繁，请稍后重试。',
    422: '输入未通过校验，请检查后重试。',
    503: '服务暂不可用，请稍后再试。',
  }[response.status] || fallback;
}

function retryAfterSeconds(response, fallback = 60) {
  const header = response.headers?.get?.('Retry-After');
  const parsed = Number.parseInt(header || '', 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function mountRegisterPage(doc) {
  const codeForm = doc.getElementById('email-register-code-form');
  const form = doc.getElementById('email-register-form');
  if (!codeForm || !form) return;
  const email = doc.getElementById('email');
  const code = doc.getElementById('code');
  const password = doc.getElementById('password');
  const confirm = doc.getElementById('password-confirm');
  const send = doc.getElementById('register-send-code');
  const submit = doc.getElementById('email-register-submit');
  const message = doc.getElementById('auth-message');
  const status = doc.getElementById('register-status');
  const unavailable = doc.querySelector('[data-registration-available="false"]');
  let sending = false;
  let registering = false;
  let disposed = false;
  let cooldownTimer = null;
  let cooldownRemaining = 0;

  const showMessage = value => {
    if (!message) return;
    message.dataset.sourceMessage = value || '';
    message.textContent = translate(value) || '';
    message.hidden = !value;
  };
  const showStatus = value => {
    if (status) { status.dataset.sourceMessage = value || ''; status.textContent = translate(value) || ''; }
  };
  const sendLabel = () => {
    if (!send) return;
    const label = cooldownTimer === null ? translate('发送验证码') : locale() === 'zh-CN' ? `${cooldownRemaining + 1}s 后重发` : `${cooldownRemaining + 1}s ${translate('后重发')}`;
    const span = send.querySelector('span');
    if (span) span.textContent = label;
    else send.textContent = label;
  };
  const offLocale = onLocaleChange(() => { showMessage(message?.dataset.sourceMessage); showStatus(status?.dataset.sourceMessage); sendLabel(); });
  const setSendDisabled = disabled => {
    if (send) send.disabled = disabled || sending || registering || Boolean(unavailable);
  };
  const setRegisterDisabled = disabled => {
    if (submit) submit.disabled = disabled || sending || registering || Boolean(unavailable);
  };
  const stopCooldown = () => {
    if (cooldownTimer !== null) doc.defaultView.clearTimeout(cooldownTimer);
    cooldownTimer = null;
    cooldownRemaining = 0;
  };
  const tickCooldown = () => {
    if (disposed || cooldownRemaining <= 0) {
      stopCooldown();
      setSendDisabled(false);
      sendLabel();
      return;
    }
    setSendDisabled(true);
    if (send) {
      const label = locale() === 'zh-CN' ? `${cooldownRemaining}s 后重发` : `${cooldownRemaining}s ${translate('后重发')}`;
      const span = send.querySelector('span');
      if (span) span.textContent = label;
      else send.textContent = label;
    }
    cooldownRemaining -= 1;
    cooldownTimer = doc.defaultView.setTimeout(tickCooldown, 1000);
  };
  const startCooldown = seconds => {
    stopCooldown();
    cooldownRemaining = Math.max(1, Math.min(600, Number.parseInt(seconds, 10) || 60));
    tickCooldown();
  };
  const validEmail = () => {
    const value = email?.value.trim() || '';
    if (!value || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) throw new Error('请输入有效邮箱地址。');
    return value;
  };
  const validRegistration = () => {
    const emailValue = validEmail();
    const codeValue = code?.value.trim() || '';
    const passwordValue = password?.value || '';
    if (!/^\d{6}$/.test(codeValue)) throw new Error('请输入 6 位验证码。');
    if (passwordValue.length < 12 || passwordValue.length > 128) throw new Error('密码长度需为 12-128 个字符。');
    if (passwordValue !== (confirm?.value || '')) throw new Error('两次输入的密码不一致。');
    return { email: emailValue, code: codeValue, password: passwordValue };
  };
  const dispose = () => {
    offLocale();
    disposed = true;
    sending = false;
    registering = false;
    stopCooldown();
    clearInputs(password, confirm, code);
    showMessage('');
    setSendDisabled(true);
    setRegisterDisabled(true);
  };

  if (unavailable) {
    setSendDisabled(true);
    setRegisterDisabled(true);
  }

  codeForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (sending || registering || disposed || unavailable || cooldownTimer !== null) return;
    let emailValue;
    try {
      emailValue = validEmail();
    } catch (error) {
      showMessage(error.message);
      return;
    }
    sending = true;
    email.disabled = true;
    setSendDisabled(true);
    setRegisterDisabled(true);
    clearInputs(code);
    showMessage('');
    try {
      const response = await doc.defaultView.fetch('/auth/register/code', {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        body: JSON.stringify({ email: emailValue }),
      });
      if (disposed) return;
      if (!response.ok) {
        if (response.status === 429) startCooldown(retryAfterSeconds(response));
        throw new Error(await readError(response, `请求失败 (HTTP ${response.status})。`));
      }
      const result = await response.json();
      if (disposed) return;
      showStatus('如该邮箱可注册，验证码将发送至邮箱，请查收。');
      startCooldown(result.retry_after || 60);
    } catch (error) {
      if (disposed) return;
      showMessage(error.message || '验证码发送失败，请稍后重试。');
      if (cooldownTimer === null) setSendDisabled(false);
    } finally {
      sending = false;
      if (!disposed) {
        email.disabled = false;
        setSendDisabled(cooldownTimer !== null);
        setRegisterDisabled(false);
      }
    }
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (registering || sending || disposed || unavailable) return;
    let payload;
    try {
      payload = validRegistration();
    } catch (error) {
      showMessage(error.message);
      return;
    }
    registering = true;
    email.disabled = true;
    setSendDisabled(true);
    setRegisterDisabled(true);
    showMessage('');
    try {
      const response = await doc.defaultView.fetch('/auth/register', {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        body: JSON.stringify(payload),
      });
      if (disposed) return;
      if (!response.ok) {
        const detail = response.status === 401
          ? '验证码错误或已失效，请检查或重新获取。'
          : await readError(response, `请求失败 (HTTP ${response.status})。`);
        throw new Error(detail);
      }
      clearInputs(password, confirm, code);
      dispose();
      doc.defaultView.location.assign('/');
    } catch (error) {
      if (disposed) return;
      clearInputs(password, confirm, code);
      showMessage(error.message || '注册失败，请稍后重试。');
    } finally {
      registering = false;
      if (!disposed) {
        email.disabled = false;
        setRegisterDisabled(false);
        setSendDisabled(cooldownTimer !== null);
      }
    }
  });

  email.addEventListener('input', () => {
    clearInputs(code);
    showStatus('');
  });
  doc.defaultView.addEventListener('pagehide', dispose, { once: true });
  setSendDisabled(false);
  setRegisterDisabled(false);
}

export function mountAuthPage(doc) {
  if (!doc.querySelector('#email-login-form, #email-register-form, #email-redeem-form, #email-reset-form')) return;
  doc.defaultView.addEventListener('pageshow', event => {
    if (event.persisted) doc.defaultView.location.reload();
  });
  if (doc.defaultView.location.pathname === '/auth/register') {
    mountRegisterPage(doc);
    return;
  }
  const form = doc.querySelector('#email-login-form, #email-redeem-form, #email-reset-form');
  if (!form) return;
  const path = doc.defaultView.location.pathname;
  const email = doc.getElementById('email');
  const password = doc.getElementById('password');
  const submit = form.querySelector('button[type="submit"]');
  const message = doc.getElementById('auth-message');
  const status = doc.getElementById('auth-fragment-status');
  let secret = null;
  let pending = false;
  let disposed = false;

  const showMessage = value => {
    if (!message) return;
    message.dataset.sourceMessage = value || '';
    message.textContent = translate(value) || '';
    message.hidden = !value;
  };
  const showStatus = () => {
    if (!status || path === '/auth/login') return;
    status.textContent = secret ? `${translate(path === '/auth/redeem' ? '邀请链接已载入：' : '重置链接已载入：')}${secret.email}` : translate('链接无效或已失效，请重新获取。');
  };
  const offLocale = onLocaleChange(() => { showMessage(message?.dataset.sourceMessage); showStatus(); });
  const dispose = () => {
    offLocale();
    disposed = true;
    pending = false;
    secret = null;
    clearInputs(password);
    showMessage('');
    if (submit) submit.disabled = true;
  };

  if (path !== '/auth/login') {
    const params = new URLSearchParams(doc.defaultView.location.hash.slice(1));
    const token = params.get('token');
    const secretEmail = params.get('email');
    doc.defaultView.history.replaceState(null, '', doc.defaultView.location.pathname + doc.defaultView.location.search);
    if (token && secretEmail) {
      secret = { token, email: secretEmail };
      showStatus();
    } else {
      showStatus();
      if (submit) submit.disabled = true;
    }
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (pending || disposed) return;
    pending = true;
    showMessage('');
    if (submit) submit.disabled = true;
    try {
      const payload = path === '/auth/login'
        ? { email: email.value.trim(), password: password.value }
        : (() => {
          if (!secret) throw new Error('链接无效或已失效，请重新获取。');
          return { token: secret.token, email: secret.email, password: password.value };
        })();
      const response = await doc.defaultView.fetch(path, {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        body: JSON.stringify(payload),
      });
      if (disposed) return;
      if (!response.ok) throw new Error(await readError(response, `请求失败 (HTTP ${response.status})。`));
      clearInputs(password);
      dispose();
      doc.defaultView.location.assign('/');
    } catch (error) {
      if (disposed) return;
      clearInputs(password);
      showMessage(error.message || '请求失败，请稍后重试。');
    } finally {
      if (!disposed && submit) submit.disabled = false;
      pending = false;
    }
  });

  doc.defaultView.addEventListener('pagehide', dispose, { once: true });
  if (submit) submit.disabled = path !== '/auth/login' && !secret;
}

export function mountEmailAccount(doc, { request, onSession, onLogout }) {
  const accountName = node(doc, 'span', '', 'small');
  accountName.id = 'account-name';
  doc.querySelector('.topbar-end').prepend(accountName);
  doc.querySelector('.local-label').textContent = 'EMAIL';

  const launcher = node(doc, 'button', undefined, 'icon-button');
  launcher.id = 'account-panel-toggle';
  launcher.type = 'button';
  launcher.title = translate('账户与安全');
  launcher.setAttribute('aria-label', launcher.title);
  launcher.innerHTML = '<span class="account-avatar"><i data-lucide="circle-user-round"></i></span>';
  doc.querySelector('.topbar-end').append(launcher);

  const dialog = node(doc, 'dialog');
  dialog.id = 'account-dialog';
  dialog.className = 'resource-dialog account-dialog';
  dialog.innerHTML = `
    <header class="resource-heading"><h2 id="account-heading">账户与安全</h2><button id="account-close" type="button" class="icon-button" aria-label="关闭账户面板" title="关闭账户面板"><i data-lucide="x"></i></button></header>
    <p id="account-error" class="error-message" role="alert" hidden></p>
    <section class="resource-section account-summary">
      <p id="account-email" class="mono"></p>
      <p id="account-role" class="muted"></p>
    </section>
    <section class="resource-section budget-section">
      <div class="panel-heading"><h3>本月模型预算</h3><span id="budget-month" class="muted small"></span></div>
      <dl class="budget-grid">
        <div><dt>上限</dt><dd id="budget-limit">--</dd></div>
        <div><dt>保守扣减</dt><dd id="budget-spent">--</dd></div>
        <div><dt>预留</dt><dd id="budget-reserved">--</dd></div>
        <div><dt>可用</dt><dd id="budget-available">--</dd></div>
      </dl>
      <p id="budget-reset" class="muted small">--</p>
      <p class="muted small">仅本工作台托管身份模型调用，非 Azure 账单。按长上下文单价保守扣减，实际账单可能更低；不含 BYOK、数据源和基础设施费用。</p>
    </section>
    <section class="resource-section">
      <form id="password-form" class="resource-form" novalidate>
        <h3>修改密码</h3>
        <label>当前密码<input id="account-current-password" type="password" autocomplete="current-password" required></label>
        <label>新密码<input id="account-new-password" type="password" autocomplete="new-password" required></label>
        <div class="resource-actions"><button id="password-save" type="submit" class="primary-button"><i data-lucide="check"></i>更新密码</button></div>
      </form>
    </section>
    <section id="admin-section" class="resource-section" hidden>
      <form id="invite-form" class="resource-form" novalidate>
        <h3>邀请新用户</h3>
        <label>邮箱<input id="invite-email" type="email" autocomplete="email"></label>
        <div class="resource-actions"><button id="invite-send" type="submit" class="primary-button">生成邀请链接</button></div>
        <p id="invite-result" class="inline-note" role="status"></p>
      </form>
      <form id="origins-form" class="resource-form" novalidate>
        <h3>兼容接口白名单</h3>
        <label>每行一个 HTTPS Origin<textarea id="approved-origins" rows="5"></textarea></label>
        <div class="resource-actions"><button id="origins-save" type="submit"><i data-lucide="check"></i>保存白名单</button></div>
      </form>
      <div class="resource-section">
        <div class="panel-heading"><h3>用户</h3><span id="users-count" class="muted small">0</span></div>
        <div id="user-list" class="user-list"></div>
      </div>
    </section>
    <div class="resource-actions account-actions"><button id="account-sign-out" type="button" class="danger-button">退出登录</button></div>`;
  doc.body.append(dialog);
  dialog.dataset.localeRoot = '';
  bindLocale(doc, '#account-dialog');

  const byId = id => doc.getElementById(id);
  let session = null;
  let originVersion = null;
  let accountVersion = null;
  let busy = false;
  let disposed = false;
  let dialogGeneration = 0;
  let inviteTokenId = null;
  let budgetSnapshot = null;
  let adminUsers = [];
  let inviteResult = null;
  void import('./vendor.js').then(({ createIcons, icons }) => {
    if (disposed) return;
    createIcons({ icons, root: dialog, attrs: { 'aria-hidden': 'true', 'stroke-width': 1.6 } });
    createIcons({ icons, root: launcher, attrs: { 'aria-hidden': 'true', 'stroke-width': 1.6 } });
  }).catch(() => {});

  const showError = value => {
    byId('account-error').dataset.sourceMessage = value || '';
    byId('account-error').textContent = translate(value) || '';
    byId('account-error').hidden = !value;
  };
  const applyBudget = snapshot => {
    budgetSnapshot = snapshot;
    byId('budget-month').textContent = snapshot?.month || '--';
    byId('budget-limit').textContent = formatBudget(snapshot?.limit_microusd);
    byId('budget-spent').textContent = formatBudget(snapshot?.spent_microusd);
    byId('budget-reserved').textContent = formatBudget(snapshot?.reserved_microusd);
    byId('budget-available').textContent = formatBudget(snapshot?.available_microusd);
    const reset = new Date(snapshot?.resets_at || snapshot?.reset_at || '');
    byId('budget-reset').textContent = Number.isFinite(reset.getTime()) ? `${translate('UTC 重置：')}${locale() === 'zh-CN' ? reset.toISOString() : ` ${reset.toLocaleString(locale(), { timeZone: 'UTC', dateStyle: 'medium', timeStyle: 'short' })} UTC`}` : '--';
  };
  const setBusy = value => {
    busy = value;
    for (const element of dialog.querySelectorAll('button, input, textarea')) {
      if (element.id === 'account-close') continue;
      element.disabled = value;
    }
  };
  const formatBudget = value => typeof value === 'number' ? `$${(value / 1_000_000).toFixed(2)}` : '--';
  const dialogOpen = () => dialog.hasAttribute('open');
  const clearInviteResult = ({ preserveToken = false } = {}) => {
    if (!preserveToken) inviteTokenId = null;
    inviteResult = null;
    byId('invite-result').replaceChildren();
    byId('invite-result').textContent = '';
  };
  const clearDialogState = ({ releaseBusy = true } = {}) => {
    dialogGeneration += 1;
    adminUsers = [];
    clearInputs(byId('account-current-password'), byId('account-new-password'), byId('invite-email'));
    clearInviteResult();
    originVersion = null;
    accountVersion = null;
    byId('approved-origins').value = '';
    byId('users-count').textContent = '0';
    byId('user-list').replaceChildren();
    showError('');
    if (releaseBusy && !disposed) setBusy(false);
  };
  const stillCurrent = generation => !disposed && generation === dialogGeneration && dialogOpen();
  const renderInviteResult = ({ token_id, email, fragment }) => {
    inviteResult = { token_id, email, fragment };
    inviteTokenId = token_id || null;
    const container = byId('invite-result');
    container.replaceChildren();
    const absoluteUrl = new URL(fragment, doc.defaultView.location.origin).toString();
    const link = node(doc, 'a', email || absoluteUrl);
    link.href = absoluteUrl;
    const copy = node(doc, 'button', translate('复制链接'));
    copy.type = 'button';
    copy.addEventListener('click', async () => {
      try {
        await doc.defaultView.navigator.clipboard?.writeText?.(absoluteUrl);
      } catch {
        /* Clipboard may be unavailable in tests or restricted browsers. */
      }
    });
    container.append(link, copy);
    if (inviteTokenId) {
      const revoke = node(doc, 'button', translate('撤销邀请'), 'danger-button');
      revoke.type = 'button';
      revoke.addEventListener('click', async () => {
        if (!inviteTokenId || busy || disposed) return;
        const generation = dialogGeneration;
        setBusy(true);
        showError('');
        try {
          await request(`/api/admin/invitations/${inviteTokenId}`, { method: 'DELETE' });
          if (!stillCurrent(generation)) return;
          clearInviteResult();
        } catch (error) {
          if (stillCurrent(generation)) showError(error.message || '无法撤销邀请。');
        } finally {
          if (stillCurrent(generation)) setBusy(false);
        }
      });
      container.append(revoke);
    }
  };
  const setSession = identity => {
    session = identity;
    accountName.textContent = identity?.user?.name || identity?.user?.email || '';
    accountName.title = identity?.user?.email || '';
    byId('account-email').textContent = identity?.user?.email || '';
    byId('account-role').textContent = identity?.user ? translate(identity.user.is_admin ? '管理员 · 可使用托管身份' : '受邀用户 · 仅可使用 BYOK') : '';
    byId('admin-section').hidden = !identity?.user?.is_admin;
    byId('budget-reset').closest('.budget-section').hidden = !identity?.user?.is_admin;
  };
  const offLocale = onLocaleChange(() => {
    launcher.title = translate('账户与安全');
    launcher.setAttribute('aria-label', launcher.title);
    setSession(session);
    applyBudget(budgetSnapshot);
    byId('account-error').textContent = translate(byId('account-error').dataset.sourceMessage || '');
    if (dialogOpen()) renderAdminUsers(adminUsers);
    if (inviteResult) renderInviteResult(inviteResult);
  });

  async function loadAdmin(generation = dialogGeneration) {
    if (!session?.user?.is_admin) return;
    const [users, policy] = await Promise.all([
      request('/api/admin/accounts'),
      request('/api/admin/approved-origins'),
    ]);
    if (!stillCurrent(generation)) return;
    originVersion = policy.version;
    accountVersion = users.version;
    byId('approved-origins').value = (policy.approved_origins || []).join('\n');
    adminUsers = users.accounts || [];
    renderAdminUsers(adminUsers);
  }

  function renderAdminUsers(users) {
    byId('users-count').textContent = String(users.length);
    byId('user-list').replaceChildren(...users.map(user => {
      const row = node(doc, 'div', undefined, 'user-row');
      const info = node(doc, 'div');
      const strong = node(doc, 'strong', user.name || user.email);
      const meta = node(doc, 'span', `${user.email}${user.disabled ? ` · ${translate('已禁用')}` : ''}${user.is_admin ? ` · ${translate('管理员')}` : ''}`, 'muted small');
      info.append(strong, meta);
      row.append(info);
      if (!user.is_admin && !user.disabled) {
        const actions = node(doc, 'div', undefined, 'resource-actions');
        const recovery = node(doc, 'button', translate('重置链接'));
        recovery.type = 'button';
        recovery.addEventListener('click', async () => {
          const generation = dialogGeneration;
          setBusy(true);
          showError('');
          try {
            const result = await request('/api/admin/recoveries', { method: 'POST', body: { user_id: user.object_id } });
            if (!stillCurrent(generation)) return;
            renderInviteResult(result);
          } catch (error) {
            if (stillCurrent(generation)) showError(error.message || '无法生成重置链接。');
          } finally {
            if (stillCurrent(generation)) setBusy(false);
          }
        });
        const disable = node(doc, 'button', translate('禁用'), 'danger-button');
        disable.type = 'button';
        const actionGeneration = dialogGeneration;
        const revision = accountVersion;
        disable.addEventListener('click', async () => {
          if (busy || !stillCurrent(actionGeneration) || !revision) return;
          if (!doc.defaultView.confirm(`${translate('禁用')} ${user.email}?`)) return;
          setBusy(true);
          showError('');
          try {
            await request(`/api/admin/accounts/${user.object_id}`, {
              method: 'PATCH', revision, body: { disabled: true, permissions: user.permissions },
            });
            if (!stillCurrent(actionGeneration)) return;
            await loadAdmin(actionGeneration);
          } catch (error) {
            if (stillCurrent(actionGeneration)) showError(error.message || '无法禁用用户。');
          } finally {
            if (stillCurrent(actionGeneration)) setBusy(false);
          }
        });
        actions.append(recovery, disable);
        row.append(actions);
      }
      return row;
    }));
  }

  launcher.addEventListener('click', async () => {
    if (busy) return;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    const generation = ++dialogGeneration;
    setBusy(true);
    showError('');
    try {
      if (session?.user?.is_admin) {
        const budget = await request('/api/budget');
        if (!stillCurrent(generation)) return;
        applyBudget(budget);
      }
      await loadAdmin(generation);
    } catch (error) {
      if (stillCurrent(generation)) showError(error.message || '账户信息加载失败。');
    } finally {
      if (stillCurrent(generation)) setBusy(false);
    }
  });
  byId('account-close').addEventListener('click', () => {
    clearDialogState();
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
  });
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    clearDialogState();
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
  });
  byId('password-form').addEventListener('submit', async event => {
    event.preventDefault();
    const generation = dialogGeneration;
    setBusy(true);
    showError('');
    try {
      const result = await request('/auth/password', {
        method: 'POST',
        body: {
          current_password: byId('account-current-password').value,
          new_password: byId('account-new-password').value,
        },
      });
      if (!stillCurrent(generation)) return;
      clearInputs(byId('account-current-password'), byId('account-new-password'));
      setSession(result);
      onSession?.(result);
    } catch (error) {
      if (stillCurrent(generation)) {
        clearInputs(byId('account-current-password'), byId('account-new-password'));
        showError(error.message || '密码更新失败。');
      }
    } finally {
      if (stillCurrent(generation)) setBusy(false);
    }
  });
  byId('invite-form').addEventListener('submit', async event => {
    event.preventDefault();
    const generation = dialogGeneration;
    setBusy(true);
    showError('');
    try {
      const result = await request('/api/admin/invitations', { method: 'POST', body: { email: byId('invite-email').value.trim() } });
      if (!stillCurrent(generation)) return;
      renderInviteResult(result);
      clearInputs(byId('invite-email'));
      await loadAdmin();
    } catch (error) {
      if (stillCurrent(generation)) {
        clearInputs(byId('invite-email'));
        clearInviteResult();
        showError(error.message || '无法生成邀请链接。');
      }
    } finally {
      if (stillCurrent(generation)) setBusy(false);
    }
  });
  byId('origins-form').addEventListener('submit', async event => {
    event.preventDefault();
    const generation = dialogGeneration;
    setBusy(true);
    showError('');
    try {
      const approved_origins = byId('approved-origins').value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
      const result = await request('/api/admin/approved-origins', {
        method: 'PUT',
        revision: originVersion ?? undefined,
        body: { approved_origins },
      });
      if (!stillCurrent(generation)) return;
      originVersion = result.version;
      byId('approved-origins').value = (result.approved_origins || []).join('\n');
    } catch (error) {
      if (stillCurrent(generation)) showError(error.message || '白名单保存失败。');
    } finally {
      if (stillCurrent(generation)) setBusy(false);
    }
  });
  byId('account-sign-out').addEventListener('click', async () => {
    const generation = dialogGeneration;
    setBusy(true);
    showError('');
    try {
      await request('/auth/logout', { method: 'POST', body: {} });
      if (!stillCurrent(generation)) return;
      clearDialogState({ releaseBusy: false });
      onLogout?.();
      doc.defaultView.location.assign('/signed-out');
    } catch (error) {
      if (stillCurrent(generation)) {
        clearDialogState();
        showError(error.message || '退出失败。');
        setBusy(false);
      }
    }
  });

  return {
    setSession,
    setBudget(snapshot) {
      applyBudget(snapshot);
    },
    handleError(error) {
      if (error?.status === 401) doc.defaultView.location.assign('/auth/login');
    },
    dispose() {
      offLocale();
      disposed = true;
      clearDialogState();
      launcher.remove();
      dialog.remove();
      accountName.remove();
    },
  };
}

if (typeof document !== 'undefined') mountAuthPage(document);