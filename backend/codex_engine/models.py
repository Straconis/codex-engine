from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, StrictBool


class SourceRow(BaseModel):
    id: int
    title: str
    path: str
    sha256: str
    pages: int
    enabled: bool
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


class SetEnabledArgs(BaseModel):
    enabled: StrictBool


class StartIngestArgs(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class ResolveDuplicateArgs(BaseModel):
    ingest_id: int
    action: Literal["discard", "replace", "new_copy"]


class OpenPdfArgs(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    page: int = Field(default=1, ge=1)


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


class PageContent(BaseModel):
    """One page as produced by extraction + deterministic cleanup, before storage."""

    raw_text: str
    clean_md: str
    clean_version: int


class PageRow(BaseModel):
    source_id: int
    page_num: int
    raw_text: str | None = None
    clean_md: str
    clean_version: int = 0
    ai_md: str | None = None
    ai_model: str | None = None
    ai_source_hash: str | None = None
    ai_error: str | None = None
    ai_updated_at: str | None = None
    edited_md: str | None = None
    edited_at: str | None = None


class AIFormatArgs(BaseModel):
    model: str | None = None
    force: bool = False  # bypass the cache and ask the model again
    # After "couldn't format some sections": the user chose to keep the cleaned text for them.
    use_clean_for_failed: bool = False


class EditPageArgs(BaseModel):
    markdown: str = Field(max_length=500_000)


class PullModelArgs(BaseModel):
    model: str
