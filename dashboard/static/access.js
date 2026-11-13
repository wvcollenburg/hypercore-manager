/* HyperCore Manager — roles, tenants and access (plans/PLAN-roles.md).
 *
 * - hcmModal / hcmConfirm / hcmPrompt / hcmAlert: the one dialog component (no browser popups).
 * - can() / canSome() / canVm(): what the signed-in user may do, from /api/users/me → access.
 *   The browser only hides what would be refused anyway; the server checks every call.
 * - The upgrade dialog: after the roles upgrade a root admin assigns access to every account the
 *   upgrade disabled; it cannot be closed while one is left.
 * - Settings → Access: users, groups, roles, assignments, audit (and guest console access, R4).
 */
'use strict';

// ─────────────────────────────────────────────────────────────── dialogs ──
const _hcmModals = [];

function hcmModal({ title = '', body = '', buttons = [], closable = true, width = 'min(640px, 96vw)', onClose = null, cls = '' } = {}) {
  const back = document.createElement('div');
  back.className = 'vm-modal-backdrop visible hcm-modal-back';
  back.style.zIndex = String(10200 + _hcmModals.length * 10);
  back.innerHTML = `
    <div class="vm-modal hcm-modal ${cls}" role="dialog" aria-modal="true" style="width:${width}">
      <div class="vm-modal-header">
        <div class="vm-modal-title">${title}</div>
        ${closable ? '<button class="vm-modal-close" data-x="1" aria-label="close">✕</button>' : ''}
      </div>
      <div class="hcm-modal-body"></div>
      <div class="hcm-modal-msg"></div>
      <div class="hcm-modal-foot"></div>
    </div>`;
  const handle = {
    el: back,
    body: back.querySelector('.hcm-modal-body'),
    close(result) {
      const i = _hcmModals.indexOf(handle);
      if (i >= 0) _hcmModals.splice(i, 1);
      back.remove();
      if (onClose) onClose(result);
    },
    setBody(html) { handle.body.innerHTML = html; },
    setMsg(text, kind = 'err') {
      const m = back.querySelector('.hcm-modal-msg');
      m.innerHTML = text ? `<div class="${kind === 'ok' ? 'um-success' : 'um-error'}">${_esc(text)}</div>` : '';
    },
    setButtons(list) {
      const foot = back.querySelector('.hcm-modal-foot');
      foot.innerHTML = '';
      for (const b of list) {
        const el = document.createElement('button');
        el.className = b.kind === 'primary' ? 'update-apply-btn' : b.kind === 'danger' ? 'um-action-btn danger hcm-btn-danger' : 'refresh-btn';
        el.textContent = b.label;
        if (b.id) el.id = b.id;
        el.onclick = async () => {
          if (el.disabled) return;
          el.disabled = true;
          try { await b.onClick(handle); } finally { el.disabled = false; }
        };
        foot.appendChild(el);
      }
    },
    closable,
  };
  handle.body.innerHTML = body;
  handle.setButtons(buttons);
  if (closable) {
    back.querySelector('[data-x]').onclick = () => handle.close(null);
    back.addEventListener('mousedown', e => { if (e.target === back) handle.close(null); });
  }
  document.body.appendChild(back);
  _hcmModals.push(handle);
  setTimeout(() => back.querySelector('input:not([type=hidden]):not([type=checkbox]), select, textarea')?.focus(), 30);
  return handle;
}
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || !_hcmModals.length) return;
  const top = _hcmModals[_hcmModals.length - 1];
  if (top.closable) { e.stopPropagation(); top.close(null); }
}, true);

function hcmConfirm(message, { title = t('acc.confirm'), confirmLabel = t('acc.ok'), danger = false, typeToConfirm = null } = {}) {
  return new Promise(resolve => {
    const extra = typeToConfirm ? `<label class="hcm-field" style="margin-top:12px;"><span>${_esc(t('acc.type_to_confirm', { name: typeToConfirm }))}</span>
      <input type="text" class="um-input" id="hcmTypeConfirm" autocomplete="off"></label>` : '';
    let done = false;
    const m = hcmModal({
      title: _esc(title), width: 'min(480px, 96vw)',
      body: `<div class="hcm-text">${_esc(message).replace(/\n/g, '<br>')}</div>${extra}`,
      onClose: () => { if (!done) resolve(false); },
      buttons: [
        { label: t('console.cancel'), onClick: h => h.close() },
        { label: confirmLabel, kind: danger ? 'danger' : 'primary', onClick: h => {
          if (typeToConfirm && document.getElementById('hcmTypeConfirm').value.trim() !== typeToConfirm) { h.setMsg(t('acc.type_mismatch')); return; }
          done = true; h.close(); resolve(true);
        } },
      ],
    });
    m.el.addEventListener('keydown', e => { if (e.key === 'Enter') m.el.querySelector('.hcm-modal-foot button:last-child').click(); });
  });
}

function hcmPrompt(label, { title = '', value = '', type = 'text', placeholder = '', confirmLabel = t('acc.ok'), hint = '' } = {}) {
  return new Promise(resolve => {
    let done = false;
    const m = hcmModal({
      title: _esc(title || label), width: 'min(480px, 96vw)',
      body: `<label class="hcm-field"><span>${_esc(label)}</span>
               <input class="um-input" id="hcmPromptIn" type="${type}" value="${_esc(value)}" placeholder="${_esc(placeholder)}" autocomplete="off"></label>
             ${hint ? `<div class="um-hint">${_esc(hint)}</div>` : ''}`,
      onClose: () => { if (!done) resolve(null); },
      buttons: [
        { label: t('console.cancel'), onClick: h => h.close() },
        { label: confirmLabel, kind: 'primary', onClick: h => { done = true; const v = document.getElementById('hcmPromptIn').value; h.close(); resolve(v); } },
      ],
    });
    m.el.querySelector('#hcmPromptIn').addEventListener('keydown', e => { if (e.key === 'Enter') m.el.querySelector('.hcm-modal-foot button:last-child').click(); });
  });
}

function hcmAlert(message, { title = t('acc.notice') } = {}) {
  return new Promise(resolve => {
    hcmModal({ title: _esc(title), width: 'min(480px, 96vw)', body: `<div class="hcm-text">${_esc(message).replace(/\n/g, '<br>')}</div>`,
      onClose: () => resolve(), buttons: [{ label: t('acc.ok'), kind: 'primary', onClick: h => h.close() }] });
  });
}

// ───────────────────────────────────────────────────────── what may I do ──
window.HCM_ACCESS = null;

function _accSet(me) { window.HCM_ACCESS = (me && me.access) || null; }

/** On the whole cluster (or, without one, anywhere at all). */
function can(perm, clusterId) {
  const a = window.HCM_ACCESS;
  if (!a) return false;
  if (a.root_admin) return true;
  if (!clusterId) return (a.anywhere || []).includes(perm);
  const c = (a.clusters || {})[clusterId];
  return !!c && c.all.includes(perm);
}
/** On the cluster, or on at least some of its VMs. */
function canSome(perm, clusterId) {
  const a = window.HCM_ACCESS;
  if (!a) return false;
  if (a.root_admin) return true;
  const c = (a.clusters || {})[clusterId || currentCluster];
  return !!c && (c.all.includes(perm) || c.some.includes(perm));
}
/** On one VM (its tags matter for tag grants). */
function canVm(perm, clusterId, vmUuid, tags) {
  const a = window.HCM_ACCESS;
  if (!a) return false;
  if (a.root_admin) return true;
  const c = (a.clusters || {})[clusterId || currentCluster];
  if (!c) return false;
  if (c.all.includes(perm)) return true;
  return (c.grants || []).some(g => g.perms.includes(perm) && ((g.vm && g.vm === vmUuid) || (g.tag && (tags || []).includes(g.tag))));
}
/** A tree permission at the install, or at the given node or above. */
function canNode(perm, nodeId) {
  const a = window.HCM_ACCESS;
  if (!a) return false;
  if (a.root_admin) return true;
  const tops = (a.nodes || {})[perm] || [];
  if (tops.includes('*')) return true;
  if (!nodeId) return false;
  return _accAncestors(nodeId).some(n => tops.includes(n));
}
function _accAncestors(nodeId) {
  const out = [];
  const nodes = (_ac.data && _ac.data.nodes) || [];
  let cur = nodeId, guard = 0;
  while (cur && guard++ < 10) { out.push(cur); cur = (nodes.find(n => n.id === cur) || {}).parent_id; }
  if (!nodes.length) out.push(nodeId);
  return out;
}
/** Does this user see any Settings panel at all. */
function canSettings() {
  return ['cluster.settings', 'cluster.manage', 'users.manage', 'groups.manage', 'roles.manage', 'audit.view',
          'backup.targets.manage', 'backup.proxies.manage', 'console_check.manage', 'console.audit', 'install.manage',
          'guest_console.grant', 'guest_console.request'].some(p => can(p) || canAnyVmPerm(p));
}

function _accMe() {
  return (typeof _currentUser !== 'undefined' && _currentUser) || window._accMeCache || {};
}

async function hcmLoadAccess() {
  const me = await apiFetch('/api/users/me');
  _accSet(me);
  return me;
}

// ──────────────────────────────────────────────────────────── shared data ──
let _ac = { data: null, perms: null, tab: 'users', query: '', auditRows: [] };

async function _acLoad() {
  const [data, perms] = await Promise.all([apiFetch('/api/access/overview'), _ac.perms ? Promise.resolve(_ac.perms) : apiFetch('/api/access/permissions')]);
  _ac.data = data;
  _ac.perms = perms;
  return data;
}

