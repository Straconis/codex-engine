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

The packaging flow is:

1. Build the React frontend into `dist/`.
2. Build `resources/backend/codex-engine-backend.exe` with PyInstaller.
3. Package Electron with the frontend, assets, and backend sidecar.
4. The installed app launches the backend automatically; users do not manage terminals or a browser.

## Architecture note

There are still two processes internally: the Electron UI process and the Python backend process. That is intentional. It isolates long-running PDF ingest/search work from the UI, keeps the backend reusable, and makes Python packaging practical. To the user, it should behave as one desktop program.

