# Codex Engine

Codex Engine is a desktop app for indexing, searching and reading your own tabletop RPG PDFs. Everything stays on your computer: the library, the search index and the optional AI formatting.

It is a Windows app (Electron window, Python backend). The original Rust/Tauri version is kept on the `legacy-rust` branch.

## What it does

- Add PDFs and extract their text page by page
- Search across the books you have turned on (each page is listed once, with its best match)
- Spot a PDF that is already in the library (same file contents) and ask what to do
- Turn books on and off, or delete them
- Read any result page in a built-in reader with cleaned-up formatting, or open the PDF at that page
- Fix a page by hand, or have a local AI model restore its layout (optional, through Ollama)

## Page reader and formatting

Books don't follow a standard layout, so text goes through a pipeline before it is shown or searched:

`PDF -> extraction -> rule-based cleanup -> optional local AI formatting -> stored per page -> reader and search`

**Rule-based cleanup** (`backend/codex_engine/formatting.py`) always runs when a book is added. It:

- removes running headers, footers and page numbers;
- finds headings from font size and rebuilds paragraphs and lists;
- re-joins words hyphenated across lines, keeping real compounds such as "well-known" when the book uses them elsewhere;
- bolds stat-block labels and turns ability scores into a table.

Search is built from this cleaned text, or from your own edit of a page if you made one.

**Storage.** Each page keeps its raw extraction, its cleaned Markdown (with the cleanup version that made it) and, if made, its AI-formatted version (with the model and a hash of the text it came from). Books added by older versions upgrade themselves the first time a page is opened: pages and search are rebuilt from the PDF, and existing AI versions and edits are kept. If the PDF at that path now has fewer pages and you had edited or AI formatted one of the missing pages, nothing is rebuilt.

**Reader.** Click a search result (or tab to it and press Enter) to open the page. It shows the best version available: your edit, else AI formatted, else cleaned, else raw. You can switch between them, AI format or reformat the page, fix the page by hand (undoing your edit brings the other versions back), and open the original PDF.

### AI formatting (optional)

