# Software Requirements & System Architecture Specification (SRS)

This document provides a detailed, step-by-step technical explanation of the Social AI Research project. It breaks down the system lifecycle from the moment a user scrolls to a post, all the way to how the data is processed, scored, stored, and visualized.

---

## 1. System Initialization & Authentication

When the extension is first installed, it needs to establish a secure identity with the backend without requiring the user to create an account.

### 1.1 Extension Bootstrapping
- **File:** `src/manifest.json`
- **Mechanism:** Defines the extension permissions (`storage`, `host_permissions`) and tells Chrome to automatically inject `twitter.js` and `linkedin.js` when the user navigates to those sites.

### 1.2 Registration & Identity
- **File:** `backend/main.py`
- **Function:** `@app.post("/register")`
- **Flow:** 
  1. The extension background script calls `/register` on its first run.
  2. The backend generates a secure `participantId` (UUID) and a `token`.
  3. **A/B Testing:** The user is randomly assigned to either a `control` or `treatment` group based on `CONTROL_GROUP_PROBABILITY` in the `.env` file.
  4. The identity is saved to MongoDB (`participants` collection).
  5. The extension stores this token in `chrome.storage.local` and passes it in the `Authorization: Bearer <token>` header for all future requests.

---

## 2. Data Collection (Scraping & Observing)

As the user scrolls through their feed, the extension actively scans the page for new posts.

### 2.1 The MutationObserver (The Trigger)
- **Files:** `src/content/twitter.js` & `src/content/linkedin.js`
- **Function:** `scan()`
- **Flow:** A throttled `MutationObserver` watches the `document.body` for changes. Every ~500-800ms during scrolling, it fires the `scan()` function to look for new post elements.

### 2.2 Finding & Extracting the Post
- **Files:** `src/content/twitter.js` & `src/content/linkedin.js`
- **Functions:** `extractListItem()` (LinkedIn), `extractTweet()` (Twitter)
- **Flow:** 
  1. **Selector Cascade:** The script attempts to find posts using various CSS selectors (handling LinkedIn's multiple DOM variants).
  2. **Extraction:** It strips the text from the post and parses engagement numbers (e.g., `"1.2K Likes"`) using the `parseCount()` utility located in `src/content/shared.js`.
  3. **Validation:** It checks if the text is longer than 250 characters. If it's shorter, it immediately flags it as `TOO SHORT` and ignores it.
  4. **ID Generation:** It calls `getOrComputeKey()` to assign a stable `postId` to the DOM element so it isn't processed twice.

### 2.3 Viewport Tracking (Prioritization)
- **File:** `src/content/shared.js`
- **Mechanism:** `IntersectionObserver`
- **Flow:** Users scroll faster than the API can score posts. The `IntersectionObserver` watches the extracted posts and calculates a priority score (e.g., 100 for posts fully in view, lower for posts scrolled past). 
- **Action:** Calls `sendPost(post, priority)`, firing a `POST_OBSERVED` message to the background Service Worker. Priority updates are sent in batches via `POST_PRIORITY_BATCH`.

---

## 3. Queue Management & API Communication

The Service Worker acts as the middleman, pacing requests to the backend to avoid rate limits.

### 3.1 The Priority Queue
- **File:** `src/background.js`
- **State:** `queue` (A Javascript Map)
- **Flow:** 
  1. Receives `POST_OBSERVED` messages and places them in the `queue`.
  2. Updates priorities in real-time when it receives `POST_PRIORITY_BATCH` messages.
  3. **Concurrency:** Up to 2 concurrent workers loop continuously, calling `dequeueHighest()` to find the post with the highest priority score, processing the most visible posts first.

### 3.2 Scoring the Post
- **File:** `backend/main.py`
- **Function:** `@app.post("/score")`
- **Flow:** 
  1. The Service worker sends the `postId` and text to `/score`.
  2. **Rate Limiting:** `backend/rate_limit.py` ensures the user isn't spamming requests.
  3. **Cache Check:** The backend checks the `scored_posts` MongoDB collection for an exact `postId` match, or a `textHash` match (using `_hash_text` to catch copy-pasted posts). If found, it returns the cached score to save money.
  4. **Pangram Inference:** If it's a cache miss, it calls `pangram.score_text()` (`backend/pangram.py`), which forwards the text to `text.api.pangram.com`.
  5. The result is saved to the MongoDB cache and returned to the Service Worker.

---

## 4. UI Updates & Badging

Once the score is retrieved, the UI must be updated so the participant can see the result.

### 4.1 Saving to Local Storage
- **File:** `src/background.js`
- **Flow:** The worker receives the score, injects it into a master array of observations, and safely writes it to `chrome.storage.local` using a `writeChain` promise to prevent race conditions.

### 4.2 Painting the Badge
- **Files:** `src/content/twitter.js` & `src/content/linkedin.js`
- **Function:** `paintBadge(item)`
- **Flow:** 
  1. The content scripts listen for `chrome.storage.onChanged` events.
  2. When the storage updates, they call `paintAll()`, which iterates over every visible post and calls `paintBadge()`.
  3. **Classification:** It calls `tierFromDetector()` to parse the Pangram response and assign a category (`Human`, `Mixed`, or `AI`).
  4. **DOM Injection:** It dynamically creates a `div` with the class `.sair-badge` and injects it into the post. 
     - *Quirk:* On Twitter, it uses `findContentColumn()` to ensure the badge doesn't break the CSS grid layout.
- **Styling:** Controlled entirely by `src/content/badge.css`.

---

## 5. Telemetry & Analytics

The system logs user behavior for downstream research analysis.

### 5.1 Observation Logging
- **File:** `backend/main.py`
- **Function:** `@app.post("/observations")`
- **Flow:** Whenever a post enters the user's viewport, the extension batches these "views" and sends them to the backend, appending them to the `observations` MongoDB collection.

### 5.2 Interaction Logging
- **File:** `backend/main.py`
- **Function:** `@app.post("/interactions")`
- **Flow:** If a user clicks Like, Comment, or Repost on a scored post, the extension catches this click and logs it in the `interactions` MongoDB collection, allowing researchers to correlate AI scores with user engagement.

