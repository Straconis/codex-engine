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

