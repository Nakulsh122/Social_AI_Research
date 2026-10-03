# Social AI Research — Project Context

## Purpose

Study 1 deliverable for a three-part research project measuring:
1. How much AI-generated content people encounter on Twitter/X and LinkedIn feeds
2. Whether labeling AI content changes engagement behavior (future studies)
3. What mechanisms underlie those effects (future studies)

**This codebase is the data-collection instrument.** It is a Chrome extension that, for consenting participants:
- Scrapes the text of feed posts as they appear in the browser
- Sends each post body to the Pangram AI-detection API
- Stores the score + engagement metadata locally for downstream analysis
- Surfaces a visible badge (`HUMAN` / `MIXED` / `AI` / `TOO SHORT` / `SCORING` / `ERROR`) on each post so the participant can see what was assessed

It is a research instrument, not a product. Prioritize extraction reliability, structured logging, and reproducibility over UI polish or feature breadth.

---

## Tech stack & architecture

- **Manifest V3 Chrome extension** (no MV2 fallback)
- Vanilla JavaScript, no framework, no bundler. A tiny `build.js` (~60 lines, Node stdlib only) reads `.env` and copies `src/` → `dist/`, injecting credentials into a generated `config.js`
- **Three execution contexts** in the extension:
  - **Content scripts** (per tab, isolated world): scrape the feed, observe viewport via IntersectionObserver, paint AI-score badges directly into the page DOM
  - **Service worker** (`background.js`): receives observed posts, runs a 2-worker priority queue against the Pangram API, writes results to `chrome.storage.local`
  - **Popup**: management UI (Test / Refresh / Export / Clear / Rescore) plus a list of recent observations

Storage is `chrome.storage.local` only — no remote backend. Researchers export JSON via the popup.

---

## File structure

```
Social_AI_Research/
├── .env                       # gitignored — holds PANGRAM_API_KEY
├── .env.example               # template
├── .gitignore                 # .env, dist/, node_modules/, .DS_Store
├── package.json               # one script: "build": "node build.js"
├── build.js                   # reads .env, copies src/→dist/, writes dist/config.js
├── BUGS_TODO.md               # 24 known issues prioritized P0-P3
├── CLAUDE.md                  # this file
├── src/                       # source
│   ├── manifest.json          # MV3, host_permissions for x.com / linkedin.com / pangram
│   ├── background.js          # SW: priority queue, Pangram client, storage
│   ├── content/
│   │   ├── shared.js          # parseCount, hashString, sendPost, IntersectionObserver tracker
│   │   ├── twitter.js         # X extraction + badge painting
│   │   ├── linkedin.js        # LinkedIn extraction + badge painting
│   │   └── badge.css          # shared badge styles
│   └── popup/
│       ├── popup.html
│       ├── popup.js
│       └── popup.css
├── dist/                      # gitignored — output of `npm run build`, what you load as unpacked
└── example_exports/           # sample observation JSON exports
```

---

## How it works end-to-end

### Per-tab data flow

