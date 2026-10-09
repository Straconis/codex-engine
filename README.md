# Codex Engine

Codex Engine is a local-first desktop app for indexing and searching your own TTRPG PDFs.

This branch refactors the original Linux-oriented Tauri/Rust backend into a cross-platform Python backend with a React/Vite frontend and an Electron desktop shell.

## What it does

- Import PDFs and extract text page-by-page
- Chunk and index extracted text into a local SQLite FTS5 database
- Search across enabled sources
- Detect duplicate PDFs by SHA-256
- Enable, disable, and delete sources
- Open a PDF at a result page through the host OS
- Read any result page in a built-in reader with cleaned-up formatting
- Optionally reformat a page with a local AI model (Ollama)

## Page reader and formatting

Third-party books don't follow a standard layout, so text goes through a formatting pipeline before it is shown or searched:

`PDF -> extraction -> deterministic cleanup -> optional local AI formatting -> stored per page -> reader / search`

**Deterministic cleanup** (`backend/codex_engine/formatting.py`) always runs at ingest. It removes running headers, footers and page numbers, finds headings from font size, rebuilds paragraphs and lists, re-joins words hyphenated across lines (keeping real compounds such as "well-known" when the book uses them elsewhere), bolds stat-block labels and turns ability scores into a table. Search chunks are built from this cleaned text.

**Storage.** Each page keeps its raw extraction, its cleaned Markdown (with the formatter version that made it) and, if generated, its AI-formatted version (with the model used and a hash of the text it was made from). Books from older versions upgrade themselves the first time they are opened in the reader: pages and search chunks are rebuilt from the PDF, and AI output already generated is kept.

**Reader.** Click a search result to open the page. It shows the best available version: AI formatted, else cleaned, else raw. You can switch between the three, AI format or regenerate the page, and open the original PDF.

