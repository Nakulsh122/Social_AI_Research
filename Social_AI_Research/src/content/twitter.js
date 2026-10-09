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
      scan(); // Kickoff scan now that we are enrolled
    }
  });

  // Code points, not UTF-16 code units — matches Python's len() on the backend
  // so the "TOO SHORT" classification agrees with what the backend will accept.
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

  function log(...args) { if (DEBUG) console.debug('[SAIR-X]', ...args); }

  // ───────── Tweet extraction ─────────

  function countFromAria(el) {
    if (!el) return null;
    const aria = el.getAttribute('aria-label') || '';
    const m = aria.match(/([\d.,]+\s*[KMB]?)/i);
    return m ? parseCount(m[1]) : 0;
  }

  function findViews(article) {
    const group = article.querySelector('[role="group"]');
    if (!group) return null;
    const candidates = group.querySelectorAll('[aria-label]');
    for (const el of candidates) {
      const aria = el.getAttribute('aria-label') || '';
      const m = aria.match(/([\d.,]+\s*[KMB]?)\s*views?/i);
      if (m) return parseCount(m[1]);
    }
    return null;
  }

  function extractAuthor(article) {
    const userNameEl = article.querySelector('[data-testid="User-Name"]');
    if (!userNameEl) return null;
    const m = userNameEl.innerText.match(/@([A-Za-z0-9_]+)/);
    return m ? m[1] : null;
  }

  // Twitter exposes both the absolute timestamp (via the <time datetime>
  // attribute) and the human-readable relative form. We store both.
  function extractPostedAt(article) {
    const timeEl = article.querySelector('time');
    if (!timeEl) return { iso: null, relative: null };
    const iso = timeEl.getAttribute('datetime') || null;
    const relative = (timeEl.innerText || '').trim() || null;
    return { iso, relative };
  }

  // Pulls display name + verified-badge metadata from the same User-Name node.
  // verifiedType is one of "blue" / "business" / "government" / null. X's
  // badge DOM has changed several times; types may fall back to "blue" if the
  // aria-label parsing misses a new variant.
  function extractAuthorMeta(article) {
    const out = { displayName: null, verified: false, verifiedType: null };
    const el = article.querySelector('[data-testid="User-Name"]');
    if (!el) return out;

    const raw = (el.innerText || '').trim();
    const m = raw.match(/^([\s\S]+?)@[A-Za-z0-9_]+/);
    if (m) out.displayName = m[1].replace(/\s+/g, ' ').trim() || null;
    else out.displayName = (raw.split('\n')[0] || '').trim() || null;

    const badge = el.querySelector('[data-testid="icon-verified"]');
    if (badge) {
      out.verified = true;
      const aria = (badge.getAttribute('aria-label') || '').toLowerCase();
      if (aria.includes('government')) out.verifiedType = 'government';
      else if (aria.includes('business')) out.verifiedType = 'business';
      else out.verifiedType = 'blue';
    }

    return out;
  }

  // Compute (or read cached) postId from a tweet article element.
  function getOrComputeKey(article) {
    if (article.dataset.sairKey) return article.dataset.sairKey;
    const link = article.querySelector('a[href*="/status/"]');
    if (link) {
      const m = (link.getAttribute('href') || '').match(/\/status\/(\d+)/);
      if (m) {
        article.dataset.sairKey = m[1];
        return m[1];
      }
    }
    const textEl = article.querySelector('[data-testid="tweetText"]');
    if (!textEl) return null;
    const text = (textEl.innerText || '').trim();
    if (!text) return null;
    const key = hashString(text.slice(0, 500));
    article.dataset.sairKey = key;
    return key;
  }

  function extractTweet(article) {
    const textEl = article.querySelector('[data-testid="tweetText"]');
    if (!textEl) return null;
    const text = (textEl.innerText || '').trim();
    if (!text) return null;

    const tweetId = getOrComputeKey(article);
    if (!tweetId) return null;
    if (seen.has(tweetId)) return null;
    if (codePointLength(text) < MIN_FOR_SCORING) {
      tooShort.add(tweetId);
      return null;
    }
    seen.add(tweetId);

    const statusLink = article.querySelector('a[href*="/status/"]');
    const url = statusLink ? new URL(statusLink.getAttribute('href'), location.origin).href : null;

    const meta = extractAuthorMeta(article);
    const posted = extractPostedAt(article);
    const post = {
      platform: 'twitter',
      postId: tweetId,
      url,
      author: extractAuthor(article),
      authorDisplayName: meta.displayName,
      authorVerified: meta.verified,
      authorVerifiedType: meta.verifiedType,
      // X "Premium" is the paid blue check; gold (business) and grey
      // (government) are separate verification types, not Premium per se.
      authorPremium: meta.verifiedType === 'blue',
      postedAt: posted.iso,
      postedAtRelative: posted.relative,
      text,
      engagement: {
        replies: countFromAria(article.querySelector('[data-testid="reply"]')),
        retweets: countFromAria(article.querySelector('[data-testid="retweet"], [data-testid="unretweet"]')),
        likes: countFromAria(article.querySelector('[data-testid="like"], [data-testid="unlike"]')),
        views: findViews(article),
      },
      seenAt: Date.now(),
    };
    log('captured: @' + post.author, '·',
      'likes=' + post.engagement.likes,
      'rt=' + post.engagement.retweets,
      'replies=' + post.engagement.replies,
      'views=' + post.engagement.views,
      '·', text.slice(0, 80).replace(/\n/g, ' '));
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

  // X's tweet article is a flex row: [avatar column, content column]. Inserting
  // the badge directly into the article makes it a flex item that stretches
  // vertically and sits in the wrong column. We instead inject it into the
  // content column (a normal block container) where standard CSS works.
  function findContentColumn(article) {
    const textEl = article.querySelector('[data-testid="tweetText"]');
    if (textEl) {
      let p = textEl.parentElement;
      while (p && p.parentElement !== article) p = p.parentElement;
      if (p && p.parentElement === article) return p;
    }
    const userNameEl = article.querySelector('[data-testid="User-Name"]');
    if (userNameEl) {
      let p = userNameEl.parentElement;
      while (p && p.parentElement !== article) p = p.parentElement;
      if (p && p.parentElement === article) return p;
    }
    return article.lastElementChild || null;
  }

  // Per-article badge tracking via WeakMap — querySelector can't reliably find
  // the badge once it's nested inside the content column.
  const badges = new WeakMap();

  function ensureBadge(article) {
    let badge = badges.get(article);
    if (badge && article.contains(badge)) return badge;

    badge = document.createElement('div');
    badge.className = 'sair-badge';

    const target = findContentColumn(article) || article;
    target.insertBefore(badge, target.firstChild);
    badges.set(article, badge);
    return badge;
  }

  function removeBadge(article) {
    const badge = badges.get(article);
    if (badge && badge.parentNode) badge.remove();
    badges.delete(article);
  }

  function paintBadge(article) {
    if (!showBadges) return;
    const key = getOrComputeKey(article);
    if (!key) { removeBadge(article); return; }
    const rec = scored.get(key);
    const isPending = inFlight.has(key) && !rec;
    const isShort = tooShort.has(key) && !rec;

    if (!rec && !isPending && !isShort) { removeBadge(article); return; }

    const badge = ensureBadge(article);

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
    const articles = document.querySelectorAll('article[data-testid="tweet"]');
    for (const a of articles) paintBadge(a);
  }

  // ───────── Scan / observe ─────────

  function scan() {
    if (!isEnrolled) return;
    const articles = document.querySelectorAll('article[data-testid="tweet"]');
    log(`scan: ${articles.length} tweet(s) on page`);
    let captured = 0;
    for (const a of articles) {
      const post = extractTweet(a);
      if (post) {
        tracker.observe(a, post.postId);
        inFlight.add(post.postId);
        sendPost(post, computePriorityNow(a));
        captured++;
      } else if (a.dataset.sairKey) {
        // Already-seen post in a possibly-new DOM element (X virtualizes).
        // Re-observe so viewport changes still flow to the SW priority queue.
        tracker.observe(a, a.dataset.sairKey);
      }
      paintBadge(a);
    }
    if (captured) log(`-> sent ${captured} new tweet(s) to background`);
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
    if (rec.platform !== 'twitter') return false;
    scored.set(rec.postId, rec);
    inFlight.delete(rec.postId);
    paintAll();
    return false;
  });

  // ───────── Interaction (click intent) capture ─────────
  // Event delegation on document — survives X's tweet virtualization without
  // needing per-tweet listener (re)attachment. Identifies the action by the
  // stable [data-testid] attribute on the clicked button, then walks up to
  // the containing article to find the postId.

  const X_INTERACTION_BY_TESTID = {
    'like': 'like_click',
    'unlike': 'like_click',
    'reply': 'comment_click',
    'retweet': 'repost_click',
    'unretweet': 'repost_click',
  };

  document.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('[data-testid]');
    if (!btn) return;
    const action = X_INTERACTION_BY_TESTID[btn.getAttribute('data-testid')];
    if (!action) return;
    const article = btn.closest('article[data-testid="tweet"]');
    if (!article) return;
    const postId = getOrComputeKey(article);
    if (!postId) return;
    chrome.runtime.sendMessage({
      type: 'INTERACTION_OBSERVED',
      payload: {
        platform: 'twitter',
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
