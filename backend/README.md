# Codex Engine Python Backend

Cross-platform local backend for indexing and searching owned TTRPG PDFs.

## Setup

```bash
cd backend
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn codex_engine.app:app --host 127.0.0.1 --port 8787 --reload
```

The API stores its SQLite database in the platform app-data directory via `platformdirs`:

- Windows: `%LOCALAPPDATA%\Codex Engine\codex-engine.sqlite3`
- macOS: `~/Library/Application Support/Codex Engine/codex-engine.sqlite3`
- Linux: `~/.local/share/Codex Engine/codex-engine.sqlite3`

Set `CODEX_ENGINE_DB` to override the database location.


## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## Local API safety

Non-GET requests must send `X-Codex-Engine-Client: 1` (the frontend does this). That forces a CORS preflight, so web pages open in your browser cannot fire cross-site requests at the local API. Requests with a `Host` other than `127.0.0.1`/`localhost` are rejected (DNS-rebinding guard).

The desktop app starts the backend on port 8787, or on a free port if 8787 is taken by something else.
