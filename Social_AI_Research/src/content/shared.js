(function () {
  if (window.__SAIR_SHARED__) return;
  window.__SAIR_SHARED__ = true;

  function parseCount(input) {
    if (input == null) return null;
    const s = String(input).trim().replace(/,/g, '');
    if (!s) return null;
    const m = s.match(/^([\d.]+)\s*([KMB])?/i);
    if (!m) return null;
    let n = parseFloat(m[1]);
    if (Number.isNaN(n)) return null;
    const unit = (m[2] || '').toUpperCase();
    if (unit === 'K') n *= 1e3;
    else if (unit === 'M') n *= 1e6;
    else if (unit === 'B') n *= 1e9;
    return Math.round(n);
  }

  function hashString(s) {
    let h = 5381;
    const str = String(s);
    for (let i = 0; i < str.length; i++) {
      h = ((h << 5) + h) + str.charCodeAt(i);
      h |= 0;
    }
    return (h >>> 0).toString(36);
  }

  function sendPost(payload, priority) {
    try {
      const msg = { type: 'POST_OBSERVED', payload };
      if (typeof priority === 'number') msg.priority = priority;
      chrome.runtime.sendMessage(msg, () => {
        void chrome.runtime.lastError;
      });
    } catch (_) {
      // SW reloading; drop silently.
    }
  }

  function makeThrottledScanner(scanFn, delayMs) {
    let timer = null;
    return function trigger() {
      if (timer) return;
      timer = setTimeout(() => {
        timer = null;
        try { scanFn(); } catch (e) { console.warn('[SAIR] scan error', e); }
      }, delayMs);
    };
  }

  // Synchronous viewport-relative priority for an element. Used to set the
  // initial priority of a job as it enters the SW queue, before the
  // IntersectionObserver has had a chance to fire its first callback.
  function computePriorityNow(element) {
    if (document.visibilityState !== 'visible') return 0;
    if (!element || !element.getBoundingClientRect) return 50;
    const r = element.getBoundingClientRect();
    const vh = window.innerHeight || 1;
    if (r.bottom < 0 || r.top > vh) {
      if (r.bottom < -2 * vh) return 10;
      if (r.top > 2 * vh) return 30;
      return 50;
    }
    const visible = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    const ratio = visible / (r.height || 1);
    if (ratio >= 0.5) return 100;
    if (ratio >= 0.1) return 70;
    return 60;
  }

  // Per-tab viewport tracker: observes posts, computes their priority based on
  // intersection state, and ships batched priority updates to the SW every 250ms.
  function createViewportTracker() {
    const priorities = new Map();   // postId -> latest priority
    const elToId = new WeakMap();   // element -> postId (auto-cleared when LinkedIn/X virtualizes)
    const pending = new Map();      // postIds with unflushed priority changes
    let flushTimer = null;

    function priorityForEntry(entry) {
      if (document.visibilityState !== 'visible') return 0;
      if (entry.isIntersecting) {
        const r = entry.intersectionRatio;
        if (r >= 0.5) return 100;
        if (r >= 0.1) return 70;
        return 60;
      }
      const rect = entry.boundingClientRect;
      const vh = window.innerHeight || 1;
      if (rect.bottom < -2 * vh) return 10;
      if (rect.top > 2 * vh) return 30;
      return 50;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const postId = elToId.get(entry.target);
          if (!postId) continue;
          const p = priorityForEntry(entry);
          priorities.set(postId, p);
          pending.set(postId, p);
        }
        scheduleFlush();
      },
      {
        root: null,
        rootMargin: '500px 0px 500px 0px',
        threshold: [0, 0.1, 0.5, 1.0],
      }
    );

    function scheduleFlush() {
      if (flushTimer) return;
      flushTimer = setTimeout(flush, 250);
    }

    function flush() {
      flushTimer = null;
      if (pending.size === 0) return;
      const updates = [];
      for (const [postId, priority] of pending) updates.push({ postId, priority });
      pending.clear();
      try {
        chrome.runtime.sendMessage(
          { type: 'POST_PRIORITY_BATCH', updates },
          () => { void chrome.runtime.lastError; }
        );
      } catch (_) {}
    }

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState !== 'visible') {
        for (const postId of priorities.keys()) {
          priorities.set(postId, 0);
          pending.set(postId, 0);
        }
        scheduleFlush();
      }
      // When visible again, IntersectionObserver re-fires on next change.
    });

    return {
      observe(element, postId) {
        if (!element || !postId) return;
        elToId.set(element, postId);
        observer.observe(element);
      },
      priorityFor(postId) {
        return priorities.get(postId);
      },
    };
  }

  window.SAIR = {
    parseCount,
    hashString,
    sendPost,
    makeThrottledScanner,
    computePriorityNow,
    createViewportTracker,
  };
})();
