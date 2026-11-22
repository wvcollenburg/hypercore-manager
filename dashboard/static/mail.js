// Settings → Mail: the installation's SMTP relay (plans/PLAN-roles.md, "Accounts", E2).
// Invitations, password resets and address changes go out through it; without it the person who sends
// an invitation gets the link to pass on. Administrators of the whole installation only.

async function loadMailPanel() {
  const el = document.getElementById('setpanel-mail');
  if (!el) return;
  const d = await apiFetch('/api/mail/settings');
  if (!d) { el.innerHTML = `<div class="empty-state">${_esc(t('empty.settings_failed'))}</div>`; return; }
  const sec = [['starttls', t('mail.sec_starttls')], ['tls', t('mail.sec_tls')], ['none', t('mail.sec_none')]];
  el.innerHTML = `
    <div class="settings-group">
      <div class="settings-group-header">
        <div class="chart-title-dot" style="background:var(--accent-cyan)"></div>
        <span class="settings-group-title">${_esc(t('mail.title'))}</span>
        <span style="flex:1"></span>
        <span class="um-badge ${d.configured ? 'um-badge-otp-on' : 'hcm-badge-off'}">${_esc(t(d.configured ? 'mail.state_on' : 'mail.state_off'))}</span>
      </div>
      <div class="um-hint" style="margin-bottom:12px">${_esc(t('mail.intro'))}</div>
      <div class="hcm-grid2">
        <label class="hcm-field"><span>${_esc(t('mail.host'))}</span><input class="um-input" id="mlHost" value="${_esc(d.host || '')}" placeholder="smtp.example.com" autocomplete="off"></label>
        <div class="hcm-grid2">
          <label class="hcm-field"><span>${_esc(t('mail.port'))}</span><input class="um-input" id="mlPort" type="number" min="1" max="65535" value="${_esc(d.port)}"></label>
          <label class="hcm-field"><span>${_esc(t('mail.security'))}</span><select class="um-select" id="mlSec" onchange="mailSecChanged()">
            ${sec.map(([k, l]) => `<option value="${k}" ${d.security === k ? 'selected' : ''}>${_esc(l)}</option>`).join('')}</select></label>
        </div>
        <label class="hcm-field"><span>${_esc(t('mail.username'))}</span><input class="um-input" id="mlUser" value="${_esc(d.username || '')}" autocomplete="off"></label>
        <label class="hcm-field"><span>${_esc(t('mail.password'))}</span><input class="um-input" id="mlPw" type="password" autocomplete="new-password"
          placeholder="${_esc(d.has_password ? t('mail.password_kept') : '')}"></label>
        <label class="hcm-field"><span>${_esc(t('mail.sender'))}</span><input class="um-input" id="mlSender" type="email" value="${_esc(d.sender || '')}" placeholder="hcm@example.com"></label>
        <label class="hcm-field"><span>${_esc(t('mail.sender_name'))}</span><input class="um-input" id="mlName" value="${_esc(d.sender_name || '')}" placeholder="HyperCore Manager"></label>
        <label class="hcm-field" style="grid-column:1/-1"><span>${_esc(t('mail.public_url'))}</span><input class="um-input" id="mlUrl" value="${_esc(d.public_url || '')}" placeholder="${_esc(d.suggested_public_url)}">
          <span class="um-hint">${_esc(t('mail.public_url_hint'))}</span></label>
        <label class="hcm-field" style="grid-column:1/-1"><span>${_esc(t('mail.guest_url'))}</span><input class="um-input" id="mlGuestUrl" value="${_esc(d.guest_url || '')}" placeholder="${_esc(d.env_guest_url || d.public_url || d.suggested_public_url)}">
          <span class="um-hint">${_esc(t('mail.guest_url_hint'))}</span></label>
      </div>
      <div id="mlSecWarn" class="um-hint hcm-badge-warn" style="margin-top:6px;${d.security === 'none' ? '' : 'display:none'}">${_esc(t('mail.sec_none_warn'))}</div>
      <div style="display:flex;gap:8px;align-items:center;margin-top:12px;flex-wrap:wrap">
        <button class="um-primary-btn" onclick="mailSave()">${_esc(t('acc.save'))}</button>
        ${d.has_password ? `<button class="um-action-btn" onclick="mailForgetPassword()">${_esc(t('mail.forget_password'))}</button>` : ''}
        <span style="flex:1"></span>
        <input class="um-input" id="mlTo" type="email" style="max-width:260px" placeholder="${_esc(t('mail.test_to'))}" value="${_esc((_accMe().username || '').includes('@') ? _accMe().username : '')}">
        <button class="um-action-btn" onclick="mailTest()" ${d.configured ? '' : 'disabled'}>${_esc(t('mail.test'))}</button>
      </div>
      <div id="mlMsg" class="um-hint" style="margin-top:8px"></div>
      ${d.updated_by ? `<div class="um-hint" style="margin-top:4px">${_esc(t('mail.updated', { who: d.updated_by, at: new Date(d.updated_at).toLocaleString(_hcmLocale()) }))}</div>` : ''}
    </div>`;
}

function mailSecChanged() {
  const s = document.getElementById('mlSec').value;
  const port = document.getElementById('mlPort');
  if (['587', '465', '25'].includes(port.value)) port.value = { starttls: 587, tls: 465, none: 25 }[s];
  document.getElementById('mlSecWarn').style.display = s === 'none' ? '' : 'none';
}

function _mailMsg(text, ok) {
  const m = document.getElementById('mlMsg');
  if (m) { m.textContent = text; m.style.color = ok ? 'var(--accent-green)' : 'var(--bad)'; }
}

async function mailSave(extra) {
  const v = id => document.getElementById(id).value;
  const body = { host: v('mlHost'), port: Number(v('mlPort')), security: v('mlSec'), username: v('mlUser'),
                 sender: v('mlSender'), sender_name: v('mlName'), public_url: v('mlUrl'), guest_url: v('mlGuestUrl'), ...(extra || {}) };
  if (v('mlPw')) body.password = v('mlPw');
  try { await _acCall('PUT', '/api/mail/settings', body); await loadMailPanel(); _mailMsg(t('acc.saved'), true); }
  catch (e) { _mailMsg(e.message, false); }
}

async function mailForgetPassword() { await mailSave({ password: '' }); }

async function mailTest() {
  _mailMsg(t('mail.testing'), true);
  try {
    await _acCall('POST', '/api/mail/test', { to: document.getElementById('mlTo').value, lang: window._hcmLang || 'en' });
    _mailMsg(t('mail.test_sent', { to: document.getElementById('mlTo').value }), true);
  } catch (e) { _mailMsg(t('mail.test_failed', { error: e.message }), false); }
}
