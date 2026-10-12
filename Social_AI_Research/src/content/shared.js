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

  // Per-tab dwell time tracker: captures true active attention time on each post
  // in the viewport (>=50% visible, tab focused/visible), batches and flushes to SW.
  function createDwellTracker(platform) {
    const elToId = new WeakMap();     // element -> postId
    const idToEl = new Map();         // postId -> element (most recent)
    const activeSince = new Map();    // postId -> performance.now()
    const pendingDwell = new Map();   // postId -> total accumulated unflushed ms
    let flushTimer = null;
    let heartbeatTimer = null;

    function isElementInView(el) {
      if (!el || !el.isConnected) return false;
      if (document.visibilityState !== 'visible') return false;
      const rect = el.getBoundingClientRect();
      const vh = window.innerHeight || 1;
      const vw = window.innerWidth || 1;
      if (rect.bottom <= 0 || rect.top >= vh || rect.right <= 0 || rect.left >= vw) {
        return false;
      }
      const visibleHeight = Math.max(0, Math.min(rect.bottom, vh) - Math.max(rect.top, 0));
      const visibleWidth = Math.max(0, Math.min(rect.right, vw) - Math.max(rect.left, 0));
      const elArea = (rect.height || 1) * (rect.width || 1);
      const visibleArea = visibleHeight * visibleWidth;
      const ratio = visibleArea / elArea;
      if (ratio >= 0.5) return true;
      if (visibleHeight / vh >= 0.5) return true;
      return false;
    }

    function recordElapsed(postId, ms) {
      if (!ms || ms <= 0) return;
      const curr = pendingDwell.get(postId) || 0;
      pendingDwell.set(postId, curr + ms);
      scheduleFlush();
    }

    function checkEntry(entry) {
      const postId = elToId.get(entry.target);
      if (!postId) return;
      idToEl.set(postId, entry.target);
      const inView = isElementInView(entry.target);
      const now = performance.now();

      if (inView) {
        if (!activeSince.has(postId)) {
          activeSince.set(postId, now);
        }
      } else {
        if (activeSince.has(postId)) {
          const start = activeSince.get(postId);
          activeSince.delete(postId);
          const elapsed = Math.round(now - start);
          if (elapsed >= 300) {
            recordElapsed(postId, elapsed);
          }
        }
      }
    }

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          checkEntry(entry);
        }
      },
      {
        root: null,
        rootMargin: '0px',
        threshold: [0, 0.25, 0.5, 0.75, 1.0],
      }
    );

    heartbeatTimer = setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      const now = performance.now();
      for (const [postId, start] of activeSince) {
        const el = idToEl.get(postId);
        if (!el || !isElementInView(el)) {
          activeSince.delete(postId);
          const elapsed = Math.round(now - start);
          if (elapsed >= 300) recordElapsed(postId, elapsed);
          continue;
        }
        const chunk = Math.round(now - start);
        if (chunk >= 2000) {
          recordElapsed(postId, chunk);
          activeSince.set(postId, now);
        }
      }
    }, 2500);

    function scheduleFlush() {
      if (flushTimer) return;
      flushTimer = setTimeout(flush, 2000);
    }

    function snapshotAllActive() {
      const now = performance.now();
      for (const [postId, start] of activeSince) {
        const elapsed = Math.round(now - start);
        if (elapsed >= 200) {
          recordElapsed(postId, elapsed);
        }
      }
      activeSince.clear();
    }

    function flush() {
      if (flushTimer) {
        clearTimeout(flushTimer);
        flushTimer = null;
      }
      if (pendingDwell.size === 0) return;
      const updates = [];
      const now = Date.now();
      for (const [postId, dwellMs] of pendingDwell) {
        if (dwellMs >= 200) {
          updates.push({
            platform: platform || 'unknown',
            postId,
            dwellMs,
            occurredAt: now,
          });
        }
      }
      pendingDwell.clear();
      if (updates.length > 0) {
        try {
          chrome.runtime.sendMessage(
            { type: 'DWELL_OBSERVED', updates },
            () => { void chrome.runtime.lastError; }
          );
        } catch (_) {}
      }
    }

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState !== 'visible') {
        snapshotAllActive();
        flush();
      } else {
        const now = performance.now();
        for (const [postId, el] of idToEl) {
          if (isElementInView(el)) {
            activeSince.set(postId, now);
          }
        }
      }
    });

    window.addEventListener('pagehide', () => {
      snapshotAllActive();
      flush();
    });

    window.addEventListener('beforeunload', () => {
      snapshotAllActive();
      flush();
    });

    return {
      observe(element, postId) {
        if (!element || !postId) return;
        elToId.set(element, postId);
        idToEl.set(postId, element);
        observer.observe(element);
        if (isElementInView(element) && !activeSince.has(postId)) {
          activeSince.set(postId, performance.now());
        }
      },
      flushNow() {
        snapshotAllActive();
        flush();
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
    createDwellTracker,
  };
})();