**AI formatting (optional)** (`backend/codex_engine/ai_format.py`) sends the page, in sections, to a local model through [Ollama](https://ollama.com). Text never leaves the machine. The model is told to restore structure only (paragraphs, headings, lists, quotations, section breaks, tables) and never to summarise, paraphrase, censor or rewrite. Long pages are split into sections; a paragraph too long for one section (a big table or stat block) is split at line breaks, then at sentence ends, and put back together exactly. Every section is checked against its source: changing, adding or dropping any word or punctuation mark, reordering text, merging numbers ("1 0" to "10") or removing a hyphen that isn't a word split across a line ("1-2", "ten-foot") gets that section rejected, and it keeps its cleaned version. The only text the model may drop is a page number on a line of its own. Accepted output is cached by content, so the same text is never sent to the model twice, and cached or saved AI output is re-checked against the current rules: anything that no longer passes is formatted again, or (for a saved page) the reader shows the cleaned text with a notice and a Reformat button. One page can only be AI formatted once at a time. To find every outdated AI page in a book, click **Check AI pages** on the book in the Sources list: it lists the pages that fail the current check or were made from older cleaned text, and **Reformat all** reformats them one at a time (pages where the AI still can't format part of the text are left for you to decide on in the reader). Without Ollama the app works normally and the reader says AI formatting is off.

### Setting up AI formatting (per computer)

AI settings are stored per computer, in `settings.json` in the app data folder. One machine can keep its models on the system drive and another on a second or companion drive. Everything is set in the app under **Settings**:

1. Install [Ollama](https://ollama.com), in any folder or on any drive. Codex Engine finds it automatically, or you can pick `ollama.exe` with **Browse…**.
2. Choose a **model storage folder** with room for models (about 1–5 GB each), e.g. `D:\ollama\models`.
3. Click **Download** to fetch a model (default `qwen2.5:1.5b`). Progress shows in the panel.

By default **Codex Engine runs Ollama itself** (`ollama serve` with `OLLAMA_MODELS` set to your folder). It runs on its own port (11435), so it never conflicts with an Ollama you run separately, and it stops when the app closes. On Windows, Ollama and its model runners sit in a Job Object, so they also stop if the app crashes. If the model folder's drive isn't connected, the app says so and keeps working without AI.

Alternatively, choose **I run Ollama myself** and enter its address (default `http://127.0.0.1:11434`, or another machine on your network).

Developer overrides (environment variables, take precedence over Settings):

- `CODEX_ENGINE_SETTINGS`: path of the settings file
- `CODEX_ENGINE_OLLAMA_URL`: use this Ollama and don't manage one
- `CODEX_ENGINE_OLLAMA_MODEL`: model to use
- `CODEX_ENGINE_AI_SECTION_CHARS` (default 3000), `CODEX_ENGINE_AI_NUM_CTX` (8192), `CODEX_ENGINE_AI_TIMEOUT` (300 s), `CODEX_ENGINE_AI_TEMPERATURE` (0), `CODEX_ENGINE_AI_RETRY_TEMPERATURE` (0.3), `CODEX_ENGINE_AI_ATTEMPTS` (4)

Note for development on Windows: if the backend runs on a Microsoft Store Python (as the dev `.venv` here does), Windows redirects its app data (database, settings, logs) to `%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.*\LocalCache\Local\Codex Engine`. The packaged app uses the normal `%LOCALAPPDATA%\Codex Engine`.

## Tech stack

- Desktop shell: Electron
- Frontend: React + TypeScript + Vite
- Backend: Python + FastAPI
- PDF extraction: PyMuPDF
- Database: SQLite + FTS5
- Cross-platform paths: platformdirs

## Development

Install frontend dependencies:

```bash
npm install
```

Install backend dependencies:

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

During development, run the renderer and desktop shell in separate terminals:

```bash
npm run dev
```

```bash
npm run desktop
```

`npm run desktop` opens a Codex Engine desktop window and starts the Python backend automatically from `backend/.venv`.

You can still open `http://127.0.0.1:1420` directly for browser debugging.

Set `VITE_CODEX_ENGINE_API` if the backend is not on `http://127.0.0.1:8787`.

### Tests and CI

Run the backend tests from the repo root (install `backend/requirements-dev.txt` as well as `requirements.txt` first):

```bash
python -m pytest backend/tests -q
```

`npm run build` type-checks and builds the frontend.

GitHub Actions (`.github/workflows/ci.yml`) runs both on every pull request and on every push to `python-refactor`: the backend tests on Windows and Linux, and the frontend build. A pull request shows a green tick when they pass and a red cross when something broke.

## Windows packaging

The end-user app should be a single launcher. Electron owns the window and starts a bundled Python backend sidecar.

Build the backend sidecar:

```powershell
cd C:\Projects\codex-engine
npm run build:backend:win
```

Build the full Windows installer with Electron Builder/NSIS:

```powershell
npm run dist:win
```

Build the full Windows installer with Inno Setup 6:

```powershell
npm run dist:win:inno
```

If you already have `release\win-unpacked`, build only the Inno installer:

```powershell
npm run installer:inno
```

The app version lives in `package.json`. `build-installer-inno.ps1` passes it to Inno Setup, and `build-backend-win.ps1` fails if `backend/codex_engine/config.py` `APP_VERSION` doesn't match, so bump both together (along with `package-lock.json` and `installer/codex-engine.iss`), and add a `release-notes-<version>.txt`.

The packaging flow is:

1. Build the React frontend into `dist/`.
2. Build `resources/backend/codex-engine-backend.exe` with PyInstaller.
3. Package Electron with the frontend, assets, and backend sidecar.
4. The installed app launches the backend automatically; users do not manage terminals or a browser.

## Architecture note

There are still two processes internally: the Electron UI process and the Python backend process. That is intentional. It isolates long-running PDF ingest/search work from the UI, keeps the backend reusable, and makes Python packaging practical. To the user, it should behave as one desktop program.

