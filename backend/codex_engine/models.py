from __future__ import annotations

from pydantic import BaseModel


class SourceRow(BaseModel):
    id: int
    title: str
    path: str
    sha256: str
    pages: int
    enabled: int
    source_key: str


class ChunkRow(BaseModel):
    page_num: int
    heading: str | None
    body: str
    loc: str


class SearchRow(BaseModel):
    source_id: int
    source_title: str
    source_path: str
    page_num: int
    heading: str | None
    snippet: str
    loc: str | None


class StartIngestArgs(BaseModel):
    path: str


class ResolveDuplicateArgs(BaseModel):
    ingest_id: int
    action: str


class OpenPdfArgs(BaseModel):
    path: str
    page: int


class IngestProgress(BaseModel):
    id: int
    stage: str
    message: str
    current: int
    total: int
    done: bool
    error: str | None = None


class DuplicateDetectedPayload(BaseModel):
    ingest_id: int
    new_path: str
    new_title: str
    sha256: str
    existing_id: int
    existing_title: str
    existing_path: str