1. **Content script injects** at `document_idle` per `matches` patterns in [manifest.json](src/manifest.json#L17-L33)
2. A throttled `MutationObserver` on `document.body` fires `scan()` at ~500-800ms intervals when the DOM mutates (scroll, lazy-load)
3. `scan()` calls `findFeedItems()` which uses a cascade of selectors to locate post containers — see "LinkedIn DOM variants" below
4. For each candidate, `extractListItem(item)` (or `extractTweet(article)` on X) returns either a `post` object or `null`. The function:
   - Runs platform-specific text/engagement extraction (`extractBody`, `extractEngagement`, `extractAuthor`)
   - Computes a stable `postId` via `getOrComputeKey`
   - Returns null and marks the post in a `tooShort` Set if body < 250 chars (badge shows "TOO SHORT")
   - Returns null for posts already in the in-memory `seen` Set
5. If a post is returned, `sendPost(post, priority)` fires a `POST_OBSERVED` message to the service worker with a viewport-relative initial priority. The post element is registered with the IntersectionObserver to receive priority updates
6. `paintBadge(item)` runs regardless of whether extraction returned a post — it reads the local `scored` / `inFlight` / `tooShort` state and renders/updates the badge DOM

### Per-extension data flow

7. SW receives `POST_OBSERVED`, validates `text.length >= 250`, inserts into `queue` Map keyed by `postId` (deduplicated)
8. SW spawns up to `CONCURRENCY = 2` workers if needed. Each worker calls `dequeueHighest()` (O(N) max-priority scan), fetches Pangram, awaits, then writes to storage. Writes are serialized via a `writeChain` Promise to prevent record-loss races
9. SW writes result to `chrome.storage.local["sair_observations"]` (full array rewrite — caveat below)
10. `chrome.storage.onChanged` fires in every tab. Content scripts hydrate their local `scored` Map and call `paintAll()` to update every visible post's badge

### Viewport priority loop

11. As the user scrolls, `IntersectionObserver` callbacks compute new priorities per post (0=hidden tab, 10=far past, 30=far ahead, 50=near, 60=just entering, 70=in viewport partial, 100=in viewport ≥50%)
12. Updates are batched every 250ms via `POST_PRIORITY_BATCH` messages so a fast scroll doesn't fire hundreds of messages
13. SW handler updates `job.priority` on any queued (not yet processing) jobs in place. Workers pick max priority on next iteration. **No preemption** — a worker mid-fetch on a low-priority post finishes that fetch before picking up a higher-priority one

---

## Detector: Pangram V3

```
POST https://text.api.pangram.com/v3
Headers:
  Content-Type: application/json
  x-api-key: <from .env>
Body:
  { "text": "..." }
```

Response is rich:
```json
{
  "text": "...",
  "version": "3.0",
  "prediction_short": "AI" | "Human" | "Mixed" | "AI-Assisted",
  "fraction_ai": 0.0–1.0,
  "fraction_ai_assisted": 0.0–1.0,
  "fraction_human": 0.0–1.0,
  "headline": "AI Detected",
  "windows": [...],  // per-segment analysis
  ...
}
```

**Tier classification** ([twitter.js:104-126](src/content/twitter.js#L104), [linkedin.js:179-201](src/content/linkedin.js#L179)) is provider-agnostic via `tierFromDetector(d)`:
1. Uses Pangram's `prediction_short` if present
2. Falls back to `fraction_ai` threshold (≥0.7 → AI, ≥0.3 → Mixed, else Human)
3. Falls back to `score` field (Sapling-era — see "History" below)

**Historical note**: detector was briefly switched Pangram → Sapling on 2026-04-30 then back to Pangram on 2026-05-12. The fallback paths in `tierFromDetector` exist so any Sapling-era records still in a user's storage continue to display correctly.

### Stored record shape

```json
{
  "platform": "twitter" | "linkedin",
  "postId": "urn:li:activity:7460033..." | "1942350123...",
  "url": "https://...",
  "author": "elonmusk",
  "text": "...",
  "engagement": {
    // twitter:
    "likes": 4483, "retweets": 455, "replies": 0, "views": 38837759
    // linkedin:
    "likes": 444, "comments": 81, "reposts": 48
  },
  "seenAt": 1746201230412,
  "detector": { /* full Pangram response */ },
  "scoredAt": 1746201232000,
  "error": null  // or string if Pangram failed
}
```

The full Pangram response is preserved per record — including `windows[]` — so downstream analysis can re-derive any aggregate without re-querying the API.

---

## LinkedIn DOM variants (important)

**LinkedIn ships at least two distinct DOM variants in production simultaneously.** Confirmed via field diagnostic 2026-05-11 when a user reported the extension working on X but not LinkedIn.

**Variant A** (older, what we originally built for):
- Feed wrapped in `<main>` → `[role="list"]`
- Each post in `[role="listitem"]`
- `componentkey="<guid>"` attribute used as postId
- Class names fully hashed (`_2174b9ce` style — change per deploy)
- No `data-id` or `data-urn` attributes
- No semantic class names

**Variant B** (newer / discovered via diagnostic):
- **No `role="listitem"` anywhere** — this is the smoking gun
- Posts are `<div data-id="urn:li:activity:NNN">` with `role="article"`
- Semantic class names ARE present: `feed-shared-*`, `update-components-*`
- `data-urn="urn:li:activity:NNN"` also appears on some elements

**Selector cascade** in [linkedin.js:265-290](src/content/linkedin.js#L265):
1. `main [role="list"] [role="listitem"]` (variant A primary)
2. `main [role="listitem"]` (variant A fallback)
3. `main [data-id^="urn:li:activity:"]` (variant B primary)
4. `main [data-urn^="urn:li:activity:"]` (variant B alt)
5. `main [role="article"]` (variant B fallback)
6. `[role="listitem"]` (last resort)

First selector that returns ≥1 element wins.

**postId derivation** ([linkedin.js:127-156](src/content/linkedin.js#L127)) in `getOrComputeKey(item, body?)`:
1. `dataset.sairKey` cache
2. `data-id` starting with `urn:` (variant B, most stable)
3. `data-urn` starting with `urn:` (variant B alt)
4. `componentkey` (variant A)
5. Hash of body content (last resort — collides on templated text)

**Body extraction is text-pattern based** on `innerText` because the DOM offers no stable selector for the post body in either variant. See `extractBody`, `findFooterStart`, `findBodyStart`, `isFooterCruft` in [linkedin.js](src/content/linkedin.js). This is **English-locale-dependent** — the parser looks for literal `"Follow"` / `"Like"` / `"Comment"` / `"Promoted"`. Standardizing study participants on English LinkedIn UI is the recommended methodological safeguard.

---

## X / Twitter DOM

Much simpler because X uses stable `data-testid` attributes:
- Posts: `article[data-testid="tweet"]`
- Body: `[data-testid="tweetText"]`
- Author: `[data-testid="User-Name"]`
- Engagement buttons: `[data-testid="reply"]`, `[data-testid="retweet"]` (or `unretweet`), `[data-testid="like"]` (or `unlike`)
- Views: aria-label match for `/views?/i` inside `[role="group"]`

**Important quirk**: X's `<article data-testid="tweet">` is a **CSS Grid container** (avatar column + content column). Direct-prepending the badge places it in the narrow avatar column where text wraps vertically. Solution in [twitter.js:113-130](src/content/twitter.js#L113): find the content column via `findContentColumn(article)` (walks up from `tweetText` to a direct child of the article), insert badge there. WeakMap-tracked because the badge is no longer a `:scope >` direct child.

**Locale-agnosticism**: X engagement counts use aria-label regex `([\d.,]+\s*[KMB]?)` which extracts the leading number regardless of language. Views extraction is the one weak spot — it requires the English word `views` in the aria-label. Otherwise the X scraper works across all locales.

---

## Concurrency & priority queue

[background.js](src/background.js):
- `CONCURRENCY = 2` — two parallel workers pull from the same queue
- `RATE_LIMIT_MS = 50` — short pause between requests per worker (so peak burst is ~40 req/s across both workers)
- `queue` is `Map<postId, { kind, payload, priority }>` — not a heap. `dequeueHighest()` does an O(N) max scan per pull, fine because N is small (typically <50)
- `writeChain` Promise serializes all storage writes so concurrent workers can't lose records via read-modify-write races (this race is real and must be guarded — see BUGS_TODO #5)

Messages handled:
- `POST_OBSERVED` (with `payload` + initial `priority`)
- `POST_PRIORITY_BATCH` (array of `{postId, priority}` updates)
- `GET_OBSERVATIONS` → returns full list
- `CLEAR_OBSERVATIONS`
- `RESCORE_FAILED` → re-queues any record with `error` set or `detector` null
- `TEST_API` → fires one sample request, returns full response for the popup's diagnostic dialog

---

## Badge UI

[badge.css](src/content/badge.css) plus per-platform `ensureBadge` / `paintBadge` in each content script.

**Visual**: small right-aligned tab at the top of each post (uses `display: block; width: fit-content; margin-left: auto`). Bottom-left corner rounded so it visually "tabs off" the post's top-right corner. Single line of label text only — no percentage in the badge itself.

**Five tiers**:
| Tier | Label | Color | When |
|---|---|---|---|
| `error` | `ERROR` | faint red bg | Pangram call failed (full message in tooltip) |
| `short` | `TOO SHORT` | neutral gray | Body < 250 chars; not sent for scoring |
| `pending` | `SCORING` | dark gray | In SW queue awaiting Pangram |
| `human` | `HUMAN` | green | `prediction_short === "Human"` or `fraction_ai < 0.3` |
| `mixed` | `MIXED` | amber | "Mixed" / "AI-Assisted" or 0.3 ≤ fraction_ai < 0.7 |
| `ai` | `AI` | red | "AI" or fraction_ai ≥ 0.7 |

Hover any badge for the percentage and sentence-count tooltip.

**X-specific quirk**: badge inserted into the content column (not direct child of `<article>`) because the article is a flex/grid container. CSS rule `article[data-testid="tweet"] > .sair-badge` adds defensive fallback (`grid-column: 1 / -1; align-self: flex-start; order: 999; flex-shrink: 0`) in case the article-direct insertion path is taken.

---

## Build & install

```bash
# 1. .env must exist with PANGRAM_API_KEY=...
cp .env.example .env  # then edit and paste your key

# 2. Build
npm run build  # → produces dist/
                # logs apiUrl + key length so you can verify

# 3. Load
# chrome://extensions → Developer mode → Load unpacked → select dist/
```

**When manifest.json's `host_permissions` change** (e.g., adding/removing API hosts): a plain Reload often doesn't re-grant the new origin. **Remove and re-add the unpacked extension** to force a clean grant.

**Test the wiring**: open the popup → click **Test**. A successful response shows `prediction_short`, `fraction_ai`, etc. in a dialog. Errors show the full message — enough to diagnose key/URL/host_permissions problems.

---

## Known issues

See **[BUGS_TODO.md](BUGS_TODO.md)** for the full prioritized list (24 items, P0-P3).

**The current open investigation** (as of 2026-05-12): a user reports ~1 in 10 LinkedIn posts missing badges. Diagnostic results (see "Diagnostic console scripts" below) have ruled out:
- Selector cascade asymmetry (only 1 missed element across all selectors, and it's a job-aggregation card, not a real post)
- The 250-char threshold (zero posts in this bucket on the user's snapshot)
- Body-extraction heuristic failures (zero posts in this bucket)

The remaining hypothesis is the **MutationObserver throttle race**: posts that LinkedIn renders and then virtualizes within the 800ms scan window leave behind empty wrappers (`rawLen: 0`) that we skip as "too-short-raw". Confirmed by one of the virtualized wrappers still containing our HUMAN badge from when it was scored earlier — meaning the system works correctly when a post is in DOM long enough for a scan to fire.

**Proposed fix** (not yet applied): drop the throttle from 800ms to 250ms in [linkedin.js:300](src/content/linkedin.js#L300). One-line change, low risk, fully reversible. If still insufficient, escalate to hybrid scan-on-intersection-enter.

**The very top P0 items** worth tackling first regardless:
1. **Duplicate records across sessions** ([BUGS_TODO.md](BUGS_TODO.md#1) — `seen` is in-memory, so reloading the tab and re-scrolling stores the same post twice)
2. **`chrome.storage.local` 10MB cap unguarded** (silently fails at ~1500-3000 records — add `"unlimitedStorage"` permission and strip `tokens` / `token_probs` from saved Pangram responses)
3. **`DEBUG = true` hardcoded** in both content scripts — console logs include real post snippets (privacy concern for participants)
4. **Stale manifest description** still references Pangram (this one is fine again now since we're back on Pangram)

---

## Diagnostic console scripts

When debugging "extension not working for user X", paste these into the user's DevTools console on `linkedin.com/feed` (or `x.com`).

### 1. Quick state check (small output)

```js
(() => {
  const items = document.querySelectorAll(
    'main [role="list"] [role="listitem"], main [role="listitem"], main [data-id^="urn:li:activity:"]'
  );
  return `listitems/articles=${items.length} · htmlLang=${document.documentElement.lang} · feedURL=${location.href}`;
})();
```

### 2. Full DOM diagnostic (used to identify the variant B issue)

Outputs `pageState`, `selectors`, `extensionFootprint`, `mainStructure`, `altSelectors`, `roleHistogram`, `accountState`. See git log around 2026-05-09 for the full snippet — it's pasted in chat history. Key signals to interpret:
- `mainStructure.innerTextHead` contains `HUMAN` / `AI` / `MIXED` → extension is running and painting
- `altSelectors["main div[componentkey]"]` vs `["main div[data-id*=\"urn:li:activity\"]"]` → tells you which variant
- `roleHistogram["listitem"]` vs `["article"]` → confirms variant

### 3. Cascade verification (proves the cascade is/isn't dropping posts)

Outputs `verdict` field with one of:
- `"OK: cascade is catching all candidate post elements..."`
- `"CONFIRMED: cascade is dropping non-activity URN posts..."`
- `"UNCLEAR: cascade is missing elements but not via URN-type asymmetry..."`

Plus `perSelectorCount`, `missedByURNType`, `missedSamples`. Full snippet in chat history around 2026-05-12.

### 4. Extraction classifier (proves which step drops posts)

Replicates the LinkedIn extraction logic in-page and buckets every cascade-found element:
- `captured` — successfully extracted and sent for scoring
- `too-short-raw` — `innerText < 60` (often virtualized wrappers)
- `no-body-extracted` — `findBodyStart` or footer failed
- `body-too-short` — body < 30 chars (rare)
- `below-250-threshold` — body < 250 chars (intentional skip)

This is the script that revealed the virtualization-race pattern. Full snippet in chat history around 2026-05-12.

---

## Memory files (auto-loaded)

The `/Users/abhinavgothwal/.claude/projects/-Users-abhinavgothwal-Social-AI-Research/memory/` directory holds:
- **`MEMORY.md`** — index of all memory entries
- **`project_overview.md`** — high-level project context (research goals, deliverable)
- **`dom_extraction_notes.md`** — DOM variants for X and LinkedIn (kept current with both variants)

These are auto-loaded into Claude Code sessions in this project.

---

## Things NOT yet built

Worth knowing before scoping new work:

- **Consent gate** on first install (the research methodology requires it for participants)
- **Remote storage backend** — currently local-only; researcher exports manually via popup
- **Exposure-tier annotation** (dwell time / scroll velocity / direction reversal) — distinguishes "post rendered" from "post actually attended to". Architecture sketched in chat history but not implemented.
- **Persistent cross-session dedup** — `seen` Set is in-memory only; reloading the tab causes re-extraction. See BUGS_TODO #1.
- **Rate-limit-aware backoff** for Pangram 429s — currently surfaces as `ERROR` badges. See BUGS_TODO #10.
- **Sapling response shape regression tests** — fixture-based tests would catch upstream API drift.
- **Background-tab data collection** — extension only works when a feed tab is actively open. By design (consent + ToS), but worth re-considering for the study.

---

## Conventions established in chat history

- **All code changes verified with `node --check`** before declaring done. Service workers and content scripts are plain ES module-free JavaScript.
- **Memory files updated** when project state shifts meaningfully (provider switches, DOM variant discoveries, etc.)
- **Diagnostic-first approach** when debugging user-reported issues: write a small console snippet that produces structured JSON, have the user paste it back, interpret. This was how we found Variant B and the throttle race.
- **No emojis in code or commit messages** unless explicitly requested.
- **No new docs files unless asked** — work from chat context, not intermediate files. `BUGS_TODO.md` and this `CLAUDE.md` are explicit user requests.
