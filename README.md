# Codex Engine

Codex Engine is a **local-first desktop app** for **indexing and searching your own TTRPG PDFs** (rulebooks, supplements, SRDs you legally own). It ingests PDFs into a local SQLite database with full-text search, so you can quickly find rules, keywords, and references across multiple books.

> **No PDFs are included.** You supply your own files, and indexing/search happens on your machine.

## What it does

- **Import (ingest) PDFs** and extract text (page-by-page)
- **Chunk + index** extracted text into a local SQLite database (FTS5)
- **Search** across all enabled sources with fast full-text search
- **Duplicate detection** via file hashing (sha256), with options to discard/replace/keep as copy
- **Enable/disable** sources and **delete** sources from the library

## Tech stack

- **Frontend:** React + TypeScript + Vite :contentReference[oaicite:2]{index=2}
- **Desktop runtime:** Tauri (Rust backend) :contentReference[oaicite:3]{index=3}
- **Database:** SQLite + FTS5 (via `rusqlite` bundled build) :contentReference[oaicite:4]{index=4}

## Requirements (Linux)

### Build requirements
- Node.js + npm
- Rust toolchain (stable) + cargo
- Tauri system dependencies (varies by distro; see Tauri docs)

### Runtime / ingest requirements
Codex Engine calls these tools during ingest:
- `sha256sum` (usually from `coreutils`) :contentReference[oaicite:5]{index=5}
- `pdfinfo` and `pdftotext` (from `poppler-utils`) :contentReference[oaicite:6]{index=6}

On Debian/Ubuntu:
```bash
sudo apt update
sudo apt install -y poppler-utils coreutils
