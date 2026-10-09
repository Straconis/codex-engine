# Codex Engine Python Backend

The local API behind the Codex Engine desktop app: it reads PDFs, cleans up their text, stores pages and search chunks in SQLite (FTS5) and answers searches. See the main [README](../README.md) for the whole app.

## Setup

```bash
cd backend
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
```

## Run

The desktop app (`npm run desktop`) starts the backend by itself. To run it on its own, with auto-reload while you edit:

```bash
npm run backend                  # from the repo root; or, in backend/:
uvicorn codex_engine.app:app --host 127.0.0.1 --port 8787 --reload
```

The packaged app runs `server_entry.py` (built into `codex-engine-backend.exe`), which accepts only loopback addresses for `--host` and, with `--exit-with-stdin`, stops when the desktop app closes its stdin (so it never outlives a crashed app).

The database lives in the platform app-data folder (via `platformdirs`):

- Windows: `%LOCALAPPDATA%\Codex Engine\codex-engine.sqlite3`
- macOS: `~/Library/Application Support/Codex Engine/codex-engine.sqlite3`
- Linux: `~/.local/share/Codex Engine/codex-engine.sqlite3`

Set `CODEX_ENGINE_DB` to use another database file.

## Tests and lint

From the repo root:

```bash
python -m pytest backend/tests -q
ruff check backend
```

## Local API safety

- It listens on 127.0.0.1 only.
- Non-GET requests must send `X-Codex-Engine-Client: 1` (the frontend does). That forces a CORS preflight, so web pages open in your browser can't fire cross-site requests at it.
- Requests with a `Host` other than `127.0.0.1`/`localhost` are rejected (DNS-rebinding guard).
- `/api/shutdown` needs the secret token the desktop app passes in `CODEX_ENGINE_SHUTDOWN_TOKEN`.

The desktop app starts the backend on port 8787, or on a free port if 8787 is taken by something else.
