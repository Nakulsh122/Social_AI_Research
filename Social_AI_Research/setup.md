# Setup Guide & Data Storage 

This document explains how to run the Social AI Research project locally or on a server using `tmux`, as well as a breakdown of exactly where and how data is stored across the system.

## 🚀 Running the Project via `tmux`

Since this project consists of a backend API (FastAPI) and a frontend build process (Node.js), using `tmux` is a great way to manage both processes in a single terminal window.

### Prerequisites
- `tmux` installed (`sudo apt install tmux` or `brew install tmux`)
- Python 3.10+ and `pip`
- Node.js (for the build script)
- MongoDB running locally or accessible via URI

### Step-by-Step `tmux` Setup

**1. Start a new tmux session:**
```bash
tmux new -s sair_project
```

**2. Setup and Run the Backend (Pane 1):**
Inside the newly created tmux session, navigate to the backend directory, set up the virtual environment, and start the FastAPI server.

```bash
cd /path/to/Social_AI_Research/backend
python -m venv .venv

# Activate the virtual environment
source .venv/bin/activate  # (On Windows/WSL: source .venv/Scripts/activate)

# Install dependencies
pip install -r requirements.txt

# Create .env from example (make sure to edit it with your PANGRAM_API_KEY and Mongo URI)
cp .env.example .env

# Run the server
uvicorn main:app --reload
```

**3. Split the Window & Build the Extension (Pane 2):**
While the server is running in the first pane, press `Ctrl+B`, then `%` to split the terminal vertically (or `Ctrl+B`, then `"` for a horizontal split).

In this new pane, navigate to the project root to build the extension:

```bash
cd /path/to/Social_AI_Research

# Create .env from example and add your keys
cp .env.example .env

# Build the extension into the dist/ directory
npm run build
```
*(You can leave this second pane open to easily re-run `npm run build` whenever you make changes to the extension's source code).*

**4. Load the Extension in Chrome:**
- Open Chrome and navigate to `chrome://extensions/`
- Enable **Developer Mode** (top right corner)
- Click **Load unpacked**
- Select the `dist/` directory generated in the previous step.

**Helpful `tmux` commands to remember:**
- `Ctrl+B`, then `d`: Detach from the session (leaves it running in the background).
- `tmux attach -t sair_project`: Reattach to the background session.
- `Ctrl+B`, then `Arrow Keys`: Switch between the backend pane and the extension build pane.

---

## 💾 Where is the Data Stored?

Because this is a distributed extension + backend system, data is deliberately split across **two primary data stores**.

### 1. The Client (Participant's Browser)
Data stored here is strictly local to the participant's machine and is used for fast, real-time UI updates (badge rendering).

- **Store location:** `chrome.storage.local`
- **What is stored:** 
  - The participant's authentication token (generated on first load).
  - The `sair_observations` list, which contains the full Pangram score payloads for every post they have seen.
  - The local `queue` state for posts waiting to be scored.
- **Constraints:** Chrome local storage has a soft 10MB cap. The extension frequently rewrites the entire array to storage when a new post is scored. Researchers can manually export this data to a JSON file via the extension's popup UI.

### 2. The Backend (MongoDB Database)
The persistent source of truth for the study's research data. This is where all analytics, metrics, and deduplicated scores live.

- **Store location:** Your configured MongoDB instance (via `db.py`).
- **Collections:**
  - **`participants`**: Stores the randomly generated `participantId`, their authentication `token`, and whether they are assigned to the `control` or `treatment` group.
  - **`scored_posts`**: A heavy caching collection. Stores the raw text, the `postId`, the `textHash`, and the exact response from the Pangram API. This prevents the system from re-calling the paid Pangram API if two users see the exact same post.
  - **`observations`**: An append-only log storing exactly when a specific participant viewed a specific post on their screen.
  - **`interactions`**: An append-only log tracking whenever a participant clicks an engagement button (Like, Repost, Comment) on a scored post.

### 3. External / Third-Party (Pangram API)
- **Store location:** External API servers.
- **What is stored:** The extension sends the raw text of social media posts to the Pangram V3 API for AI detection scoring. Depending on Pangram's data retention policies, this text may be temporarily or permanently stored on their servers.
