// Service worker: receives observed posts from content scripts, fetches
// scores from the SAIR backend (which dedupes against Mongo + Pangram), and
// broadcasts the result to all matching tabs so they can paint badges.
//
// chrome.storage.local is used ONLY for the participant token. Score results
// are not cached locally; the backend's `(platform, postId)` cache hits in
// ~2-3 ms, fast enough that local caching adds no perceivable value.

importScripts('config.js');

// Open the setup page in a tab the moment the extension is installed, so the
// participant doesn't have to discover the icon and click it. Fires only on
// fresh install (not on updates or browser restarts). If a Participant ID was
// somehow already saved (e.g., partial uninstall that kept storage), skip.
chrome.runtime.onInstalled.addListener(async (details) => {
  if (details.reason !== 'install') return;
  const r = await chrome.storage.local.get('sair_external_id');
  if (r['sair_external_id']) return;
  chrome.tabs.create({ url: chrome.runtime.getURL('popup/popup.html') });
});

const DEBUG = false;
const TOKEN_KEY = 'sair_token';        // { participantId, token, createdAt }
const EXT_ID_KEY = 'sair_external_id'; // string: user-entered recruitment-platform ID

const RATE_LIMIT_MS = 10;
const MIN_TEXT_LENGTH = 250;
const CONCURRENCY = 2;
const DEFAULT_PRIORITY = 50;
const TAB_MATCH_URLS = [
  'https://twitter.com/*',
  'https://x.com/*',
  'https://www.linkedin.com/*',
];

// Priority queue: postId -> { payload, priority }
const queue = new Map();
let activeWorkers = 0;
// Singleton promise so concurrent first-time callers share one /register call.
let tokenPromise = null;

// Count code points (not UTF-16 code units) to match Python's len() on the
// backend. Without this, posts with emoji can pass the extension's check but
// fail the backend's, producing a spurious 400 ERROR badge.
function codePointLength(s) {
  let n = 0;
  for (const _ of String(s || '')) n++;
  return n;
}

// Slim the detector to just the fields the in-page badge needs. Drops `text`,
// the `windows[]` body, `headline`, version, etc. — those are preserved on
// the backend and joined into the popup view via /observations.
function slimDetector(d) {
  if (!d || typeof d !== 'object') return null;
  const out = {};
  if (d.prediction_short) out.prediction_short = d.prediction_short;
  if (typeof d.fraction_ai === 'number') out.fraction_ai = d.fraction_ai;
  if (Array.isArray(d.windows)) out.windows_length = d.windows.length;
  else if (Array.isArray(d.sentence_scores)) out.windows_length = d.sentence_scores.length;
  return out;
}

// ────────────────────────── Message handling ──────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== 'object') return false;

  if (msg.type === 'POST_OBSERVED') {
    if (msg.payload && codePointLength(msg.payload.text) >= MIN_TEXT_LENGTH) {
      const priority = typeof msg.priority === 'number' ? msg.priority : DEFAULT_PRIORITY;
      queue.set(msg.payload.postId, { payload: msg.payload, priority });
      pump();
    }
    return false;
  }

  if (msg.type === 'POST_PRIORITY_BATCH') {
    for (const u of msg.updates || []) {
      const job = queue.get(u.postId);
      if (job && typeof u.priority === 'number') job.priority = u.priority;
    }
    return false;
  }

  if (msg.type === 'INTERACTION_OBSERVED') {
    // Fire and forget — interaction logging shouldn't block the page or the
    // participant's experience. If the backend's down or rate-limits us, we
    // drop the row rather than retrying.
    if (msg.payload && msg.payload.platform && msg.payload.postId && msg.payload.action) {
      recordInteraction(msg.payload).catch((e) => {
        if (DEBUG) console.error('[SAIR] interaction error:', e?.message || e);
      });
    }
    return false;
  }

  if (msg.type === 'GET_OBSERVATIONS') {
    fetchObservations().then((r) => sendResponse(r));
    return true;
  }

  if (msg.type === 'TEST_BACKEND') {
    testBackend().then((r) => sendResponse(r));
    return true;
  }

  if (msg.type === 'SETUP_PARTICIPANT') {
    // Popup writes externalId to storage, then asks us to register against
    // the backend. Force a fresh /register call so the participant doc gets
    // the externalId attached at creation time.
    tokenPromise = null;
    chrome.storage.local.remove(TOKEN_KEY).then(() => {
      ensureToken()
        .then((t) => {
          // Notify content scripts that enrollment is complete so they can begin scanning
          chrome.tabs.query({ url: TAB_MATCH_URLS }, (tabs) => {
            for (const tab of tabs) {
              chrome.tabs.sendMessage(tab.id, { type: 'ENROLLMENT_COMPLETE', group: t.group }, () => {
                void chrome.runtime.lastError;
              });
            }
          });
          sendResponse({ ok: true, participantId: t.participantId, group: t.group });
        })
        .catch((e) => sendResponse({ ok: false, error: String(e?.message || e) }));
    });
    return true;
  }

  if (msg.type === 'GET_GROUP') {
    chrome.storage.local.get(TOKEN_KEY).then((r) => {
      const tok = r[TOKEN_KEY];
      // A valid enrollment requires BOTH a token AND a group. If group is
      // missing (e.g. token from an old install before groups were stored),
      // report as unenrolled so the participant goes back through setup and
      // gets a proper server-assigned group. Never default to 'treatment' —
      // that would silently show badges to what might be a control-group user.
      const enrolled = !!(tok && tok.token && tok.group);
      sendResponse({
        enrolled,
        group: enrolled ? tok.group : null,
      });
    });
    return true;
  }

  return false;
});

