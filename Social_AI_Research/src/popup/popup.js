const $ = (s) => document.querySelector(s);

const EXT_ID_KEY = 'sair_external_id';

function showSetup() {
  $('#setup-view').classList.remove('hidden');
  $('#main-view').classList.add('hidden');
  setTimeout(() => $('#setup-input').focus(), 0);
}

function showMain() {
  $('#setup-view').classList.add('hidden');
  $('#thanks-view').classList.add('hidden');
  $('#main-view').classList.remove('hidden');
}

function showThanks() {
  $('#setup-view').classList.add('hidden');
  $('#main-view').classList.add('hidden');
  $('#thanks-view').classList.remove('hidden');
}

// chrome.tabs.getCurrent() returns the Tab object when running inside a tab
// (the auto-opened setup page), or undefined when running inside the action
// popup. Lets us pick the right post-setup view.
async function isRunningInTab() {
  try {
    if (typeof chrome === 'undefined' || !chrome.tabs) {
      document.body.classList.add('is-tab');
      return true; // Force tab view for local HTML testing
    }
    const tab = await chrome.tabs.getCurrent();
    const inTab = !!tab;
    if (inTab) document.body.classList.add('is-tab');
    return inTab;
  } catch (_) {
    return false;
  }
}

async function getStoredExternalId() {
  if (typeof chrome === 'undefined' || !chrome.storage || !chrome.storage.local) {
    return '';
  }
  const r = await chrome.storage.local.get(EXT_ID_KEY);
  return (r[EXT_ID_KEY] || '').trim();
}

async function submitSetup() {
  const input = $('#setup-input');
  const errBox = $('#setup-error');
  const btn = $('#setup-submit');
  const value = input.value.trim();
  errBox.textContent = '';
  if (!value) {
    errBox.textContent = 'Participant ID is required.';
    input.focus();
    return;
  }
  btn.disabled = true;
  btn.textContent = 'Linking…';
  try {
    await chrome.storage.local.set({ [EXT_ID_KEY]: value });
    const res = await new Promise((resolve) =>
      chrome.runtime.sendMessage({ type: 'SETUP_PARTICIPANT' }, resolve)
    );
    if (!res || !res.ok) {
      throw new Error((res && res.error) || 'unknown error');
    }
    // In the auto-opened tab: show a thanks page and stop here so we don't
    // pull data the participant didn't ask to see. In the popup dropdown:
    // flip to the normal observations view as before.
    if (await isRunningInTab()) {
      showThanks();
    } else {
      showMain();
      load();
      startAutoRefresh();
    }
  } catch (e) {
    errBox.textContent = `Could not link: ${e?.message || e}. Check the ID and try again.`;
    btn.disabled = false;
    btn.textContent = 'Continue';
  }
}