async function _acCall(method, path, body) {
  const r = await fetch(`${API_BASE}${path}`, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}

function _acNodePath(id) {
  if (!id) return t('acc.whole_install');
  const nodes = (_ac.data && _ac.data.nodes) || [];
  const parts = [];
  let cur = id, guard = 0;
  while (cur && guard++ < 10) {
    const n = nodes.find(x => x.id === cur);
    if (!n) break;
    parts.unshift(n.name);
    cur = n.parent_id;
  }
  if (_ac.data && _ac.data.single_tenant) parts.splice(0, Math.min(2, parts.length - 1));
  return parts.join(' / ');   // a node above the reader is not in their list: no name, no id
}
function _acLevelLabel(level) { return t('acc.level_' + level); }
function _acRoleName(r) { return r.builtin_key ? t('acc.role_' + r.builtin_key) : r.name; }

/** Nodes the user may use as a home or a scope for a given node permission, sorted by path. */
function _acNodesFor(perm, levels) {
  const nodes = ((_ac.data && _ac.data.nodes) || []).filter(n => !levels || levels.includes(n.level));
  return nodes.filter(n => canNode(perm, n.id)).sort((a, b) => _acNodePath(a.id).localeCompare(_acNodePath(b.id)));
}

// ─────────────────────────────────────────────────── the upgrade dialog ──
async function hcmCheckPending() {
  // outside console requests that wait for this user (anyone who may grant on their VM)
  if (canAnyVmPerm('guest_console.grant')) {
    const g = await apiFetch('/api/guest/grants');
    if (g && g.pending) showToast(t('gc.pending_toast', { n: g.pending }), 'ok');
    _ac.guestPending = (g && g.pending) || 0;
  }
  if (!(window.HCM_ACCESS && window.HCM_ACCESS.root_admin)) return;
  const d = await apiFetch('/api/access/pending');
  if (!d || !(d.pending || []).length) return;
  await _acLoad();
  _openPendingModal(d.pending);
}

function _openPendingModal(pending) {
  const roles = (_ac.data.roles || []).filter(r => r.node_id === null);
  const viewer = roles.find(r => r.builtin_key === 'viewer');
  const nodes = (_ac.data.nodes || []).slice().sort((a, b) => _acNodePath(a.id).localeCompare(_acNodePath(b.id)));
  const clusters = _ac.data.clusters || [];
  const scopeOptions = (u) => {
    const own = u.legacy_folders.length ? `<option value="legacy" selected>${_esc(t('acc.pending_former', { list: u.legacy_folders.map(f => f.name).join(', ') }))}</option>` : '';
    return own + `<option value="root" ${own ? '' : 'selected'}>${_esc(t('acc.whole_install'))}</option>`
      + nodes.map(n => `<option value="node:${n.id}">${_esc(_acLevelLabel(n.level))}: ${_esc(_acNodePath(n.id))}</option>`).join('')
      + clusters.map(c => `<option value="cluster:${c.cluster_id}">${_esc(t('acc.scope_cluster'))}: ${_esc(c.name)}</option>`).join('');
  };
  const row = u => `
    <tr data-uid="${u.user_id}">
      <td class="um-username">${_esc(u.username)}${u.display_name ? ` <span class="um-hint">${_esc(u.display_name)}</span>` : ''}</td>
      <td><select class="um-select" data-role>${roles.map(r => `<option value="${r.role_id}" ${viewer && r.role_id === viewer.role_id ? 'selected' : ''}>${_esc(_acRoleName(r))}</option>`).join('')}</select></td>
      <td><select class="um-select" data-scope style="max-width:340px">${scopeOptions(u)}</select></td>
      <td style="white-space:nowrap">
        <button class="um-primary-btn" onclick="pendingSave('${u.user_id}')">${_esc(t('acc.pending_save'))}</button>
        <button class="um-action-btn danger" onclick="pendingDelete('${u.user_id}', '${_esc(u.username)}')">${_esc(t('users.delete'))}</button>
      </td>
    </tr>`;
  window._pendingList = pending;
  window._pendingModal = hcmModal({
    title: _esc(t('acc.pending_title')), closable: false, width: 'min(1000px, 96vw)',
    body: `<p class="hcm-text">${_esc(t('acc.pending_intro'))}</p>
           <table class="um-table"><thead><tr><th>${_esc(t('acc.col_email'))}</th><th>${_esc(t('acc.col_role'))}</th><th>${_esc(t('acc.col_scope'))}</th><th></th></tr></thead>
           <tbody id="pendingRows">${pending.map(row).join('')}</tbody></table>
           <p class="um-hint" style="margin-top:10px">${_esc(t('acc.pending_hint'))}</p>`,
    buttons: [],
  });
}

async function pendingSave(uid) {
  const tr = document.querySelector(`#pendingRows tr[data-uid="${uid}"]`);
  const u = window._pendingList.find(x => x.user_id === uid);
  const role_id = tr.querySelector('[data-role]').value;
  const sv = tr.querySelector('[data-scope]').value;
  const scopes = sv === 'legacy' ? u.legacy_folders.map(f => ({ kind: 'node', node_id: f.id }))
    : sv === 'root' ? [{ kind: 'root' }]
    : sv.startsWith('node:') ? [{ kind: 'node', node_id: sv.slice(5) }]
    : [{ kind: 'cluster', cluster_id: sv.slice(8) }];
  try {
    for (const scope of scopes) await _acCall('POST', '/api/access/assignments', { user_id: uid, role_id, scope });
    _pendingDone(uid);
    showToast(t('acc.pending_saved', { name: u.username }), 'ok');
  } catch (e) { window._pendingModal.setMsg(e.message); }
}

async function pendingDelete(uid, name) {
  if (!await hcmConfirm(t('acc.delete_user_confirm', { name }), { danger: true, confirmLabel: t('users.delete') })) return;
  try { await _acCall('DELETE', `/api/access/users/${uid}`); _pendingDone(uid); } catch (e) { window._pendingModal.setMsg(e.message); }
}

function _pendingDone(uid) {
  window._pendingList = window._pendingList.filter(x => x.user_id !== uid);
  document.querySelector(`#pendingRows tr[data-uid="${uid}"]`)?.remove();
  if (!window._pendingList.length) { window._pendingModal.close(); hcmAlert(t('acc.pending_all_done')); }
}

// ───────────────────────────────────────────────────── Settings → Access ──
async function loadAccessPanel() {
  const el = document.getElementById('setpanel-users');
  if (!el) return;
  if (!_ac.data) el.innerHTML = `<div class="loading"><div class="spinner"></div></div>`;
  await _acLoad();
  if (!_ac.data) { el.innerHTML = `<div class="empty-state">${_esc(t('empty.settings_failed'))}</div>`; return; }
  renderAccessPanel();
}

function acTab(tab) { _ac.tab = tab; try { localStorage.setItem('hcm.access.tab', tab); } catch (_) {} renderAccessPanel(); }
try { _ac.tab = localStorage.getItem('hcm.access.tab') || 'users'; } catch (_) {}

function renderAccessPanel() {
  const el = document.getElementById('setpanel-users');
  if (!el || !_ac.data) return;
  const tabs = [
    ['users', t('acc.tab_users'), can('users.manage')],
    ['groups', t('acc.tab_groups'), can('groups.manage') || can('users.manage')],
    ['roles', t('acc.tab_roles'), true],
    ['tree', t('acc.tab_tree'), can('tree.manage') || can('users.manage')],
    ['guest', t('acc.tab_guest') + (_ac.guestPending ? ` (${_ac.guestPending})` : ''), canAnyVmPerm('guest_console.request')],
    ['audit', t('acc.tab_audit'), can('audit.view')],
  ].filter(x => x[2]);
  if (!tabs.some(x => x[0] === _ac.tab)) _ac.tab = tabs[0][0];
  const newPerms = (_ac.data.new_permissions || []);
  const banner = newPerms.length ? `<div class="hcm-banner">${_esc(t('acc.new_perms', { list: newPerms.join(', ') }))}
      <button class="um-action-btn" onclick="acAckPerms()">${_esc(t('acc.new_perms_ack'))}</button></div>` : '';
  el.innerHTML = `
    <div class="settings-group">
      <div class="settings-group-header">
        <div class="chart-title-dot" style="background:var(--accent-purple)"></div>
        <span class="settings-group-title">${_esc(t('acc.title'))}</span>
        <span style="flex:1"></span>
        <span class="um-seg">${tabs.map(([k, label]) => `<button class="${_ac.tab === k ? 'active' : ''}" onclick="acTab('${k}')">${_esc(label)}</button>`).join('')}</span>
      </div>
      ${banner}
      <div id="acBody"></div>
    </div>`;
  ({ users: _acRenderUsers, groups: _acRenderGroups, roles: _acRenderRoles, tree: _acRenderTree, audit: _acRenderAudit,
     guest: (window.acRenderGuest || (() => {})) })[_ac.tab]();
}

function canAnyVmPerm(perm) {
  const a = window.HCM_ACCESS;
  if (!a) return false;
  return Object.values(a.clusters || {}).some(c => c.all.includes(perm) || c.some.includes(perm));
}

async function acAckPerms() {
  try { await _acCall('POST', '/api/access/permissions/acknowledge'); await loadAccessPanel(); } catch (e) { hcmAlert(e.message); }
}

// ── users ───────────────────────────────────────────────────────────────────
function acSearch(v) { _ac.query = v; const b = document.getElementById('acBody'); if (b) _acRenderUsers(true); }

function _acRenderUsers(keepFocus) {
  const body = document.getElementById('acBody');
  const q = _ac.query.trim().toLowerCase();
  const users = (_ac.data.users || []).filter(u => !q || u.username.toLowerCase().includes(q) || (u.display_name || '').toLowerCase().includes(q));
  const me = _accMe();
  const state = u => u.awaiting_email ? `<span class="um-badge hcm-badge-warn">${_esc(t('acc.state_awaiting_email'))}</span>`
    : u.awaiting_access ? `<span class="um-badge hcm-badge-warn">${_esc(t('acc.state_awaiting'))}</span>`
    : !u.enabled ? `<span class="um-badge hcm-badge-off">${_esc(t('acc.state_disabled'))}</span>`
    : `<span class="um-badge um-badge-otp-on">${_esc(t('acc.state_enabled'))}</span>`;
  body.innerHTML = `
    <div class="um-toolbar">
      <input class="um-input" id="acSearch" placeholder="${_esc(t('users.search_placeholder'))}" value="${_esc(_ac.query)}" oninput="acSearch(this.value)" autocomplete="off">
      <span style="flex:1"></span>
      <button class="um-primary-btn" onclick="acNewUser()">+ ${_esc(t('users.add'))}</button>
    </div>
    ${users.length ? `<table class="um-table"><thead><tr><th>${_esc(t('acc.col_email'))}</th>
        <th>${_esc(t('acc.col_state'))}</th><th>OTP</th><th></th></tr></thead><tbody>
      ${users.map(u => `<tr>
        <td class="um-username">${_esc(u.username)}${u.username === me.username ? ` <span class="um-hint">(${_esc(t('users.you'))})</span>` : ''}
            ${u.display_name ? `<div class="um-hint">${_esc(u.display_name)}</div>` : ''}</td>
        <td>${state(u)}</td>
        <td><span class="um-badge ${u.otp_enabled ? 'um-badge-otp-on' : 'um-badge-otp-off'}">${u.otp_enabled ? 'on' : 'off'}</span></td>
        <td style="white-space:nowrap"><button class="um-action-btn" onclick="acOpenUser('${u.user_id}')">${_esc(t('acc.access_btn'))}</button></td>
      </tr>`).join('')}</tbody></table>`
      : `<div class="um-hint" style="padding:10px 0">${_esc(t('users.none'))}</div>`}`;
  if (keepFocus) { const i = document.getElementById('acSearch'); if (i) { i.focus(); i.setSelectionRange(i.value.length, i.value.length); } }
}

/** The scope an assign form (prefix p) describes. */
function _acScopeFrom(p) {
  const $ = id => document.getElementById(p + id);
  const k = $('Kind').value;
  const scope = { kind: k };
  if (k === 'node') scope.node_id = $('Node').value;
  if (['cluster', 'tag', 'vm'].includes(k)) scope.cluster_id = $('Cluster').value;
  if (k === 'tag') scope.tag = $('Target').value;
  if (k === 'vm') { scope.vm_uuid = $('Target').value; scope.vm_name = $('Target').selectedOptions[0]?.textContent; }
  return scope;
}

/** A new account: its login is the email address, and it comes with its first access (below the top level
 *  an account without access in your reach would vanish from your sight at once). */
function acNewUser() {
  const m = hcmModal({
    title: _esc(t('users.add')), width: 'min(820px, 96vw)',
    body: `<div class="hcm-form">
      <label class="hcm-field"><span>${_esc(t('acc.col_email'))}</span><input class="um-input" id="nuEmail" type="email" autocomplete="off" placeholder="name@example.com"></label>
      <label class="hcm-field"><span>${_esc(t('acc.display_name'))}</span><input class="um-input" id="nuDisplay" autocomplete="off"></label>
      <label class="hcm-field"><span>${_esc(t('users.password'))}</span><input class="um-input" id="nuPw" type="password" autocomplete="new-password"></label>
      <div class="um-section-title" style="margin-top:6px">${_esc(t('acc.first_access'))}</div>
      ${_acAssignForm('nu')}
      <div class="um-hint">${_esc(t('acc.new_user_hint'))}</div></div>`,
    buttons: [
      { label: t('console.cancel'), onClick: h => h.close() },
      { label: t('acc.create'), kind: 'primary', onClick: async h => {
        try {
          const d = await _acCall('POST', '/api/access/users', { email: document.getElementById('nuEmail').value,
            display_name: document.getElementById('nuDisplay').value, password: document.getElementById('nuPw').value,
            role_id: document.getElementById('nuRole').value, scope: _acScopeFrom('nu') });
          h.close(); await _acLoad(); renderAccessPanel(); acOpenUser(d.user_id);
        } catch (e) { h.setMsg(e.message); }
      } },
    ],
  });
  const viewer = (_ac.data.roles || []).find(r => r.builtin_key === 'viewer');
  if (viewer) document.getElementById('nuRole').value = viewer.role_id;   // the least a new account starts with
  _acAssignWire('nu', {}, m, () => {});
  document.getElementById('nuAdd').style.display = 'none';      // the dialog's own button creates
}

async function acOpenUser(uid) {
  const u = await apiFetch(`/api/access/users/${uid}`);
  if (!u) return;
  const me = _accMe();
  const self = u.username === me.username;
  const may = u.may || {};
  const rows = (u.assignments || []).map(a => `<tr>
      <td>${_esc(a.builtin_key ? t('acc.role_' + a.builtin_key) : a.role_name)}</td>
      <td>${_esc(a.scope_label)}${a.via_group ? ` <span class="um-hint">(${_esc(t('acc.via_group', { name: a.group_name }))})</span>` : ''}</td>
      <td class="um-hint">${_esc(a.created_by || '')}</td>
      <td>${!a.via_group && !self ? `<button class="um-action-btn danger" onclick="acRemoveAssignment('${a.assignment_id}', 'user', '${uid}')">${_esc(t('acc.remove'))}</button>` : ''}</td></tr>`).join('');
  const m = hcmModal({
    title: `${_esc(t('acc.user'))} · ${_esc(u.display_name ? u.display_name + ' · ' + u.username : u.username)}`, width: 'min(900px, 96vw)',
    body: `
      <div class="hcm-grid2">
        <label class="hcm-field"><span>${_esc(t('acc.display_name'))}</span><input class="um-input" id="uaDisplay" value="${_esc(u.display_name || '')}" ${may.display_name ? '' : 'disabled'}></label>
        ${may.account ? `<label class="hcm-field"><span>${_esc(t('acc.col_email'))}</span><input class="um-input" id="uaEmail" type="email" value="${_esc(u.awaiting_email ? '' : u.username)}" placeholder="name@example.com"></label>` : ''}
      </div>
      ${may.account && u.awaiting_email ? `<div class="um-hint hcm-badge-warn" style="margin-top:6px">${_esc(t('acc.email_needed_hint', { name: u.username }))}</div>
        <label class="hcm-field" style="margin-top:6px"><span>${_esc(t('users.password'))}</span><input class="um-input" id="uaPw" type="password" autocomplete="new-password"></label>` : ''}
      ${may.account ? '' : `<div class="um-hint" style="margin-top:6px">${_esc(t('acc.account_top_only'))}</div>`}
      <div class="um-section-title" style="margin-top:14px">${_esc(t('acc.assignments'))}</div>
      ${self ? `<div class="um-hint">${_esc(t('acc.self_hint'))}</div>` : ''}
      ${rows ? `<table class="um-table"><thead><tr><th>${_esc(t('acc.col_role'))}</th><th>${_esc(t('acc.col_scope'))}</th><th>${_esc(t('acc.col_by'))}</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
             : `<div class="um-hint">${_esc(t('acc.no_assignments'))}</div>`}
      ${u.legacy_folder_names && u.legacy_folder_names.length ? `<div class="um-hint" style="margin-top:6px">${_esc(t('acc.legacy_folders', { list: u.legacy_folder_names.join(', ') }))}</div>` : ''}
      ${self ? '' : `<div class="um-section-title" style="margin-top:14px">${_esc(t('acc.add_assignment'))}</div>${_acAssignForm('ua')}`}
      ${(_ac.data.groups || []).length && !self ? `<div class="um-section-title" style="margin-top:14px">${_esc(t('acc.tab_groups'))}</div>
        <div class="hcm-chips">${(_ac.data.groups || []).map(g => `<label class="hcm-chip"><input type="checkbox" ${u.groups.includes(g.group_id) ? 'checked' : ''}
          onchange="acToggleMember('${g.group_id}', '${uid}', this.checked, this)"> ${_esc(g.name)}</label>`).join('')}</div>` : ''}`,
    buttons: [
      ...(self || may.account ? [] : [
        { label: t('acc.remove_from_tenants'), kind: 'danger', onClick: async h => {
          if (!await hcmConfirm(t('acc.remove_from_tenants_confirm', { name: u.username }), { danger: true, confirmLabel: t('acc.remove') })) return;
          try { await _acCall('POST', `/api/access/users/${uid}/remove`); h.close(); await loadAccessPanel(); } catch (e) { h.setMsg(e.message); }
        } },
      ]),
      ...(self || !may.account ? [] : [
        { label: t('users.delete'), kind: 'danger', onClick: async h => {
          if (!await hcmConfirm(t('acc.delete_user_confirm', { name: u.username }), { danger: true, confirmLabel: t('users.delete'), typeToConfirm: u.username })) return;
          try { await _acCall('DELETE', `/api/access/users/${uid}`); h.close(); await loadAccessPanel(); } catch (e) { h.setMsg(e.message); }
        } },
        { label: u.enabled ? t('acc.disable') : t('acc.enable'), onClick: async h => {
          try { await _acCall('PUT', `/api/access/users/${uid}`, { enabled: !u.enabled }); h.close(); await loadAccessPanel(); acOpenUser(uid); } catch (e) { h.setMsg(e.message); }
        } },
        { label: t('users.reset_pw'), onClick: async h => {
          const pw = await hcmPrompt(t('acc.new_password_for', { name: u.username }), { type: 'password', hint: t('acc.password_hint') });
          if (!pw) return;
          try { await _acCall('PUT', `/api/access/users/${uid}/password`, { new_password: pw }); h.setMsg(t('acc.password_set'), 'ok'); } catch (e) { h.setMsg(e.message); }
        } },
      ]),
      { label: t('acc.save'), kind: 'primary', onClick: async h => {
        try {
          const body = {};
          if (may.display_name) body.display_name = document.getElementById('uaDisplay').value;
          const em = document.getElementById('uaEmail');
          if (em && em.value.trim() && em.value.trim().toLowerCase() !== (u.awaiting_email ? '' : u.username)) {
            body.email = em.value.trim();
            const pw = document.getElementById('uaPw');
            if (pw) body.password = pw.value;
          }
          if (!Object.keys(body).length) { h.close(); return; }
          await _acCall('PUT', `/api/access/users/${uid}`, body);
          h.setMsg(t('acc.saved'), 'ok'); await _acLoad(); renderAccessPanel();
        } catch (e) { h.setMsg(e.message); }
      } },
    ],
  });
  window._acUserModal = m;
  _acAssignWire('ua', { user_id: uid }, m, () => { m.close(); acOpenUser(uid); });
}

async function acToggleMember(gid, uid, member, box) {
  try { await _acCall('PUT', `/api/access/groups/${gid}/members`, { user_id: uid, member }); await _acLoad(); }
  catch (e) { box.checked = !member; hcmAlert(e.message); }
}

async function acRemoveAssignment(aid, kind, id) {
  if (!await hcmConfirm(t('acc.remove_assignment_confirm'), { danger: true, confirmLabel: t('acc.remove') })) return;
  try {
    await _acCall('DELETE', `/api/access/assignments/${aid}`);
    _hcmModals[_hcmModals.length - 1]?.close();
    await _acLoad(); renderAccessPanel();
    if (kind === 'user') acOpenUser(id); else if (kind === 'group') acOpenGroup(id); else if (kind === 'who') acWho(id.cluster, id.vm);
  } catch (e) { hcmAlert(e.message); }
}

// ── the assignment form (role + scope), reused for users, groups and folders ──
function _acAssignForm(p, preset) {
  const roles = _ac.data.roles || [];
  const nodes = (_ac.data.nodes || []).slice().sort((a, b) => _acNodePath(a.id).localeCompare(_acNodePath(b.id)));
  const clusters = (_ac.data.clusters || []).slice().sort((a, b) => a.name.localeCompare(b.name));
  const kinds = [['node', t('acc.scope_node')], ['cluster', t('acc.scope_cluster')], ['tag', t('acc.scope_tag')], ['vm', t('acc.scope_vm')]];
  if (window.HCM_ACCESS && window.HCM_ACCESS.root_admin) kinds.unshift(['root', t('acc.whole_install')]);
  const pk = (preset && preset.kind) || 'node';
  return `<div class="hcm-assign" id="${p}Assign">
    <select class="um-select" id="${p}Role">${roles.map(r => `<option value="${r.role_id}">${_esc(_acRoleName(r))}${r.node_id && _acNodePath(r.node_id) ? ' · ' + _esc(_acNodePath(r.node_id)) : ''}</option>`).join('')}</select>
    <span class="um-hint">${_esc(t('acc.on'))}</span>
    <select class="um-select" id="${p}Kind">${kinds.map(([k, l]) => `<option value="${k}" ${k === pk ? 'selected' : ''}>${_esc(l)}</option>`).join('')}</select>
    <select class="um-select" id="${p}Node" style="max-width:300px">${nodes.map(n => `<option value="${n.id}" ${preset && preset.node_id === n.id ? 'selected' : ''}>${_esc(_acLevelLabel(n.level))}: ${_esc(_acNodePath(n.id))}</option>`).join('')}</select>
    <select class="um-select" id="${p}Cluster" style="max-width:240px">${clusters.map(c => `<option value="${c.cluster_id}" ${preset && preset.cluster_id === c.cluster_id ? 'selected' : ''}>${_esc(c.name)}</option>`).join('')}</select>
    <select class="um-select" id="${p}Target" style="max-width:260px"></select>
    <button class="um-primary-btn" id="${p}Add">${_esc(t('acc.add'))}</button>
    <div class="um-hint" id="${p}RoleHint" style="width:100%"></div>
  </div>`;
}

function _acAssignWire(p, principal, modal, after) {
  const $ = id => document.getElementById(p + id);
  if (!$('Kind')) return;
  const sync = async () => {
    const k = $('Kind').value;
    $('Node').style.display = k === 'node' ? '' : 'none';
    $('Cluster').style.display = ['cluster', 'tag', 'vm'].includes(k) ? '' : 'none';
    $('Target').style.display = ['tag', 'vm'].includes(k) ? '' : 'none';
    if (['tag', 'vm'].includes(k)) {
      $('Target').innerHTML = `<option>${_esc(t('acc.loading'))}</option>`;
      const d = await apiFetch(`/api/vm-index?cluster=${encodeURIComponent($('Cluster').value)}`);
      const vms = (d && d.vms) || [];
      if (k === 'tag') {
        const tags = [...new Set(vms.flatMap(v => v.tags || []))].sort();
        $('Target').innerHTML = tags.length ? tags.map(tg => `<option value="${_esc(tg)}">${_esc(tg)} (${vms.filter(v => (v.tags || []).includes(tg)).length})</option>`).join('')
          : `<option value="">${_esc(t('acc.no_tags'))}</option>`;
      } else {
        $('Target').innerHTML = vms.sort((a, b) => a.name.localeCompare(b.name)).map(v => `<option value="${v.uuid}">${_esc(v.name)}</option>`).join('');
      }
    }
    const r = (_ac.data.roles || []).find(x => x.role_id === $('Role').value);
    $('RoleHint').textContent = r ? (r.description || r.permissions.join(', ')) : '';
  };
  $('Kind').onchange = sync; $('Cluster').onchange = sync; $('Role').onchange = sync;
  sync();
  $('Add').onclick = async () => {
    const scope = _acScopeFrom(p);
    try { await _acCall('POST', '/api/access/assignments', { ...principal, role_id: $('Role').value, scope }); await _acLoad(); renderAccessPanel(); after(); }
    catch (e) { modal.setMsg(e.message); }
  };
}

// ── groups ──────────────────────────────────────────────────────────────────
function _acRenderGroups() {
  const body = document.getElementById('acBody');
  const groups = _ac.data.groups || [];
  body.innerHTML = `
    <div class="um-toolbar"><span class="um-hint" style="flex:1">${_esc(t('acc.groups_intro'))}</span>
      ${can('groups.manage') ? `<button class="um-primary-btn" onclick="acNewGroup()">+ ${_esc(t('acc.new_group'))}</button>` : ''}</div>
    ${groups.length ? `<table class="um-table"><thead><tr><th>${_esc(t('acc.col_name'))}</th><th>${_esc(t('acc.col_defined_at'))}</th><th>${_esc(t('acc.col_members'))}</th><th></th></tr></thead><tbody>
      ${groups.map(g => `<tr><td class="um-username">${_esc(g.name)}${g.description ? `<div class="um-hint">${_esc(g.description)}</div>` : ''}</td>
        <td class="um-hint">${_esc(_acNodePath(g.node_id))}</td><td>${g.members.length}</td>
        <td><button class="um-action-btn" onclick="acOpenGroup('${g.group_id}')">${_esc(t('acc.access_btn'))}</button></td></tr>`).join('')}</tbody></table>`
      : `<div class="um-hint" style="padding:10px 0">${_esc(t('acc.no_groups'))}</div>`}`;
}

function _acNodeOptions(perm, selected, allowRoot) {
  const list = _acNodesFor(perm);
  return (allowRoot && window.HCM_ACCESS.root_admin ? `<option value="" ${!selected ? 'selected' : ''}>${_esc(t('acc.whole_install'))}</option>` : '')
    + list.map(n => `<option value="${n.id}" ${n.id === selected ? 'selected' : ''}>${_esc(_acLevelLabel(n.level))}: ${_esc(_acNodePath(n.id))}</option>`).join('');
}

function acNewGroup() {
  hcmModal({
    title: _esc(t('acc.new_group')), width: 'min(520px, 96vw)',
    body: `<div class="hcm-form">
      <label class="hcm-field"><span>${_esc(t('acc.col_name'))}</span><input class="um-input" id="ngName"></label>
      <label class="hcm-field"><span>${_esc(t('acc.description'))}</span><input class="um-input" id="ngDesc"></label>
      <label class="hcm-field"><span>${_esc(t('acc.col_defined_at'))}</span><select class="um-select" id="ngNode">${_acNodeOptions('groups.manage', null, true)}</select></label>
      <div class="um-hint">${_esc(t('acc.defined_at_hint'))}</div></div>`,
    buttons: [{ label: t('console.cancel'), onClick: h => h.close() },
      { label: t('acc.create'), kind: 'primary', onClick: async h => {
        try { const d = await _acCall('POST', '/api/access/groups', { name: document.getElementById('ngName').value, description: document.getElementById('ngDesc').value,
          node_id: document.getElementById('ngNode').value || null }); h.close(); await _acLoad(); renderAccessPanel(); acOpenGroup(d.group_id); }
        catch (e) { h.setMsg(e.message); }
      } }],
  });
}

async function acOpenGroup(gid) {
  const g = await apiFetch(`/api/access/groups/${gid}`);
  if (!g) return;
  const users = _ac.data.users || [];
  const rows = (g.assignments || []).map(a => `<tr><td>${_esc(a.builtin_key ? t('acc.role_' + a.builtin_key) : a.role_name)}</td><td>${_esc(a.scope_label)}</td>
      <td><button class="um-action-btn danger" onclick="acRemoveAssignment('${a.assignment_id}', 'group', '${gid}')">${_esc(t('acc.remove'))}</button></td></tr>`).join('');
  const memberIds = new Set(g.members.map(x => x.user_id));
  const m = hcmModal({
    title: `${_esc(t('acc.group'))} · ${_esc(g.name)}`, width: 'min(900px, 96vw)',
    body: `
      <div class="hcm-grid2">
        <label class="hcm-field"><span>${_esc(t('acc.col_name'))}</span><input class="um-input" id="gaName" value="${_esc(g.name)}"></label>
        <label class="hcm-field"><span>${_esc(t('acc.description'))}</span><input class="um-input" id="gaDesc" value="${_esc(g.description || '')}"></label>
      </div>
      <div class="um-section-title" style="margin-top:14px">${_esc(t('acc.col_members'))}</div>
      <div class="hcm-chips">${users.map(u => `<label class="hcm-chip"><input type="checkbox" ${memberIds.has(u.user_id) ? 'checked' : ''}
          onchange="acToggleMember('${gid}', '${u.user_id}', this.checked, this)"> ${_esc(u.username)}</label>`).join('') || `<span class="um-hint">${_esc(t('users.none'))}</span>`}</div>
      <div class="um-section-title" style="margin-top:14px">${_esc(t('acc.assignments'))}</div>
      ${rows ? `<table class="um-table"><thead><tr><th>${_esc(t('acc.col_role'))}</th><th>${_esc(t('acc.col_scope'))}</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
             : `<div class="um-hint">${_esc(t('acc.no_assignments'))}</div>`}
      <div class="um-section-title" style="margin-top:14px">${_esc(t('acc.add_assignment'))}</div>${_acAssignForm('ga')}`,
    buttons: [
      { label: t('acc.delete_group'), kind: 'danger', onClick: async h => {
        if (!await hcmConfirm(t('acc.delete_group_confirm', { name: g.name }), { danger: true, confirmLabel: t('users.delete') })) return;
        try { await _acCall('DELETE', `/api/access/groups/${gid}`); h.close(); await loadAccessPanel(); } catch (e) { h.setMsg(e.message); }
      } },
      { label: t('acc.save'), kind: 'primary', onClick: async h => {
        try { await _acCall('PUT', `/api/access/groups/${gid}`, { name: document.getElementById('gaName').value, description: document.getElementById('gaDesc').value });
          h.setMsg(t('acc.saved'), 'ok'); await _acLoad(); renderAccessPanel(); } catch (e) { h.setMsg(e.message); }
      } },
    ],
  });
  _acAssignWire('ga', { group_id: gid }, m, () => { m.close(); acOpenGroup(gid); });
}

// ── roles ───────────────────────────────────────────────────────────────────
function _acRenderRoles() {
  const body = document.getElementById('acBody');
  const roles = _ac.data.roles || [];
  body.innerHTML = `
    <div class="um-toolbar"><span class="um-hint" style="flex:1">${_esc(t('acc.roles_intro'))}</span>
      ${can('roles.manage') ? `<button class="um-primary-btn" onclick="acEditRole(null)">+ ${_esc(t('acc.new_role'))}</button>` : ''}</div>
    <table class="um-table"><thead><tr><th>${_esc(t('acc.col_name'))}</th><th>${_esc(t('acc.col_defined_at'))}</th><th>${_esc(t('acc.col_permissions'))}</th><th>${_esc(t('acc.col_in_use'))}</th><th></th></tr></thead><tbody>
    ${roles.map(r => `<tr>
      <td class="um-username">${_esc(_acRoleName(r))} ${r.builtin_key ? `<span class="um-badge um-badge-user">${_esc(t('acc.template'))}</span>` : ''}
          ${r.description ? `<div class="um-hint">${_esc(r.builtin_key ? t('acc.role_desc_' + r.builtin_key) : r.description)}</div>` : ''}</td>
      <td class="um-hint">${_esc(_acNodePath(r.node_id))}</td>
      <td class="um-hint">${r.permissions.length}</td>
      <td>${r.in_use || 0}</td>
      <td style="white-space:nowrap">
        <button class="um-action-btn" onclick="acViewRole('${r.role_id}')">${_esc(t('acc.view'))}</button>
        ${can('roles.manage') ? `<button class="um-action-btn" onclick="acEditRole('${r.role_id}', true)">${_esc(t('acc.copy'))}</button>` : ''}
        ${!r.builtin_key && canNode('roles.manage', r.node_id) ? `<button class="um-action-btn" onclick="acEditRole('${r.role_id}')">${_esc(t('acc.edit'))}</button>` : ''}
      </td></tr>`).join('')}</tbody></table>`;
}

function _acPermGrid(selected, disabled, held) {
  const perms = (_ac.perms && _ac.perms.permissions) || [];
  const areas = (_ac.perms && _ac.perms.areas) || [];
  return `<div class="hcm-permgrid">${areas.map(area => `
    <div class="hcm-permarea"><div class="um-section-title">${_esc(t('acc.area_' + area))}</div>
      ${perms.filter(p => p.area === area).map(p => {
        const mine = !held || held.has(p.key);
        return `<label class="hcm-perm ${mine ? '' : 'na'}" title="${_esc(p.key)}${p.implies.length ? ' → ' + _esc(p.implies.join(', ')) : ''}">
          <input type="checkbox" data-perm="${p.key}" ${selected.has(p.key) ? 'checked' : ''} ${disabled || !mine ? 'disabled' : ''}>
          <span>${_esc(t('perm.' + p.key))}</span><span class="hcm-level">${_esc(t('acc.lvl_' + p.level))}</span></label>`;
      }).join('')}</div>`).join('')}</div>`;
}

function acViewRole(rid) {
  const r = (_ac.data.roles || []).find(x => x.role_id === rid);
  if (!r) return;
  hcmModal({ title: `${_esc(t('acc.role'))} · ${_esc(_acRoleName(r))}`, width: 'min(980px, 96vw)',
    body: `${r.description ? `<p class="hcm-text">${_esc(r.builtin_key ? t('acc.role_desc_' + r.builtin_key) : r.description)}</p>` : ''}${_acPermGrid(new Set(r.permissions), true)}`,
    buttons: [{ label: t('acc.close'), kind: 'primary', onClick: h => h.close() }] });
}

function acEditRole(rid, copy) {
  const src = rid ? (_ac.data.roles || []).find(x => x.role_id === rid) : null;
  const editing = src && !copy;
  const held = new Set((window.HCM_ACCESS && window.HCM_ACCESS.root_admin) ? ((_ac.perms && _ac.perms.permissions) || []).map(p => p.key) : (window.HCM_ACCESS.anywhere || []));
  const sel = new Set(src ? src.permissions : []);
  const m = hcmModal({
    title: _esc(editing ? t('acc.edit_role') : t('acc.new_role')), width: 'min(1000px, 96vw)',
    body: `<div class="hcm-grid2">
        <label class="hcm-field"><span>${_esc(t('acc.col_name'))}</span><input class="um-input" id="reName" value="${_esc(editing ? src.name : (src ? t('acc.copy_of', { name: _acRoleName(src) }) : ''))}"></label>
        <label class="hcm-field"><span>${_esc(t('acc.col_defined_at'))}</span><select class="um-select" id="reNode" ${editing ? 'disabled' : ''}>${_acNodeOptions('roles.manage', editing ? src.node_id : null, true)}</select></label>
      </div>
      <label class="hcm-field"><span>${_esc(t('acc.description'))}</span><input class="um-input" id="reDesc" value="${_esc(editing ? (src.description || '') : '')}"></label>
      <div class="um-hint" style="margin:6px 0 10px">${_esc(t('acc.role_edit_hint'))}</div>
      ${editing && src.in_use ? `<div class="hcm-banner">${_esc(t('acc.role_in_use', { n: src.in_use }))}</div>` : ''}
      ${_acPermGrid(sel, false, held)}`,
    buttons: [
      ...(editing ? [{ label: t('users.delete'), kind: 'danger', onClick: async h => {
        if (!await hcmConfirm(t('acc.delete_role_confirm', { name: src.name }), { danger: true, confirmLabel: t('users.delete') })) return;
        try { await _acCall('DELETE', `/api/access/roles/${rid}`); h.close(); await loadAccessPanel(); } catch (e) { h.setMsg(e.message); }
      } }] : []),
      { label: t('console.cancel'), onClick: h => h.close() },
      { label: t('acc.save'), kind: 'primary', onClick: async h => {
        const perms = [...m.el.querySelectorAll('[data-perm]:checked')].map(x => x.dataset.perm);
        const body = { name: document.getElementById('reName').value, description: document.getElementById('reDesc').value, permissions: perms };
        try {
          if (editing) await _acCall('PUT', `/api/access/roles/${rid}`, body);
          else await _acCall('POST', '/api/access/roles', { ...body, node_id: document.getElementById('reNode').value || null, copied_from: src ? _acRoleName(src) : null });
          h.close(); await loadAccessPanel();
        } catch (e) { h.setMsg(e.message); }
      } },
    ],
  });
  // ticking a permission ticks what it implies (the server stores the closure anyway)
  const implies = Object.fromEntries(((_ac.perms && _ac.perms.permissions) || []).map(p => [p.key, p.implies]));
  m.el.querySelectorAll('[data-perm]').forEach(box => box.addEventListener('change', () => {
    if (!box.checked) return;
    const todo = [...(implies[box.dataset.perm] || [])];
    while (todo.length) {
      const k = todo.pop();
      const el = m.el.querySelector(`[data-perm="${k}"]`);
      if (el && !el.checked) { el.checked = true; todo.push(...(implies[k] || [])); }
    }
  }));
}

// ── tenants: a column browser (MSP → customer → folder → cluster) with a preview pane ──
const _AC_ICON = {
  msp: '<svg viewBox="0 0 24 24"><path d="M4 21V5l8-3 8 3v16M9 21v-4h6v4M8 8h2M14 8h2M8 12h2M14 12h2"/></svg>',
  customer: '<svg viewBox="0 0 24 24"><rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 13h18"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  cluster: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><path d="M7 7h.01M7 17h.01"/></svg>',
  user: '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>',
  group: '<svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5"/><path d="M2 20a7 7 0 0 1 14 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 13.5a7 7 0 0 1 4 6.5"/></svg>',
};
const _acIcon = k => `<span class="hcm-tb-icon hcm-tb-icon-${k}">${_AC_ICON[k] || ''}</span>`;
_ac.tb = _ac.tb || { path: [], cluster: null };

/** The columns' starting parent: a single-company install hides its default MSP and customer. */
function _acTbStart() {
  const nodes = _ac.data.nodes || [];
  if (!_ac.data.single_tenant) return null;
  const msp = nodes.find(n => n.level === 'msp' && !n.parent_id);
  const cust = msp && nodes.find(n => n.level === 'customer' && n.parent_id === msp.id);
  if (cust) return cust.id;
  const folder = nodes.find(n => n.level === 'folder');      // the reader sits below the hidden customer
  return folder && !nodes.some(n => n.id === folder.parent_id) ? folder.parent_id : null;
}

function _acRenderTree() {
  const body = document.getElementById('acBody');
  const nodes = _ac.data.nodes || [];
  const clusters = _ac.data.clusters || [];
  const kids = pid => nodes.filter(n => (n.parent_id || null) === (pid || null)).sort((a, b) => a.name.localeCompare(b.name));
  const tb = _ac.tb;
  const start = _acTbStart();
  // drop selections that no longer exist (after a delete or a reload)
  const valid = [];
  let parent = start;
  for (const id of tb.path) {
    const n = nodes.find(x => x.id === id && (x.parent_id || null) === (parent || null));
    if (!n) break;
    valid.push(id); parent = id;
  }
  tb.path = valid;
  const last = nodes.find(n => n.id === tb.path[tb.path.length - 1]);
  if (tb.cluster && !(last && last.level === 'folder' && clusters.some(c => c.cluster_id === tb.cluster && c.folder_id === last.id))) tb.cluster = null;

  // columns: the start's children, then the children of every selected node; a folder's column lists its clusters
  let roots = kids(start);
  if (!start) roots = nodes.filter(n => !n.parent_id || !nodes.some(x => x.id === n.parent_id)).sort((a, b) => a.name.localeCompare(b.name));
  const cols = [{ items: roots.map(n => ({ kind: n.level, id: n.id, name: n.name, count: n.level === 'folder' ? clusters.filter(c => c.folder_id === n.id).length : kids(n.id).length })),
                  sel: tb.path[0], parent: start }];
  tb.path.forEach((id, i) => {
    const n = nodes.find(x => x.id === id);
    if (n.level === 'folder') {
      cols.push({ items: clusters.filter(c => c.folder_id === id).sort((a, b) => a.name.localeCompare(b.name)).map(c => ({ kind: 'cluster', id: c.cluster_id, name: c.name })),
                  sel: tb.cluster, parent: id, clusters: true });
    } else {
      cols.push({ items: kids(id).map(k => ({ kind: k.level, id: k.id, name: k.name, count: k.level === 'folder' ? clusters.filter(c => c.folder_id === k.id).length : kids(k.id).length })),
                  sel: tb.path[i + 1], parent: id });
    }
  });
  const childLevel = { msp: 'customer', customer: 'folder' };
  const colHtml = (c, i) => {
    const pn = nodes.find(x => x.id === c.parent);
    const addLevel = c.clusters ? null : (pn ? childLevel[pn.level] : (start ? 'folder' : 'msp'));
    const mayAdd = addLevel && (pn ? canNode('tree.manage', pn.id) : (start ? canNode('tree.manage', start) : window.HCM_ACCESS.root_admin));
    return `<div class="hcm-tb-col">
      ${c.items.map(it => `<div class="hcm-tb-item${it.id === c.sel ? ' sel' : ''}" onclick="acTbPick(${i}, '${it.kind}', '${it.id}')" title="${_esc(it.name)}">
          ${_acIcon(it.kind)}<span class="hcm-tb-name">${_esc(it.name)}</span>
          ${it.kind !== 'cluster' ? `<span class="hcm-tb-count">${it.count || ''}</span><span class="hcm-tb-chev">›</span>` : ''}</div>`).join('')
        || `<div class="hcm-tb-empty">${_esc(t(c.clusters ? 'acc.tb_no_clusters' : 'acc.tb_empty'))}</div>`}
      ${mayAdd ? `<button class="hcm-tb-add" onclick="acNewNode('${addLevel}', ${c.parent ? `'${c.parent}'` : 'null'})">+ ${_esc(_acLevelLabel(addLevel))}</button>` : ''}
    </div>`;
  };
  body.innerHTML = `
    <div class="um-toolbar"><span class="um-hint" style="flex:1">${_esc(t('acc.tree_intro'))}</span></div>
    <div class="hcm-tb">
      <div class="hcm-tb-cols" id="acTbCols">${cols.map(colHtml).join('')}</div>
      <div class="hcm-tb-pane" id="acTbPane"></div>
    </div>`;
  const strip = document.getElementById('acTbCols');
  strip.scrollLeft = strip.scrollWidth;
  _acTbPane();
}

function acTbPick(col, kind, id) {
  const tb = _ac.tb;
  if (kind === 'cluster') { tb.cluster = id; }
  else { tb.path = tb.path.slice(0, col).concat(id); tb.cluster = null; }
  _acRenderTree();
}

async function _acTbPane() {
  const pane = document.getElementById('acTbPane');
  if (!pane) return;
  const tb = _ac.tb;
  const nodes = _ac.data.nodes || [];
  const nodeId = tb.path[tb.path.length - 1];
  const n = nodes.find(x => x.id === nodeId);
  const c = tb.cluster && (_ac.data.clusters || []).find(x => x.cluster_id === tb.cluster);
  if (!n && !c) { pane.innerHTML = `<div class="hcm-tb-hint">${_esc(t('acc.tb_pick'))}</div>`; return; }
  const kind = c ? 'cluster' : n.level;
  const name = c ? c.name : n.name;
  const crumbs = c ? `${_acNodePath(c.folder_id)} / ${c.name}` : _acNodePath(n.id);
  const childLevel = { msp: 'customer', customer: 'folder' };
  const actions = c ? '' : [
    canNode('tree.manage', n.id) ? `<button class="um-action-btn" onclick="acRenameNode('${n.id}')">${_esc(t('folders.rename'))}</button>` : '',
    canNode('tree.manage', n.parent_id) && (n.parent_id || window.HCM_ACCESS.root_admin) ? `<button class="um-action-btn danger" onclick="acDeleteNode('${n.id}')">${_esc(t('users.delete'))}</button>` : '',
  ].join('');
  const mayAssign = c ? (can('users.manage', c.cluster_id) || canNode('users.manage', c.folder_id)) : canNode('users.manage', n.id);
  pane.innerHTML = `
    <div class="hcm-tb-head">${_acIcon(kind)}<div style="min-width:0;flex:1">
        <div class="hcm-tb-title">${_esc(name)}</div>
        <div class="hcm-tb-crumbs"><span class="hcm-tree-level">${_esc(kind === 'cluster' ? t('acc.scope_cluster') : _acLevelLabel(kind))}</span> ${_esc(crumbs)}</div></div>
      <div class="hcm-tb-actions">${actions}</div></div>
    ${mayAssign ? `<button class="um-primary-btn" style="align-self:flex-start" onclick="acTbAssign()">+ ${_esc(t('acc.tb_assign'))}</button>` : ''}
    <div id="acTbList">${mayAssign ? `<div class="loading"><div class="spinner"></div></div>` : `<div class="hcm-tb-hint">${_esc(t('acc.tb_no_manage'))}</div>`}</div>`;
  if (!mayAssign) return;
  const want = c ? `cluster=${encodeURIComponent(c.cluster_id)}` : `node=${encodeURIComponent(n.id)}`;
  const d = await apiFetch(`/api/access/at?${want}`);
  const box = document.getElementById('acTbList');
  if (!box || (_ac.tb.cluster || null) !== (c ? c.cluster_id : null) || _ac.tb.path[_ac.tb.path.length - 1] !== nodeId) return;   // moved on meanwhile
  if (!d) { box.innerHTML = `<div class="hcm-tb-hint">${_esc(t('acc.who_failed'))}</div>`; return; }
  const me = _accMe();
  const line = (a, here) => {
    const isGroup = !!a.group_id;
    const who = isGroup ? a.group_name : a.username;
    const open = isGroup ? `acOpenGroup('${a.group_id}')` : `acOpenUser('${a.user_id}')`;
    const manageable = isGroup ? (_ac.data.groups || []).some(g => g.group_id === a.group_id) : (_ac.data.users || []).some(u => u.user_id === a.user_id);
    const role = a.builtin_key ? t('acc.role_' + a.builtin_key) : a.role_name;
    const where = here ? (a.scope_kind === 'tag' ? t('acc.tb_on_tag', { tag: a.tag }) : a.scope_kind === 'vm' ? t('acc.tb_on_vm', { vm: a.vm_name }) : '')
                       : (a.scope_kind === 'root' ? t('acc.whole_install') : a.scope_label);
    return `<div class="hcm-tb-who${here ? '' : ' inherited'}">
      ${_acIcon(isGroup ? 'group' : 'user')}
      <div class="hcm-tb-who-name">${manageable ? `<a href="#" onclick="event.preventDefault(); ${open}">${_esc(who)}</a>` : _esc(who)}
        ${isGroup && a.members != null ? `<span class="um-hint">${_esc(t('acc.tb_members', { n: a.members }))}</span>` : ''}
        ${where ? `<div class="um-hint">${_esc(where)}</div>` : ''}</div>
      <span class="hcm-tb-role">${_esc(role)}</span>
      ${here && !(a.username && a.username === me.username) ? `<button class="hcm-tb-x" title="${_esc(t('acc.remove'))}" onclick="acTbRemove('${a.assignment_id}')">×</button>` : '<span class="hcm-tb-x-pad"></span>'}
    </div>`;
  };
  const sortRows = rows => rows.slice().sort((a, b) => (!!b.group_id - !!a.group_id) || (a.group_name || a.username || '').localeCompare(b.group_name || b.username || ''));
  box.innerHTML = `
    <div class="um-section-title">${_esc(t('acc.tb_here'))} <span class="um-hint">${d.here.length || ''}</span></div>
    ${d.here.length ? sortRows(d.here).map(a => line(a, true)).join('') : `<div class="hcm-tb-hint">${_esc(t('acc.tb_nobody_here'))}</div>`}
    ${d.above.length ? `<div class="um-section-title" style="margin-top:16px">${_esc(t('acc.tb_above'))} <span class="um-hint">${d.above.length}</span></div>
      ${d.above.map(a => line(a, false)).join('')}` : ''}`;
}

async function acTbRemove(aid) {
  if (!await hcmConfirm(t('acc.remove_assignment_confirm'), { danger: true, confirmLabel: t('acc.remove') })) return;
  try { await _acCall('DELETE', `/api/access/assignments/${aid}`); await _acLoad(); _acTbPane(); } catch (e) { hcmAlert(e.message); }
}

/** Assign a user or group on the selected layer: the scope is preset to it (a cluster also offers its tags and VMs). */
function acTbAssign() {
  const tb = _ac.tb;
  const nodeId = tb.path[tb.path.length - 1];
  const preset = tb.cluster ? { kind: 'cluster', cluster_id: tb.cluster } : { kind: 'node', node_id: nodeId };
  const users = (_ac.data.users || []).filter(u => u.username !== _accMe().username).sort((a, b) => a.username.localeCompare(b.username));
  const groups = (_ac.data.groups || []).slice().sort((a, b) => a.name.localeCompare(b.name));
  const label = tb.cluster ? ((_ac.data.clusters || []).find(c => c.cluster_id === tb.cluster) || {}).name : _acNodePath(nodeId);
  const m = hcmModal({
    title: `${_esc(t('acc.tb_assign'))} · ${_esc(label || '')}`, width: 'min(820px, 96vw)',
    body: `<label class="hcm-field"><span>${_esc(t('acc.who'))}</span><select class="um-select" id="tbWho">
        ${groups.length ? `<optgroup label="${_esc(t('acc.tab_groups'))}">${groups.map(g => `<option value="g:${g.group_id}">${_esc(g.name)}</option>`).join('')}</optgroup>` : ''}
        ${users.length ? `<optgroup label="${_esc(t('acc.tab_users'))}">${users.map(u => `<option value="u:${u.user_id}">${_esc(u.username)}</option>`).join('')}</optgroup>` : ''}
      </select></label>
      <div style="margin-top:10px">${_acAssignForm('tb', preset)}</div>`,
    buttons: [{ label: t('acc.close'), onClick: h => h.close() }],
  });
  // only this layer's scopes: a node fixes the node, a cluster offers cluster, tag or VM on it
  const $ = id => document.getElementById('tb' + id);
  [...$('Kind').options].forEach(o => { if (tb.cluster ? !['cluster', 'tag', 'vm'].includes(o.value) : o.value !== 'node') o.remove(); });
  $('Node').disabled = true; $('Cluster').disabled = true;
  _acAssignWire('tb', {}, m, () => {});
  $('Add').onclick = async () => {
    const v = $('Who').value;
    if (!v) return;
    const principal = v.startsWith('u:') ? { user_id: v.slice(2) } : { group_id: v.slice(2) };
    const k = $('Kind').value;
    const scope = { kind: k };
    if (k === 'node') scope.node_id = nodeId;
    if (['cluster', 'tag', 'vm'].includes(k)) scope.cluster_id = tb.cluster;
    if (k === 'tag') scope.tag = $('Target').value;
    if (k === 'vm') { scope.vm_uuid = $('Target').value; scope.vm_name = $('Target').selectedOptions[0]?.textContent; }
    try { await _acCall('POST', '/api/access/assignments', { ...principal, role_id: $('Role').value, scope }); m.close(); await _acLoad(); _acTbPane(); }
    catch (e) { m.setMsg(e.message); }
  };
}

async function acNewNode(level, parent) {
  const name = await hcmPrompt(t('acc.new_node_name', { level: _acLevelLabel(level) }), { title: `+ ${_acLevelLabel(level)}` });
  if (!name) return;
  try { await _acCall('POST', '/api/access/nodes', { name, level, parent_id: parent }); await _acLoad(); renderAccessPanel(); loadClusters(); }
  catch (e) { hcmAlert(e.message); }
}
async function acRenameNode(id) {
  const n = (_ac.data.nodes || []).find(x => x.id === id);
  const name = await hcmPrompt(t('folders.rename_prompt'), { value: n ? n.name : '' });
  if (!name || (n && name === n.name)) return;
  try { await _acCall('PUT', `/api/access/nodes/${id}`, { name }); await _acLoad(); renderAccessPanel(); loadClusters(); } catch (e) { hcmAlert(e.message); }
}
async function acDeleteNode(id) {
  const n = (_ac.data.nodes || []).find(x => x.id === id);
  if (!await hcmConfirm(t('acc.delete_node_confirm', { name: n ? n.name : '' }), { danger: true, confirmLabel: t('users.delete'), typeToConfirm: n ? n.name : null })) return;
  try {
    const d = await _acCall('DELETE', `/api/access/nodes/${id}`);
    await _acLoad(); renderAccessPanel(); loadClusters();
    if ((d.unfiled_clusters || []).length)
      hcmAlert(t('acc.node_deleted_effects', { clusters: d.unfiled_clusters.join(', ') }));
  } catch (e) { hcmAlert(e.message); }
}

// ── audit ───────────────────────────────────────────────────────────────────
async function _acRenderAudit() {
  const body = document.getElementById('acBody');
  body.innerHTML = `<div class="loading"><div class="spinner"></div></div>`;
  const d = await apiFetch('/api/access/audit');
  const rows = (d && d.rows) || [];
  body.innerHTML = rows.length ? `<table class="um-table"><thead><tr><th>${_esc(t('acc.col_when'))}</th><th>${_esc(t('acc.col_who'))}</th>
      <th>${_esc(t('acc.col_action'))}</th><th>${_esc(t('acc.col_what'))}</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td class="um-hint" style="white-space:nowrap">${_esc(new Date(r.at).toLocaleString())}</td><td>${r.actor_above ? `<span class="um-hint">${_esc(t('acc.actor_above'))}</span>` : _esc(r.actor || '')}</td>
      <td><code>${_esc(r.action)}</code></td><td>${_esc(r.target || '')}${r.detail ? `<div class="um-hint">${_esc(_acDetail(r.detail))}</div>` : ''}</td></tr>`).join('')}
    </tbody></table>` : `<div class="um-hint" style="padding:10px 0">${_esc(t('acc.audit_empty'))}</div>`;
}
function _acDetail(d) {
  return Object.entries(d || {}).filter(([, v]) => v !== null && !(Array.isArray(v) && !v.length)).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : (typeof v === 'object' ? JSON.stringify(v) : v)}`).join(' · ');
}