`backend/codex_engine/ai_format.py` sends the page, in sections, to a local model through [Ollama](https://ollama.com). Text never leaves the computer.

- The model is told to restore structure only (paragraphs, headings, lists, quotations, section breaks, tables) and never to summarise, paraphrase, censor or rewrite.
- Long pages are split into sections. A paragraph too long for one section (a big table or stat block) is split at line breaks, then at sentence ends, and put back together exactly.
- Every section is checked against its source and rejected (keeping its cleaned text) if the model:
  - changed, added, dropped or reordered any word, number, punctuation mark or symbol (a footnote asterisk, "#3", a "______" blank);
  - split or merged words ("can not" to "cannot"; only a word split across a line break may be re-joined) or merged numbers ("1 0" to "10");
  - removed a hyphen that isn't a word split across a line ("1-2", "ten-foot").
- Accepted output is cached by content, so the same text is never sent twice. Cached and saved AI output is re-checked against the current rules: anything that no longer passes is formatted again, or (for a saved page) the reader shows the cleaned text with a notice and a Reformat button.
- **Check AI pages** on a book lists its pages that fail the current check or were made from older cleaned text, and **Reformat all** reformats them one at a time.
- Without Ollama the app works normally and the reader says AI formatting is off.

### Setting up AI formatting (per computer)

AI settings are stored per computer, in `settings.json` in the app data folder, so one PC can keep its models on the system drive and another on a second drive. Everything is set in the app under **Settings**:

1. Install [Ollama](https://ollama.com), in any folder or on any drive. Codex Engine finds it, or you can pick `ollama.exe` with **Browse…**.
2. Choose a **model storage folder** with room for models (about 1–5 GB each), for example `D:\ollama\models`.
3. Click **Download** to fetch a model (default `qwen2.5:1.5b`). Progress shows in the panel.

By default **Codex Engine runs Ollama itself** (`ollama serve` with `OLLAMA_MODELS` set to your folder), on its own port (11435), so it never clashes with an Ollama you run separately.

- It stops when the app closes. If the app crashes or is ended from Task Manager, the backend notices and stops too, taking Ollama with it.
- If the model drive isn't connected or Ollama isn't installed yet, the app says so and keeps working without AI. It tries again (at most every 10 seconds) the next time AI is needed, so no restart is needed.
- Saving Settings restarts Ollama only when a setting it runs with changed (not for a different model).

Alternatively, choose **I run Ollama myself** and enter its address (default `http://127.0.0.1:11434`, or another machine on your network).

## Your data

The library, settings and logs live in `%LOCALAPPDATA%\Codex Engine` (the **Codex Engine Data Folder** shortcut in the Start menu opens it). The window's own logs (`ui.log`, and the backend logs of the installed app) are in `%APPDATA%\Codex Engine\logs`. PDFs you upload are copied into the data folder's `uploads` folder; the copy is deleted when you delete the book.

## Tech stack

- Desktop shell: Electron
- Frontend: React, TypeScript, Vite
- Backend: Python, FastAPI, PyMuPDF, SQLite with FTS5, platformdirs
- Optional AI: Ollama
- Packaging: PyInstaller (backend and updater), electron-builder (app folder), Inno Setup (installer)

## Development

You need Node.js 22, Python 3.12 or newer and, for the installer, Windows with Inno Setup 6.

```bash
npm install
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Run the UI dev server and the desktop shell in two terminals:

```bash
npm run dev
npm run desktop
```

`npm run desktop` opens the app window and starts the backend (`backend/server_entry.py`) with `backend/.venv`. To work on the UI in a browser instead, run the backend yourself with `npm run backend` (auto-reloads; uses the `python` on your PATH) and open `http://127.0.0.1:1420`.

Environment variables for development (none are needed normally):

| Variable | What it does |
| --- | --- |
| `CODEX_ENGINE_USE_BUILD=1` | The desktop shell shows the built UI from `dist/` instead of the dev server, as the installed app does. |
| `CODEX_ENGINE_PORT` | Backend port (default 8787; the app picks a free one if it's taken). |
| `CODEX_ENGINE_FRONTEND_URL` | Dev server address (default `http://127.0.0.1:1420`). |
| `CODEX_ENGINE_PYTHON` | Python used to run the backend (and by the Windows build scripts). |
| `CODEX_ENGINE_BACKEND` | Run this backend program instead. |
| `CODEX_ENGINE_UPDATER_EXE`, `CODEX_ENGINE_APP_EXE` | Paths the updater uses, for testing updates. |
| `CODEX_ENGINE_DB` | Database file to use. |
| `CODEX_ENGINE_SETTINGS` | Settings file to use. |
| `CODEX_ENGINE_OLLAMA_URL` | Use this Ollama and don't manage one. |
| `CODEX_ENGINE_OLLAMA_MODEL` | AI model to use. |
| `CODEX_ENGINE_AI_SECTION_CHARS`, `_AI_NUM_CTX`, `_AI_TIMEOUT`, `_AI_TEMPERATURE`, `_AI_RETRY_TEMPERATURE`, `_AI_ATTEMPTS` | AI tuning (defaults 3000, 8192, 300 s, 0, 0.3, 4). |
| `VITE_CODEX_ENGINE_API` | Backend address for the UI when it runs in a browser (the desktop app tells the UI itself). |

On Windows, a Microsoft Store Python redirects the backend's app data to `%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.*\LocalCache\Local\Codex Engine`. The installed app uses the normal `%LOCALAPPDATA%\Codex Engine`.

### Tests, lint and CI

```bash
python -m pytest backend/tests -q   # backend tests
ruff check backend                  # Python lint
npm run lint                        # TypeScript and Electron lint (ESLint)
npm run build                       # type-check and build the UI
node scripts/check-version.mjs      # the version matches everywhere and has release notes
```

GitHub Actions (`.github/workflows/ci.yml`) runs all of these on every pull request and every push to `main`. The backend tests run on Windows and Linux, and a Windows job also checks the build scripts and compiles the installer script. A pull request shows a green tick when everything passes.

## Building the Windows installer

```powershell
npm run dist:win:inno
```

This:

1. builds the UI into `dist/`;
2. builds `resources\backend\codex-engine-backend.exe` and `resources\updater\codex-engine-updater.exe` with PyInstaller;
3. packages the app into `release\electron\win-unpacked` with electron-builder;
4. builds `release\installer\CodexEngineSetup-<version>.exe` with Inno Setup.

If `release\electron\win-unpacked` is already up to date, `npm run installer:inno` builds only the installer. This installer is the only one: the in-app updater and **Settings > Uninstall** rely on it.

### Versions and releases

The version lives in `package.json`, `package-lock.json` (twice) and `backend/codex_engine/config.py` (`APP_VERSION`); the installer reads it from `package.json` when it is built. Each version also gets a `release-notes-<version>.txt`. `scripts/check-version.mjs` (and CI) fail if any of these disagree or the notes are missing.

The app checks the latest GitHub release for a newer `CodexEngineSetup-<version>.exe` and can install it from **Settings > Updates**.

## How it fits together

There are two processes: the Electron window and the Python backend it starts. That keeps long PDF work away from the window and makes Python packaging practical; to the user it is one program.

- The backend listens on this computer only (it refuses any non-loopback `--host`) and stops when the window's process ends, even after a crash.
- The window only ever shows the app itself: dropped files, links and pop-ups can't take it elsewhere, the page may only run its own scripts (a Content-Security-Policy), and desktop features (file pickers, uninstall) answer only the app's own page.
- Uninstalling from Settings asks for confirmation in a Windows dialog.