function fmtNum(n) {
  if (n == null) return '—';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

// Provider-agnostic — mirrors tierFromDetector in the content scripts so the
// popup row matches the in-page badge.
function classifyScore(rec) {
  if (rec.error) return { state: 'err', dot: '', label: '', value: 'error', tooltip: rec.error };
  const d = rec.detector;
  if (!d) return { state: 'pending', dot: 'pending', label: '', value: '…', tooltip: '' };

  let tier = null;
  if (d.prediction_short) {
    const s = String(d.prediction_short).toLowerCase();
    if (s === 'human') tier = 'human';
    else if (s === 'ai') tier = 'ai';
    else if (s.includes('mixed') || s.includes('assist')) tier = 'mixed';
  }

  let pct = null;
  if (typeof d.fraction_ai === 'number') pct = d.fraction_ai;
  else if (typeof d.score === 'number') pct = d.score;

  if (tier == null && pct != null) {
    if (pct >= 0.7) tier = 'ai';
    else if (pct >= 0.3) tier = 'mixed';
    else tier = 'human';
  }

  if (tier == null) return { state: 'ok', dot: '', label: '', value: 'ok', tooltip: '' };

  const label = tier === 'ai' ? 'AI' : tier === 'mixed' ? 'Mixed' : 'Human';
  const value = pct != null ? Math.round(pct * 100) + '%' : label;
  const segs = Array.isArray(d.windows) ? d.windows.length
    : Array.isArray(d.sentence_scores) ? d.sentence_scores.length
    : 0;
  const tooltip = segs ? `${segs} segment${segs === 1 ? '' : 's'} scored` : '';

  return { state: 'ok', dot: tier, label, value, tooltip };
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function fetchObservations() {
  return new Promise((res) => chrome.runtime.sendMessage({ type: 'GET_OBSERVATIONS' }, res));
}

function platLabel(p) {
  if (p === 'twitter') return 'X';
  if (p === 'linkedin') return 'LinkedIn';
  return p || '?';
}

function render(result) {
  if (!result || result.ok === false) {
    $('#stats').textContent = `Backend unreachable: ${(result && result.error) || '—'}`;
    $('#list').innerHTML = '<li class="empty">Could not reach the SAIR backend.<br>Check that uvicorn is running and BACKEND_URL is correct in .env.</li>';
    return;
  }

  const list = result.observations || [];
  const total = list.length;
  const scored = list.filter((r) => r.detector).length;
  const errs = list.filter((r) => r.error).length;
  const tw = list.filter((r) => r.platform === 'twitter').length;
  const li = list.filter((r) => r.platform === 'linkedin').length;
  const userTag = result.userId ? ` · user=${result.userId.slice(0, 12)}…` : '';
  $('#stats').textContent = `${total} observations · ${scored} scored · ${errs} errors · ${tw} X · ${li} LinkedIn${userTag}`;

  if (!total) {
    $('#list').innerHTML = '<li class="empty">No observations recorded yet.<br>Open x.com or linkedin.com and scroll your feed.</li>';
    return;
  }

  // Backend returns observations newest-first (sorted by recordedAt desc).
  const recent = list.slice(0, 50);
  $('#list').innerHTML = recent.map((r) => {
    const e = r.engagement || {};
    const eng = r.platform === 'twitter'
      ? `${fmtNum(e.likes)} likes · ${fmtNum(e.retweets)} reposts · ${fmtNum(e.replies)} replies · ${fmtNum(e.views)} views`
      : `${fmtNum(e.likes)} likes · ${fmtNum(e.comments)} comments · ${fmtNum(e.reposts)} reposts`;
    const text = (r.text || '').trim();
    // Prefer human-readable display name when extracted; fall back to the
    // platform handle/slug so older rows still show something.
    const author = r.authorDisplayName
      ? r.authorDisplayName
      : r.author
        ? (r.platform === 'twitter' ? '@' + r.author : r.author)
        : '';
    const s = classifyScore(r);
    const scoreClass = s.state === 'err' ? 'score err' : s.state === 'pending' ? 'score pending' : 'score';
    const tooltipAttr = s.tooltip ? ` title="${escapeHtml(s.tooltip)}"` : '';
    const labelTag = s.label ? `<span class="label">${escapeHtml(s.label)}</span>` : '';
    const errLine = r.error ? `<div class="err-line">${escapeHtml(r.error)}</div>` : '';
    return `<li class="item">
      <div class="row">
        <span class="plat">${platLabel(r.platform)}</span>
        <span class="author">${escapeHtml(author)}</span>
        <span class="score-wrap"${tooltipAttr}>
          ${labelTag}
          <span class="dot ${s.dot}"></span>
          <span class="${scoreClass}">${escapeHtml(s.value)}</span>
        </span>
      </div>
      <div class="text">${escapeHtml(text)}</div>
      <div class="eng">${eng}</div>
      ${errLine}
    </li>`;
  }).join('');
}

async function load() {
  const result = await fetchObservations();
  render(result);
}

let refreshTimer = null;
function startAutoRefresh() {
  if (refreshTimer) return;
  refreshTimer = setInterval(load, 2500);
}
window.addEventListener('unload', () => { if (refreshTimer) clearInterval(refreshTimer); });

$('#setup-submit').addEventListener('click', submitSetup);
$('#setup-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') submitSetup();
});

(async () => {
  const externalId = await getStoredExternalId();
  if (!externalId) {
    showSetup();
    return;
  }
  showMain();
  load();
  startAutoRefresh();
})();
