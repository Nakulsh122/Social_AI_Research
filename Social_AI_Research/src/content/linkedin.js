(function () {
  const { parseCount, hashString, sendPost, makeThrottledScanner, computePriorityNow, createViewportTracker } = window.SAIR;
  const seen = new Set();
  const DEBUG = false;
  const MIN_FOR_SCORING = 250;
  const tracker = createViewportTracker();

  // Control-group participants see no badges at all. We also wait until
  // enrollment is fully complete before scanning to prevent "ERROR" badges.
  let showBadges = false;
  let isEnrolled = false;
  chrome.runtime.sendMessage({ type: 'GET_GROUP' }, (res) => {
    if (chrome.runtime.lastError) return;
    if (res && res.enrolled) {
      isEnrolled = true;
      showBadges = res.group !== 'control';
      console.log('🧪 [SAIR] LinkedIn Content Script loaded. Group:', res.group, '| Showing Badges:', showBadges);
      scan(); // Kickoff scan now that we are enrolled
    }
  });

  // Code points, not UTF-16 code units — matches Python's len() on the backend.
  function codePointLength(s) {
    let n = 0;
    for (const _ of String(s || '')) n++;
    return n;
  }

  // postId → stored record { detector, error, ... }
  const scored = new Map();
  // postIds we've sent to the SW that haven't come back yet
  const inFlight = new Set();
  // postIds skipped because their body is below MIN_FOR_SCORING — we still
  // surface a badge so the user can tell "skipped" apart from "not detected".
  const tooShort = new Set();
  // All postIds that we have ever tracked (sent, short, or scored). Used by
  // paintBadge to avoid removing the badge when LinkedIn re-mounts a virtualized
  // element after the inFlight entry has already been cleared (badge flicker fix).
  const trackedKeys = new Set();

  function log(...args) { if (DEBUG) console.debug('[SAIR-LI]', ...args); }

  // ───────── Body / footer parsing ─────────

  function isFooterCruft(line) {
    if (!line) return true;
    if (line === '•' || line === '·') return true;
    if (
      line === 'Repost' || line === 'Reshare' || line === 'Send' ||
      line === 'Watch again' || line === 'Reply' || line === 'See translation' ||
      line === 'Save' || line === 'Copy link to post'
    ) return true;
    if (/^…\s*more$/i.test(line) || /^\.{3}\s*more$/i.test(line)) return true;
    if (/^[\d.,]+\s*[KMB]?$/.test(line)) return true;
    if (/\b\d[\d.,]*\s*[KMB]?\s+(?:reactions?|comments?|reposts?|reshares?|likes?|others?)\b/i.test(line)) return true;
    if (/\breacted\b/i.test(line)) return true;
    return false;
  }

  function findFooterStart(lines) {
    let footer = -1;
    for (let i = 0; i < lines.length - 1; i++) {
      if (lines[i].trim() === 'Like' && (lines[i + 1] || '').trim() === 'Comment') {
        footer = i;
        break;
      }
    }
    if (footer === -1) return -1;
    while (footer > 0 && isFooterCruft(lines[footer - 1].trim())) footer--;
    return footer;
  }

  function findBodyStart(lines, limit) {
    for (let i = 0; i < limit; i++) {
      if (lines[i].trim() === 'Promoted') {
        let j = i + 1;
        while (j < limit && lines[j].trim() === '') j++;
        return j;
      }
    }
    const ACTION = new Set([
      'Follow', 'Following', '+ Follow', 'Connect', 'Pending', '+ Connect', 'Message',
    ]);
    for (let i = 0; i < limit; i++) {
      if (ACTION.has(lines[i].trim())) {
        let j = i + 1;
        while (j < limit && lines[j].trim() === '') j++;
        return j;
      }
    }
    for (let i = 0; i < limit; i++) {
      const l = lines[i].trim();
      if (
        /^(?:\d+\s*(?:s|sec|m|min|h|hr|d|w|mo|y|yr)(?:\s*[•·]\s*(?:Edited|Promoted))?|\d+\s*\w+\s*ago|yesterday|just now)$/i.test(l)
      ) {
        let j = i + 1;
        while (j < limit && lines[j].trim() !== '') j++;
        while (j < limit && lines[j].trim() === '') j++;
        return j;
      }
    }
    return -1;
  }

  function extractBody(item) {
    const text = item.innerText || '';
    const lines = text.split('\n').map((l) => l.replace(/\s+$/, ''));
    const footerStart = findFooterStart(lines);
    const limit = footerStart === -1 ? lines.length : footerStart;
    let bodyStart = findBodyStart(lines, limit);

    if (bodyStart === -1) {
      // Fallback for link-card posts or unusual layouts with no time anchor:
      // skip obvious header lines (name, short label-like tokens) and use the
      // first substantive line as the body start. We look for the first line
      // that is longer than 60 chars, treating it as the real post text.
      for (let i = 0; i < limit; i++) {
        if (lines[i].trim().length > 60) {
          bodyStart = i;
          break;
        }
      }
      // If still not found, give up.
      if (bodyStart === -1) return null;
    }

    let bodyEnd = limit;
    while (bodyEnd > bodyStart && lines[bodyEnd - 1].trim() === '') bodyEnd--;
    const body = lines.slice(bodyStart, bodyEnd).join('\n').trim();
    return body || null;
  }

  function extractEngagement(item) {
    const out = { likes: null, comments: null, reposts: null };
    const lines = (item.innerText || '').split('\n').map((l) => l.trim());
    for (const line of lines) {
      if (out.likes == null) {
        let m = line.match(/^([\d.,]+\s*[KMB]?)\s+(?:reactions?|likes?)$/i);
        if (m) { out.likes = parseCount(m[1]); continue; }
        m = line.match(/(?:^|\s)(?:and\s+)?([\d.,]+\s*[KMB]?)\s+others?\s+reacted/i);
        if (m) {
          const n = parseCount(m[1]);
          if (n != null) out.likes = n + 1;
          continue;
        }
      }
      if (out.comments == null) {
        const m = line.match(/^([\d.,]+\s*[KMB]?)\s+comments?$/i);
        if (m) { out.comments = parseCount(m[1]); continue; }
      }
      if (out.reposts == null) {
        const m = line.match(/^([\d.,]+\s*[KMB]?)\s+(?:reposts?|reshares?)$/i);
        if (m) { out.reposts = parseCount(m[1]); continue; }
      }
    }
    return out;
  }

  function extractAuthor(item) {
    const link = item.querySelector('a[href*="/in/"], a[href*="/company/"]');
    if (!link) return null;
    const m = (link.getAttribute('href') || '').match(/\/(?:in|company)\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }

  // Detect promoted/sponsored posts — LinkedIn marks these with a "Promoted"
  // label in the header area above the post body.
  function extractIsPromoted(item) {
    const text = item.innerText || '';
    const lines = text.split('\n').map((l) => l.trim());
    const footerStart = findFooterStart(lines);
    const limit = footerStart === -1 ? lines.length : footerStart;
    for (let i = 0; i < limit; i++) {
      if (lines[i] === 'Promoted') return true;
    }
    return false;
  }

  // Relative post age as displayed by LinkedIn — e.g. "6m", "1mo", "9mo".
  // We have no reliable way to derive an absolute timestamp (LinkedIn doesn't
  // expose one in the DOM), so the relative string is the best we can capture.
  function extractPostedAtRelative(item) {
    const text = item.innerText || '';
    const lines = text.split('\n').map((l) => l.trim());
    const footerStart = findFooterStart(lines);
    const limit = footerStart === -1 ? lines.length : footerStart;
    for (let i = 0; i < limit; i++) {
      const l = lines[i];
      let m = l.match(/^(?:\d+\s*(?:s|sec|m|min|h|hr|d|w|mo|y|yr)(?:\s*[•·]\s*(?:Edited|Promoted))?|\d+\s*\w+\s*ago|yesterday|just now)$/i);
      if (m) return m[0].replace(/\s+/g, '');
    }
    return null;
  }

  // LinkedIn Premium badge — the small gold "in" icon next to some authors'
  // names. The badge SVG itself has no distinguishing attribute, but
  // LinkedIn marks the wrapping author block with an aria-label of the form
  // "<Name> Premium Profile <degree>" for screen-reader accessibility.
  // That label is the most reliable signal across variants since LinkedIn
  // can't drop it without breaking screen readers.
  function extractAuthorPremium(item) {
    return !!(
      item.querySelector('[aria-label*="Premium Profile" i]') ||
      item.querySelector('[aria-label*="LinkedIn Premium" i]') ||
      item.querySelector('[aria-label*="Premium member" i]') ||
      item.querySelector('[data-test-icon*="premium" i]') ||
      item.querySelector('img[alt*="LinkedIn Premium" i]')
    );
  }

  // Heuristic — LinkedIn ships two DOM variants with hashed class names, so
  // we work from innerText lines. Returns whatever fields could be parsed;
  // missing fields are null. Tolerant to reposts, ads, articles.
  const LI_REL_TIME = /^(?:\d+\s*(?:s|sec|m|min|h|hr|d|w|mo|y|yr)(?:\s*[•·]\s*(?:Edited|Promoted))?|\d+\s*\w+\s*ago|yesterday|just now)$/i;
  const LI_DEGREE = /^(1st|2nd|3rd\+?|Following)$/i;
  const LI_DEGREE_INLINE = /[·•]\s*(1st|2nd|3rd\+?|Following)/i;
  const LI_SKIP = new Set([
    '', '•', '·', 'Promoted', 'Follow', 'Following', '+ Follow',
    'Connect', '+ Connect', 'Message', 'Pending', 'Subscribe',
    '1st', '2nd', '3rd', '3rd+',
  ]);

  function extractAuthorMeta(item) {
    const out = { displayName: null, headline: null, connectionDegree: null };
    const text = item.innerText || '';
    const lines = text.split('\n').map((l) => l.trim());
    const footerStart = findFooterStart(lines);
    const limit = footerStart === -1 ? lines.length : footerStart;
    const bodyStart = findBodyStart(lines, limit);
    if (bodyStart <= 0) return out;

    const header = lines.slice(0, bodyStart);

    for (const line of header) {
      const m1 = line.match(LI_DEGREE);
      if (m1) { out.connectionDegree = m1[1]; break; }
      const m2 = line.match(LI_DEGREE_INLINE);
      if (m2) { out.connectionDegree = m2[1]; break; }
    }

    for (const line of header) {
      if (LI_SKIP.has(line)) continue;
      if (LI_REL_TIME.test(line)) continue;
      if (/^[\d.,]+\s+followers?$/i.test(line)) continue;
      if (/reposted this/i.test(line)) continue;
      if (LI_DEGREE_INLINE.test(line)) {
        const cleaned = line.split(/[·•]/)[0].trim();
        if (cleaned) { out.displayName = cleaned; break; }
        continue;
      }
      out.displayName = line;
      break;
    }

    // Headline = the substantive line just before the relative-time line.
    for (let i = 0; i < header.length; i++) {
      if (!LI_REL_TIME.test(header[i])) continue;
      for (let j = i - 1; j >= 0; j--) {
        const l = header[j];
        if (LI_SKIP.has(l)) continue;
        if (l === out.displayName) continue;
        if (/^[\d.,]+\s+followers?$/i.test(l)) continue;
        if (/reposted this/i.test(l)) continue;
        if (LI_DEGREE.test(l)) continue;
        out.headline = l;
        break;
      }
      break;
    }

    return out;
  }

  // Walk the React fiber attached to a DOM element looking for an activity
  // URN string. Variant A LinkedIn doesn't expose the URN in any DOM
  // attribute, but it lives in the component's fiber tree. Breadth-first so
  // we prefer URNs closer to the root (the outer post's URN) over those
  // deeper down (embedded reposts' URNs). Bounded depth + WeakSet prevent
  // runaway walks. Returns null if nothing found.
  function extractUrnFromFiber(item) {
    if (!item) return null;
    const fiberKey = Object.keys(item).find((k) => k.startsWith('__reactFiber'));
    if (!fiberKey) return null;
    const root = item[fiberKey];
    if (!root) return null;

    const URN_RE = /urn:li:activity:\d+/;
    const MAX_DEPTH = 10;
    const seen = new WeakSet();
    const queue = [[root, 0]];

    while (queue.length > 0) {
      const [obj, depth] = queue.shift();
      if (!obj || typeof obj !== 'object' || depth > MAX_DEPTH) continue;
      if (seen.has(obj)) continue;
      seen.add(obj);
      for (const k in obj) {
        try {
          const v = obj[k];
          if (typeof v === 'string') {
            const m = v.match(URN_RE);
            if (m) return m[0];
          } else if (v && typeof v === 'object') {
            queue.push([v, depth + 1]);
          }
        } catch (_) { /* React internals occasionally throw on access */ }
      }
    }
    return null;
  }

  // Compute (or read cached) postId from a listitem element.
  function getOrComputeKey(item, precomputedBody) {
    if (item.dataset.sairKey) return item.dataset.sairKey;

    // Prefer urn-based IDs from the newer LinkedIn DOM variant — these are
    // stable activity identifiers and ideal as postIds.
    const dataId = item.getAttribute('data-id');
    if (dataId && dataId.startsWith('urn:')) {
      item.dataset.sairKey = dataId;
      return dataId;
    }
    const dataUrn = item.getAttribute('data-urn');
    if (dataUrn && dataUrn.startsWith('urn:')) {
      item.dataset.sairKey = dataUrn;
      return dataUrn;
    }

    // Older variant A: URN isn't exposed as a DOM attribute, but the React
    // fiber on the listitem holds it. Walking the fiber gives us the same
    // navigable activity URN that variant B exposes natively.
    const fiberUrn = extractUrnFromFiber(item);
    if (fiberUrn) {
      item.dataset.sairKey = fiberUrn;
      return fiberUrn;
    }

    // Older variant fallback: componentkey GUID when even the fiber doesn't
    // expose the URN. Not navigable but stable as an identifier.
    const ck = item.getAttribute('componentkey');
    if (ck) {
      item.dataset.sairKey = ck;
      return ck;
    }

    // Last resort: hash of the post body. Reuses an already-extracted body if
    // the caller has one to avoid the duplicate innerText parse.
    const body = precomputedBody || extractBody(item);
    if (!body) return null;
    const key = hashString(body.slice(0, 500));
    item.dataset.sairKey = key;
    return key;
  }

  function extractListItem(item) {
    const rawText = item.innerText || '';
    if (rawText.length < 60) {
      log('skip (too short):', rawText.slice(0, 60).replace(/\n/g, ' | '));
      return null;
    }
    const body = extractBody(item);
    if (!body || body.length < 30) {
      log('skip (no body):', rawText.slice(0, 120).replace(/\n/g, ' | '));
      return null;
    }
    if (codePointLength(body) < MIN_FOR_SCORING) {
      const shortKey = getOrComputeKey(item, body);
      if (shortKey) { tooShort.add(shortKey); trackedKeys.add(shortKey); }
      log(`skip (below ${MIN_FOR_SCORING}-char threshold): len=${codePointLength(body)}`);
      return null;
    }
    const key = getOrComputeKey(item, body);
    if (!key) return null;
    if (seen.has(key)) return null;
    seen.add(key);

    const meta = extractAuthorMeta(item);
    const post = {
      platform: 'linkedin',
      postId: key,
      url: null,
      author: extractAuthor(item),
      authorDisplayName: meta.displayName,
      authorHeadline: meta.headline,
      authorConnectionDegree: meta.connectionDegree,
      authorPremium: extractAuthorPremium(item),
      isPromoted: extractIsPromoted(item),
      postedAt: null,
      postedAtRelative: extractPostedAtRelative(item),
      text: body,
      engagement: extractEngagement(item),
      seenAt: Date.now(),
      badgeShown: showBadges,
    };
    log('captured:', post.author, '·',
      'likes=' + post.engagement.likes,
      'comments=' + post.engagement.comments,
      'reposts=' + post.engagement.reposts,
      '·', body.slice(0, 80).replace(/\n/g, ' '));
    return post;
  }

  // ───────── Badge overlay ─────────

  // Provider-agnostic — handles Pangram V3 and legacy Sapling records.
  function tierFromDetector(d) {
    if (!d) return null;
    if (d.prediction_short) {
      const s = String(d.prediction_short).toLowerCase();
      if (s === 'human') return 'human';
      if (s === 'ai') return 'ai';
      if (s.includes('mixed') || s.includes('assist')) return 'mixed';
    }
    const fa = typeof d.fraction_ai === 'number' ? d.fraction_ai : null;
    if (fa != null) {
      if (fa >= 0.7) return 'ai';
      if (fa >= 0.3) return 'mixed';
      return 'human';
    }
    if (typeof d.score === 'number') {
      if (d.score >= 0.7) return 'ai';
      if (d.score >= 0.3) return 'mixed';
      return 'human';
    }
    return null;
  }

  function aiPercentage(d) {
    if (!d) return null;
    if (typeof d.fraction_ai === 'number') return Math.round(d.fraction_ai * 100);
    if (typeof d.score === 'number') return Math.round(d.score * 100);
    return null;
  }

  function segmentCount(d) {
    if (!d) return 0;
    if (typeof d.windows_length === 'number') return d.windows_length;
    if (Array.isArray(d.windows)) return d.windows.length;
    if (Array.isArray(d.sentence_scores)) return d.sentence_scores.length;
    return 0;
  }

  function ensureBadge(item) {
    let badge = item.querySelector(':scope > .sair-badge');
    if (badge) return badge;
    badge = document.createElement('div');
    badge.className = 'sair-badge';
    item.insertBefore(badge, item.firstChild);
    return badge;
  }

  function removeBadge(item) {
    const badge = item.querySelector(':scope > .sair-badge');
    if (badge) badge.remove();
  }

  function paintBadge(item) {
    if (!showBadges) {
      removeBadge(item);
      return;
    }
    const key = getOrComputeKey(item);
    if (!key) { removeBadge(item); return; }
    const rec = scored.get(key);
    const isPending = inFlight.has(key) && !rec;
    const isShort = tooShort.has(key) && !rec;
    // If we've never seen/tracked this key at all, there's nothing to show.
    // But if it's in trackedKeys (we sent it, it came back, or it's short),
    // keep the badge even if inFlight is clear — prevents flash on re-mount.
    const isTracked = trackedKeys.has(key);

    if (!rec && !isPending && !isShort && !isTracked) { removeBadge(item); return; }

    const badge = ensureBadge(item);

    if (rec && rec.error) {
      badge.dataset.tier = 'error';
      badge.textContent = 'ERROR';
      badge.title = rec.error;
      return;
    }
    if (isShort) {
      badge.dataset.tier = 'short';
      badge.textContent = 'TOO SHORT';
      badge.title = 'Post body under 250 characters — not sent for scoring';
      return;
    }
    if (!rec) {
      badge.dataset.tier = 'pending';
      badge.textContent = 'SCORING';
      badge.removeAttribute('title');
      return;
    }
    const tier = tierFromDetector(rec.detector);
    if (!tier) {
      badge.dataset.tier = 'pending';
      badge.textContent = 'SCORED';
      return;
    }
    badge.dataset.tier = tier;
    badge.textContent = tier.toUpperCase();
    const pct = aiPercentage(rec.detector);
    const segs = segmentCount(rec.detector);
    const pctTxt = pct != null ? `${pct}% AI-likely` : '';
    const segsTxt = segs ? `${segs} segment${segs === 1 ? '' : 's'} scored` : '';
    badge.title = [pctTxt, segsTxt].filter(Boolean).join(' · ');
  }

  function paintAll() {
    const items = findFeedItems();
    for (const item of items) paintBadge(item);
  }

  // ───────── Scan / observe ─────────

  function findFeedItems() {
    // 1. Classic: role=list > role=listitem (the variant we originally built for)
    let items = document.querySelectorAll('main [role="list"] [role="listitem"]');
    if (items.length) return items;

    // 2. Variant without role=list parent
    items = document.querySelectorAll('main [role="listitem"]');
    if (items.length) return items;

    // 3. Newer/alternative DOM where posts are urn-tagged divs (no listitem role).
    //    Confirmed via diagnostic 2026-05-11: some users' LinkedIn has zero
    //    listitems and renders posts as `<div data-id="urn:li:activity:...">`.
    items = document.querySelectorAll('main [data-id^="urn:li:activity:"]');
    if (items.length) return items;

    // 4. Alt URN-based attribute used on some variants
    items = document.querySelectorAll('main [data-urn^="urn:li:activity:"]');
    if (items.length) return items;

    // 5. Semantic role=article (also appears in the newer variant)
    items = document.querySelectorAll('main [role="article"]');
    if (items.length) return items;

    // 6. Last resort: any role=listitem anywhere on the page
    return document.querySelectorAll('[role="listitem"]');
  }

  function scan() {
    if (!isEnrolled) return;
    const items = findFeedItems();
    log(`scan: ${items.length} listitem(s) on page`);
    let captured = 0;
    for (const item of items) {
      const post = extractListItem(item);
      if (post) {
        tracker.observe(item, post.postId);
        inFlight.add(post.postId);
        trackedKeys.add(post.postId);
        sendPost(post, computePriorityNow(item));
        captured++;
      } else if (item.dataset.sairKey) {
        // Already-seen post in a fresh DOM element (LinkedIn virtualizes).
        tracker.observe(item, item.dataset.sairKey);
      }
      paintBadge(item);
    }
    if (captured) log(`-> sent ${captured} new post(s) to background`);
  }

  // ───────── Score-result message listener ─────────
  // The SW broadcasts a SCORE_RESULT message for each post it processes.
  // We update our in-memory `scored` Map and repaint. No local cache.

  chrome.runtime.onMessage.addListener((msg, _sender, _sendResponse) => {
    if (msg && msg.type === 'ENROLLMENT_COMPLETE') {
      isEnrolled = true;
      showBadges = msg.group !== 'control';
      scan();
      return false;
    }

    if (!msg || msg.type !== 'SCORE_RESULT' || !msg.record) return false;
    
    isEnrolled = true;
    if (msg.group === 'control') showBadges = false;
    else showBadges = true;

    const rec = msg.record;
    if (rec.platform !== 'linkedin') return false;
    scored.set(rec.postId, rec);
    inFlight.delete(rec.postId);
    paintAll();
    return false;
  });

  // ───────── Interaction (click intent) capture ─────────
  // LinkedIn aria-labels are mostly unreliable (the diagnostic showed
  // Comment/Repost have aria-label="null"), so we identify the action
  // primarily by the button's innerText. The Like button is the exception:
  // its innerText changes by reaction state, so we recognize it via its
  // stable "Reaction button state:" aria-label prefix.

  function classifyLinkedinClick(btn) {
    const aria = btn.getAttribute('aria-label') || '';
    const text = (btn.innerText || '').trim();
    if (/^Reaction button state:/i.test(aria) || /^Like$/i.test(text)) {
      return 'like_click';
    }
    if (/^Comment$/i.test(text)) return 'comment_click';
    if (/^Repost$/i.test(text)) return 'repost_click';
    if (/^Send$/i.test(text)) return 'send_click';
    return null;
  }

  function findContainingListitem(el) {
    return el.closest(
      '[role="listitem"], [data-id^="urn:li:activity:"], [data-urn^="urn:li:activity:"], [role="article"]'
    );
  }

  document.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('button');
    if (!btn) return;
    const action = classifyLinkedinClick(btn);
    if (!action) return;
    const item = findContainingListitem(btn);
    if (!item) return;
    const postId = getOrComputeKey(item);
    if (!postId) return;
    chrome.runtime.sendMessage({
      type: 'INTERACTION_OBSERVED',
      payload: {
        platform: 'linkedin',
        postId,
        action,
        occurredAt: Date.now(),
      },
    });
  }, true);

  // ───────── Boot ─────────

  const trigger = makeThrottledScanner(scan, 250);
  const observer = new MutationObserver(trigger);
  observer.observe(document.body, { childList: true, subtree: true });

  scan();
  setTimeout(scan, 1500);
  setTimeout(scan, 4000);
})();