// ── who has access (a cluster or one VM) ────────────────────────────────────
async function acWho(clusterId, vmUuid, vmName) {
  if (!_ac.data) await _acLoad();
  const d = await apiFetch(`/api/access/who?cluster=${encodeURIComponent(clusterId)}${vmUuid ? '&vm=' + encodeURIComponent(vmUuid) : ''}`);
  if (!d) { hcmAlert(t('acc.who_failed')); return; }
  const c = (CLUSTERS || []).find(x => x.cluster_id === clusterId);
  const label = vmUuid ? (vmName || vmUuid) : (c ? c.name : clusterId);
  const rows = (d.users || []).map(u => `<tr><td class="um-username">${_esc(u.username)}${u.enabled ? '' : ` <span class="um-badge hcm-badge-off">${_esc(t('acc.state_disabled'))}</span>`}</td>
      <td>${u.via.map(v => `<div>${_esc(v.role)} · ${_esc(v.scope)}${v.group ? ` <span class="um-hint">(${_esc(t('acc.via_group', { name: v.group }))})</span>` : ''}</div>`).join('')}</td>
      <td class="um-hint" style="max-width:340px">${_esc(u.permissions.map(p => t('perm.' + p)).join(', '))}</td></tr>`).join('');
  const m = hcmModal({
    title: `${_esc(t('acc.who_title'))} · ${_esc(label)}`, width: 'min(1000px, 96vw)',
    body: `${vmUuid && (d.tags || []).length ? `<div class="um-hint">${_esc(t('acc.who_tags', { tags: d.tags.join(', ') }))}</div>` : ''}
      ${rows ? `<table class="um-table"><thead><tr><th>${_esc(t('acc.col_email'))}</th><th>${_esc(t('acc.who_via'))}</th><th>${_esc(t('acc.col_permissions'))}</th></tr></thead><tbody>${rows}</tbody></table>`
             : `<div class="um-hint">${_esc(t('acc.who_nobody'))}</div>`}`,
    buttons: [
      ...(vmUuid ? [{ label: t('acc.copy_from'), onClick: () => acCopyVmAccess(clusterId, vmUuid, vmName) }] : []),
      { label: t('acc.close'), kind: 'primary', onClick: h => h.close() },
    ],
  });
  return m;
}

