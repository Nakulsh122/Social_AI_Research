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

  // Per-tab dwell time tracker: captures true active attention time on the SINGLE
  // post nearest to the viewport center. Eliminates multi-post inflation.
  // Automatically locks when scrolling, when window loses focus (app switch),
  // or when user is inactive for >30s.
  function createDwellTracker(platform) {
    const elToId = new WeakMap();     // element -> postId
    const idToEl = new Map();         // postId -> element (most recent)
    const pendingDwell = new Map();   // postId -> total accumulated unflushed ms
    let activePostId = null;
    let activeSince = 0;
    let flushTimer = null;
    let watchdogTimer = null;
    let isScrolling = false;
    let scrollEndTimer = null;
    let isWindowFocused = document.hasFocus();
    let lastUserActivity = Date.now();
    const IDLE_TIMEOUT_MS = 30000;

    function recordActivity() {
      lastUserActivity = Date.now();
    }

    const activityEvents = ['mousemove', 'keydown', 'click', 'wheel', 'touchstart'];
    for (const evt of activityEvents) {
      window.addEventListener(evt, recordActivity, { passive: true, capture: true });
    }

    window.addEventListener('focus', () => {
      isWindowFocused = true;
      recordActivity();
      updateFocalDwell();
    });

    window.addEventListener('blur', () => {
      isWindowFocused = false;
      snapshotActive();
    });

    function isUserActive() {
      if (document.visibilityState !== 'visible') return false;
      if (!isWindowFocused) return false;
      if (isScrolling) return false;
      if (Date.now() - lastUserActivity > IDLE_TIMEOUT_MS) return false;
      return true;
    }

    function findNearestCenterPostId() {
      if (!isUserActive()) return null;
      const vh = window.innerHeight || 1;
      const centerY = vh / 2;
      let bestId = null;
      let bestDist = Infinity;

      for (const [postId, el] of idToEl) {
        if (!el || !el.isConnected) continue;
        const rect = el.getBoundingClientRect();
        // Element must intersect the screen vertically
        if (rect.bottom <= 0 || rect.top >= vh) continue;

        const visibleHeight = Math.max(0, Math.min(rect.bottom, vh) - Math.max(rect.top, 0));
        if (visibleHeight < 40) continue; // Skip tiny slivers

        // Calculate distance from post's center to viewport center
        let dist;
        if (rect.top <= centerY && rect.bottom >= centerY) {
          // Post covers the center line of the screen (highest priority)
          const postCenter = rect.top + rect.height / 2;
          dist = Math.abs(postCenter - centerY);
        } else if (rect.bottom < centerY) {
          dist = (centerY - rect.bottom) + 1000;
        } else {
          dist = (rect.top - centerY) + 1000;
        }

        if (dist < bestDist) {
          bestDist = dist;
          bestId = postId;
        }
      }
      return bestId;
    }

    function recordElapsed(postId, ms) {
      if (!ms || ms <= 0) return;
      const curr = pendingDwell.get(postId) || 0;
      pendingDwell.set(postId, curr + ms);
      const total = curr + ms;
      if (ms >= 60000 || total >= 120000) {
        console.warn(`⚠️ [SAIR Dwell Anomaly] Long dwell detected: +${ms}ms (total: ${total}ms) on post ${postId}`);
      }
      scheduleFlush();
    }

    function snapshotActive() {
      if (activePostId && activeSince) {
        const elapsed = Math.round(performance.now() - activeSince);
        if (elapsed >= 300) {
          recordElapsed(activePostId, elapsed);
        }
      }
      activePostId = null;
      activeSince = 0;
    }

    function updateFocalDwell() {
      const now = performance.now();
      const nextId = findNearestCenterPostId();

      if (nextId !== activePostId) {
        // Focal post changed (user moved to next post, went idle, or blurred)
        snapshotActive();
        if (nextId) {
          activePostId = nextId;
          activeSince = now;
        }
      } else if (activePostId && activeSince) {
        // Continuous reading on the same focal post: chunk every 2.5s
        const chunk = Math.round(now - activeSince);
        if (chunk >= 2500) {
          recordElapsed(activePostId, chunk);
          activeSince = now;
        }
      }
    }

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const postId = elToId.get(entry.target);
          if (postId) idToEl.set(postId, entry.target);
        }
        updateFocalDwell();
      },
      {
        root: null,
        rootMargin: '0px',
        threshold: [0, 0.25, 0.5, 0.75, 1.0],
      }
    );

    watchdogTimer = setInterval(() => {
      updateFocalDwell();
    }, 500);

    function onScrollStart() {
      if (!isScrolling) {
        isScrolling = true;
        snapshotActive();
      }
      if (scrollEndTimer) clearTimeout(scrollEndTimer);
      scrollEndTimer = setTimeout(onScrollEnd, 200);
    }

    function onScrollEnd() {
      if (scrollEndTimer) {
        clearTimeout(scrollEndTimer);
        scrollEndTimer = null;
      }
      isScrolling = false;
      updateFocalDwell();
    }

    window.addEventListener('scroll', onScrollStart, { passive: true });
    if ('onscrollend' in window) {
      window.addEventListener('scrollend', onScrollEnd, { passive: true });
    }

    function scheduleFlush() {
      if (flushTimer) return;
      flushTimer = setTimeout(flush, 2000);
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
        snapshotActive();
        flush();
      } else {
        recordActivity();
        updateFocalDwell();
      }
    });

    window.addEventListener('pagehide', () => {
      snapshotActive();
      flush();
    });

    window.addEventListener('beforeunload', () => {
      snapshotActive();
      flush();
    });

    return {
      observe(element, postId) {
        if (!element || !postId) return;
        elToId.set(element, postId);
        idToEl.set(postId, element);
        observer.observe(element);
        updateFocalDwell();
      },
      flushNow() {
        snapshotActive();
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