// ────────────────────────── Worker pool ──────────────────────────

function pump() {
  while (activeWorkers < CONCURRENCY && queue.size > 0) {
    activeWorkers++;
    runWorker().finally(() => { activeWorkers--; });
  }
}

function dequeueHighest() {
  if (queue.size === 0) return null;
  let bestId = null;
  let bestPriority = -Infinity;
  for (const [id, job] of queue) {
    if (job.priority > bestPriority) {
      bestPriority = job.priority;
      bestId = id;
    }
  }
  const job = queue.get(bestId);
  queue.delete(bestId);
  return job;
}

async function runWorker() {
  while (queue.size > 0) {
    const job = dequeueHighest();
    if (!job) break;
    const post = job.payload;
    let record;
    try {
      const res = await scorePost(post);
      record = {
        platform: post.platform,
        postId: post.postId,
        detector: slimDetector(res.detector),
        error: null,
      };
      recordObservation(post).catch((e) => {
        if (DEBUG) console.error('[SAIR] observation error:', e?.message || e);
      });
    } catch (e) {
      const error = String(e?.message || e);
      console.error('[SAIR] score error:', error);
      record = {
        platform: post.platform,
        postId: post.postId,
        detector: null,
        error,
      };
    }
    broadcastScoreResult(record);
    await sleep(RATE_LIMIT_MS);
  }
}

// ────────────────────────── Tab broadcast ──────────────────────────

async function broadcastScoreResult(record) {
  // Include the participant's group so content scripts can suppress badges
  // for control-group users without a separate storage read.
  const tok = await chrome.storage.local.get(TOKEN_KEY);
  const group = (tok[TOKEN_KEY] && tok[TOKEN_KEY].group) || null;
  let tabs;
  try {
    tabs = await chrome.tabs.query({ url: TAB_MATCH_URLS });
  } catch (_) {
    return;
  }
  for (const tab of tabs) {
    if (!tab.id) continue;
    try {
      chrome.tabs.sendMessage(tab.id, { type: 'SCORE_RESULT', record, group }, () => {
        // Swallow lastError — tab might not have a content script attached yet.
        void chrome.runtime.lastError;
      });
    } catch (_) {
      // sendMessage can throw synchronously if the tab is invalid; ignore.
    }
  }
}

// ────────────────────────── Backend client ──────────────────────────

function backendUrl() {
  const cfg = self.SAIR_CONFIG || {};
  if (!cfg.backendUrl) throw new Error('BACKEND_URL missing — set it in .env and rebuild');
  return cfg.backendUrl;
}