/** A restored or cloned VM has a new UUID: give it the single-VM access of the VM it came from. */
async function acCopyVmAccess(clusterId, toVm, toName) {
  const d = await apiFetch(`/api/vm-index?cluster=${encodeURIComponent(clusterId)}`);
  const vms = ((d && d.vms) || []).filter(v => v.uuid !== toVm).sort((a, b) => a.name.localeCompare(b.name));
  hcmModal({
    title: `${_esc(t('acc.copy_from'))} · ${_esc(toName || toVm)}`, width: 'min(560px, 96vw)',
    body: `<p class="hcm-text">${_esc(t('acc.copy_from_intro'))}</p>
      <label class="hcm-field"><span>${_esc(t('acc.copy_from_vm'))}</span><select class="um-select" id="cpFrom">
        ${vms.map(v => `<option value="${v.uuid}">${_esc(v.name)}</option>`).join('')}</select></label>`,
    buttons: [{ label: t('console.cancel'), onClick: h => h.close() },
      { label: t('acc.copy'), kind: 'primary', onClick: async h => {
        try {
          const r = await _acCall('POST', '/api/access/copy-vm-access', { cluster_id: clusterId, from_vm: document.getElementById('cpFrom').value, to_vm: toVm });
          h.close();
          _hcmModals[_hcmModals.length - 1]?.close();
          await hcmAlert(t('acc.copy_done', { copied: r.copied.join(', ') || '—', skipped: r.skipped.join(', ') || '—' }));
          acWho(clusterId, toVm, toName);
        } catch (e) { h.setMsg(e.message); }
      } }],
  });
}

// ── the folder menu's "Access…" (assign on that folder) ─────────────────────
async function acFolderAccess(folderId) {
  if (typeof _closeMenus === 'function') _closeMenus();
  await _acLoad();
  const f = (_ac.data.nodes || []).find(n => n.id === folderId);
  const users = _ac.data.users || [];
  const groups = _ac.data.groups || [];
  const m = hcmModal({
    title: `${_esc(t('acc.access_btn'))} · ${_esc(f ? f.name : '')}`, width: 'min(900px, 96vw)',
    body: `<p class="hcm-text">${_esc(t('acc.folder_access_intro'))}</p>
      <label class="hcm-field"><span>${_esc(t('acc.who'))}</span><select class="um-select" id="faWho">
        ${users.map(u => `<option value="u:${u.user_id}">${_esc(u.username)}</option>`).join('')}
        ${groups.map(g => `<option value="g:${g.group_id}">${_esc(t('acc.group'))}: ${_esc(g.name)}</option>`).join('')}</select></label>
      ${_acAssignForm('fa', { kind: 'node', node_id: folderId })}
      <div id="faWhoList" style="margin-top:14px"></div>`,
    buttons: [{ label: t('acc.close'), kind: 'primary', onClick: h => h.close() }],
  });
  const principal = () => { const v = document.getElementById('faWho').value; return v.startsWith('u:') ? { user_id: v.slice(2) } : { group_id: v.slice(2) }; };
  const list = async () => {
    const cs = (_ac.data.clusters || []).filter(c => c.folder_id === folderId);
    const box = document.getElementById('faWhoList');
    if (!cs.length) { box.innerHTML = `<div class="um-hint">${_esc(t('acc.folder_no_clusters'))}</div>`; return; }
    const d = await apiFetch(`/api/access/who?cluster=${encodeURIComponent(cs[0].cluster_id)}`);
    box.innerHTML = `<div class="um-section-title">${_esc(t('acc.who_title'))} · ${_esc(cs[0].name)}</div>` + ((d && d.users || []).map(u =>
      `<div class="hcm-who-line"><b>${_esc(u.username)}</b> — ${u.via.map(v => _esc(`${v.role} · ${v.scope}`)).join('; ')}</div>`).join('') || `<div class="um-hint">${_esc(t('acc.who_nobody'))}</div>`);
  };
  const p = 'fa';
  const add = document.getElementById(p + 'Add');
  _acAssignWire(p, {}, m, list);
  add.onclick = async () => {
    const $ = id => document.getElementById(p + id);
    const k = $('Kind').value;
    const scope = { kind: k };
    if (k === 'node') scope.node_id = $('Node').value;
    if (['cluster', 'tag', 'vm'].includes(k)) scope.cluster_id = $('Cluster').value;
    if (k === 'tag') scope.tag = $('Target').value;
    if (k === 'vm') scope.vm_uuid = $('Target').value;
    try { await _acCall('POST', '/api/access/assignments', { ...principal(), role_id: $('Role').value, scope }); m.setMsg(t('acc.saved'), 'ok'); await _acLoad(); list(); }
    catch (e) { m.setMsg(e.message); }
  };
  list();
}