async function ensureToken() {
  if (tokenPromise) return tokenPromise;
  tokenPromise = (async () => {
    const r = await chrome.storage.local.get([TOKEN_KEY, EXT_ID_KEY]);
    if (r[TOKEN_KEY] && r[TOKEN_KEY].token) return r[TOKEN_KEY];
    const externalId = (r[EXT_ID_KEY] || '').trim();
    if (!externalId) {
      throw new Error('Setup incomplete — open the extension popup and enter your Participant ID');
    }
    const res = await fetch(backendUrl() + '/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ externalId }),
    });
    if (!res.ok) {
      const body = await res.text().catch(() => '');
      throw new Error(`/register ${res.status}: ${body.slice(0, 200)}`);
    }
    const data = await res.json();
    await chrome.storage.local.set({ [TOKEN_KEY]: data });
    return data;
  })().catch((e) => {
    tokenPromise = null; // allow retry on next call
    throw e;
  });
  return tokenPromise;
}

async function backendFetch(path, init = {}) {
  const tok = await ensureToken();
  const headers = {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer ' + tok.token,
    ...(init.headers || {}),
  };
  let res;
  try {
    res = await fetch(backendUrl() + path, { ...init, headers });
  } catch (e) {
    throw new Error(`network error calling ${path}: ${e?.message || e}`);
  }
  const body = await res.text();
  let parsed;
  try { parsed = JSON.parse(body); } catch { parsed = { raw: body }; }
  if (!res.ok) {
    const msg = (parsed && (parsed.detail || parsed.message)) || body.slice(0, 200);
    throw new Error(`${path} ${res.status}: ${msg}`);
  }
  return parsed;
}

async function scorePost(post) {
  return backendFetch('/score', {
    method: 'POST',
    body: JSON.stringify({
      platform: post.platform,
      postId: post.postId,
      text: post.text,
    }),
  });
}

async function recordObservation(post) {
  return backendFetch('/observations', {
    method: 'POST',
    body: JSON.stringify({
      platform: post.platform,
      postId: post.postId,
      url: post.url || null,
      author: post.author || null,
      authorDisplayName: post.authorDisplayName || null,
      authorVerified: typeof post.authorVerified === 'boolean' ? post.authorVerified : null,
      authorVerifiedType: post.authorVerifiedType || null,
      authorPremium: typeof post.authorPremium === 'boolean' ? post.authorPremium : null,
      authorHeadline: post.authorHeadline || null,
      authorConnectionDegree: post.authorConnectionDegree || null,
      isPromoted: typeof post.isPromoted === 'boolean' ? post.isPromoted : null,
      postedAt: post.postedAt || null,
      postedAtRelative: post.postedAtRelative || null,
      engagement: post.engagement || null,
      seenAt: post.seenAt || null,
      badgeShown: typeof post.badgeShown === 'boolean' ? post.badgeShown : null,
    }),
  });
}

async function recordInteraction(payload) {
  return backendFetch('/interactions', {
    method: 'POST',
    body: JSON.stringify({
      platform: payload.platform,
      postId: payload.postId,
      action: payload.action,
      occurredAt: payload.occurredAt || null,
    }),
  });
}

async function fetchObservations() {
  try {
    const data = await backendFetch('/observations?limit=200');
    return { ok: true, ...data };
  } catch (e) {
    return { ok: false, error: String(e?.message || e), observations: [] };
  }
}

async function testBackend() {
  const out = { backendUrl: (self.SAIR_CONFIG || {}).backendUrl || '<missing>' };
  try {
    const healthRes = await fetch(backendUrl() + '/health');
    out.health = await healthRes.json().catch(() => ({ raw: 'non-JSON' }));
    const tok = await ensureToken();
    out.participantId = tok.participantId;
    const sample =
      'Artificial intelligence has rapidly transformed the way modern businesses operate, ' +
      'enabling unprecedented levels of automation, decision support, and personalized ' +
      'customer experiences across every industry vertical. Organizations are increasingly ' +
      'integrating AI into their core workflows, reshaping competitive landscapes and ' +
      'redefining what is possible at scale.';
    out.scoreResult = await backendFetch('/score', {
      method: 'POST',
      body: JSON.stringify({
        platform: 'twitter',
        postId: 'sair-test-' + Date.now(),
        text: sample,
      }),
    });
    out.ok = true;
  } catch (e) {
    out.ok = false;
    out.error = String(e?.message || e);
  }
  return out;
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