// ── boot ────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  window._accMeCache = await hcmLoadAccess();
  setTimeout(hcmCheckPending, 600);
});

// ───────────────────────────────────────────── guest console access (R4) ──
// One-time console access for an outsider: link + code, one VM, a time window, nothing else.
function _gcDt(d) { const p = n => String(n).padStart(2, '0'); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`; }
/** A name on a grant, or "an administrator above your level" when the server held it back. */
function _gcWho(g, f) { return (g.hidden || []).includes(f) ? t('acc.actor_above') : (g[f] || '—'); }
function _gcFmt(iso) { return iso ? new Date(iso).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' }) : '—'; }

window.acRenderGuest = async function () {
  const body = document.getElementById('acBody');
  body.innerHTML = `<div class="loading"><div class="spinner"></div></div>`;
  const d = await apiFetch('/api/guest/grants');
  const rows = (d && d.grants) || [];
  const mayGrant = canAnyVmPerm('guest_console.grant');
  if (d && (d.pending || 0) !== (_ac.guestPending || 0)) { _ac.guestPending = d.pending || 0; renderAccessPanel(); return; }
  const badge = g => {
    const s = g.expired && ['active', 'locked'].includes(g.status) ? 'expired' : g.status;
    const cls = { requested: 'hcm-badge-warn', active: 'um-badge-otp-on', locked: 'hcm-badge-warn', expired: 'hcm-badge-off', revoked: 'hcm-badge-off', rejected: 'hcm-badge-off' }[s] || '';
    return `<span class="um-badge ${cls}">${_esc(t('gc.st_' + s))}</span>`;
  };
  const acts = g => {
    if (!g.can_grant) return '';
    const b = [];
    if (g.status === 'requested') b.push(`<button class="um-primary-btn" onclick="gcApprove(${g.id})">${_esc(t('gc.approve'))}</button>`,
                                         `<button class="um-action-btn danger" onclick="gcReject(${g.id})">${_esc(t('gc.reject'))}</button>`);
    if (['active', 'locked'].includes(g.status) && !g.expired) b.push(`<button class="um-action-btn" onclick="gcReissue(${g.id})">${_esc(t('gc.new_code'))}</button>`,
                                                                       `<button class="um-action-btn danger" onclick="gcRevoke(${g.id})">${_esc(t('gc.revoke'))}</button>`);
    if ((g.live || []).some(s => s.source === 'guest')) b.push(`<button class="um-action-btn" onclick="window.open('/guest-watch/${g.id}', '_blank', 'noopener')">${_esc(t('gc.watch'))}</button>`,
                                                               `<button class="um-action-btn danger" onclick="gcEnd(${g.id})">${_esc(t('gc.end'))}</button>`);
    return b.join(' ');
  };
  body.innerHTML = `
    <div class="um-toolbar"><span class="um-hint" style="flex:1">${_esc(t('gc.intro'))}</span>
      ${mayGrant ? `<button class="um-primary-btn" onclick="gcForm('grant')">+ ${_esc(t('gc.give'))}</button>`
                 : `<button class="um-primary-btn" onclick="gcForm('request')">+ ${_esc(t('gc.ask'))}</button>`}</div>
    ${rows.length ? `<table class="um-table"><thead><tr><th>${_esc(t('gc.col_vm'))}</th><th>${_esc(t('gc.col_guest'))}</th><th>${_esc(t('gc.col_window'))}</th>
        <th>${_esc(t('acc.col_state'))}</th><th>${_esc(t('gc.col_sessions'))}</th><th></th></tr></thead><tbody>
      ${rows.map(g => `<tr>
        <td class="um-username">${_esc(g.vm_name || g.vm_uuid)}<div class="um-hint">${_esc(g.cluster_name || '')} · “${_esc(g.label)}”</div></td>
        <td>${_esc(g.guest_name)}${g.guest_company ? `<div class="um-hint">${_esc(g.guest_company)}</div>` : ''}${g.reason ? `<div class="um-hint">${_esc(g.reason)}</div>` : ''}</td>
        <td class="um-hint" style="white-space:nowrap">${_esc(_gcFmt(g.valid_from))}<br>${_esc(_gcFmt(g.valid_until))}</td>
        <td>${badge(g)}${(g.live || []).some(s => s.source === 'guest') ? ` <span class="um-badge um-badge-admin">${_esc(t('gc.live'))}</span>` : ''}
            <div class="um-hint">${_esc(t('gc.by', { req: _gcWho(g, 'requested_by'), grant: _gcWho(g, 'granted_by') }))}</div></td>
        <td>${g.sessions_used} / ${g.max_sessions}<div class="um-hint">${g.max_minutes} min${g.allow_open_session ? ' · ' + _esc(t('gc.open_ok_short')) : ''}</div></td>
        <td style="white-space:nowrap">${acts(g)}</td></tr>`).join('')}</tbody></table>`
      : `<div class="um-hint" style="padding:10px 0">${_esc(t('gc.none'))}</div>`}`;
};

async function gcForm(kind, preset) {
  preset = preset || {};
  const perm = kind === 'grant' ? 'guest_console.grant' : 'guest_console.request';
  const clusters = (CLUSTERS || []).filter(c => c.enabled && canSome(perm, c.cluster_id));
  if (!clusters.length) { hcmAlert(t('gc.no_vms')); return; }
  const now = new Date(), later = new Date(now.getTime() + 4 * 3600e3);
  const m = hcmModal({
    title: _esc(kind === 'grant' ? t('gc.give') : t('gc.ask')), width: 'min(720px, 96vw)',
    body: `<div class="hcm-banner">${_esc(t('gc.warning'))}</div>
      <div class="hcm-grid2">
        <label class="hcm-field"><span>${_esc(t('acc.scope_cluster'))}</span><select class="um-select" id="gcCluster">
          ${clusters.map(c => `<option value="${c.cluster_id}" ${c.cluster_id === (preset.cluster_id || currentCluster) ? 'selected' : ''}>${_esc(c.name)}</option>`).join('')}</select></label>
        <label class="hcm-field"><span>${_esc(t('gc.col_vm'))}</span><select class="um-select" id="gcVm"></select></label>
        <label class="hcm-field"><span>${_esc(t('gc.guest_name'))}</span><input class="um-input" id="gcName" autocomplete="off"></label>
        <label class="hcm-field"><span>${_esc(t('gc.guest_company'))}</span><input class="um-input" id="gcCompany" autocomplete="off"></label>
        <label class="hcm-field"><span>${_esc(t('gc.from'))}</span><input class="um-input" type="datetime-local" id="gcFrom" value="${_gcDt(now)}"></label>
        <label class="hcm-field"><span>${_esc(t('gc.until'))}</span><input class="um-input" type="datetime-local" id="gcUntil" value="${_gcDt(later)}"></label>
        <label class="hcm-field"><span>${_esc(t('gc.max_minutes'))}</span><input class="um-input" type="number" id="gcMinutes" min="5" max="720" value="120"></label>
        <label class="hcm-field"><span>${_esc(t('gc.max_sessions'))}</span><input class="um-input" type="number" id="gcSessions" min="1" max="20" value="1"></label>
      </div>
      <label class="hcm-field" style="margin-top:10px"><span>${_esc(t('gc.label'))}</span><input class="um-input" id="gcLabel" value="${_esc(t('gc.label_default'))}" maxlength="60"></label>
      <div class="um-hint">${_esc(t('gc.label_hint'))}</div>
      <label class="hcm-field" style="margin-top:10px"><span>${_esc(t('gc.reason'))}</span><input class="um-input" id="gcReason" maxlength="500"></label>
      ${kind === 'grant' ? `<label class="hcm-perm" style="margin-top:12px"><input type="checkbox" id="gcOpen"> <span>${_esc(t('gc.allow_open'))}</span></label>
        <div class="um-hint">${_esc(t('gc.allow_open_hint'))}</div>` : ''}`,
    buttons: [{ label: t('console.cancel'), onClick: h => h.close() },
      { label: kind === 'grant' ? t('gc.create') : t('gc.send_request'), kind: 'primary', onClick: async h => {
        const iso = id => { const v = document.getElementById(id).value; return v ? new Date(v).toISOString() : null; };
        const body = { cluster_id: document.getElementById('gcCluster').value, vm_uuid: document.getElementById('gcVm').value,
          guest_name: document.getElementById('gcName').value, guest_company: document.getElementById('gcCompany').value,
          valid_from: iso('gcFrom'), valid_until: iso('gcUntil'), max_minutes: +document.getElementById('gcMinutes').value,
          max_sessions: +document.getElementById('gcSessions').value, label: document.getElementById('gcLabel').value,
          reason: document.getElementById('gcReason').value, allow_open_session: !!document.getElementById('gcOpen')?.checked };
        try {
          const r = await _acCall('POST', kind === 'grant' ? '/api/guest/grants' : '/api/guest/requests', body);
          h.close();
          if (kind === 'grant') _gcShowSecrets(r, body.guest_name); else hcmAlert(t('gc.request_sent'));
          if (_ac.tab === 'guest' && document.getElementById('acBody')) window.acRenderGuest();
        } catch (e) { h.setMsg(e.message); }
      } }],
  });
  const fillVms = async () => {
    const cid = document.getElementById('gcCluster').value;
    const d = await apiFetch(`/api/vm-index?cluster=${encodeURIComponent(cid)}`);
    const vms = ((d && d.vms) || []).filter(v => canVm(perm, cid, v.uuid, v.tags)).sort((a, b) => a.name.localeCompare(b.name));
    document.getElementById('gcVm').innerHTML = vms.map(v => `<option value="${v.uuid}" ${v.uuid === preset.vm_uuid ? 'selected' : ''}>${_esc(v.name)}${v.state && v.state !== 'RUNNING' ? ' (' + _esc(v.state.toLowerCase()) + ')' : ''}</option>`).join('')
      || `<option value="">${_esc(t('gc.no_vms'))}</option>`;
  };
  document.getElementById('gcCluster').onchange = fillVms;
  fillVms();
  return m;
}

function _gcShowSecrets(r, guest) {
  hcmModal({
    title: _esc(t('gc.secrets_title')), width: 'min(640px, 96vw)', closable: true,
    body: `<p class="hcm-text">${_esc(t('gc.secrets_intro', { guest: guest || '' }))}</p>
      <div class="um-section-title">${_esc(t('gc.link'))}</div>
      <div class="hcm-guest-link" id="gcLink">${_esc(r.link)}</div>
      <div style="margin:6px 0 14px"><button class="um-action-btn" onclick="navigator.clipboard.writeText(document.getElementById('gcLink').textContent)">${_esc(t('gc.copy_link'))}</button></div>
      <div class="um-section-title">${_esc(t('gc.code'))}</div>
      <div class="hcm-guest-code">${_esc(r.code)}</div>
      <p class="um-hint">${_esc(t('gc.secrets_once'))}</p>`,
    buttons: [{ label: t('gc.done'), kind: 'primary', onClick: h => h.close() }],
  });
}

async function gcApprove(id) {
  const ok = await hcmConfirm(t('gc.approve_confirm'), { confirmLabel: t('gc.approve') });
  if (!ok) return;
  try { const r = await _acCall('POST', `/api/guest/grants/${id}/approve`, {}); _gcShowSecrets(r); window.acRenderGuest(); } catch (e) { hcmAlert(e.message); }
}
async function gcReject(id) {
  if (!await hcmConfirm(t('gc.reject_confirm'), { danger: true, confirmLabel: t('gc.reject') })) return;
  try { await _acCall('POST', `/api/guest/grants/${id}/reject`, {}); window.acRenderGuest(); } catch (e) { hcmAlert(e.message); }
}
async function gcRevoke(id) {
  if (!await hcmConfirm(t('gc.revoke_confirm'), { danger: true, confirmLabel: t('gc.revoke') })) return;
  try { await _acCall('POST', `/api/guest/grants/${id}/revoke`, {}); window.acRenderGuest(); } catch (e) { hcmAlert(e.message); }
}
async function gcReissue(id) {
  if (!await hcmConfirm(t('gc.reissue_confirm'), { confirmLabel: t('gc.new_code') })) return;
  try { const r = await _acCall('POST', `/api/guest/grants/${id}/reissue`, {}); _gcShowSecrets(r); window.acRenderGuest(); } catch (e) { hcmAlert(e.message); }
}
async function gcEnd(id) {
  if (!await hcmConfirm(t('gc.end_confirm'), { danger: true, confirmLabel: t('gc.end') })) return;
  try { await _acCall('POST', `/api/guest/grants/${id}/end`, {}); setTimeout(window.acRenderGuest, 2500); } catch (e) { hcmAlert(e.message); }
}
