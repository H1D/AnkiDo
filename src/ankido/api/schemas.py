# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request/response models for ``/v1``."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ankido.collection.ops import KINDS


class MediaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str | None = Field(default=None, max_length=200)
    data: str | None = None
    path: str | None = None
    url: str | None = None
    fields: list[str] = Field(default_factory=list)


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict[str, str]
    client_id: str | None = Field(default=None, max_length=200)
    tags: list[str] = Field(default_factory=list)
    audio: list[MediaIn] = Field(default_factory=list)
    picture: list[MediaIn] = Field(default_factory=list)
    deck: str | None = None
    model: str | None = None


class NotesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deck: str
    model: str
    notes: list[NoteIn] = Field(min_length=1, max_length=500)
    tags: list[str] = Field(default_factory=list)
    dedupe: Literal["skip", "update", "allow"] = "skip"


class NotesResponse(BaseModel):
    results: list[dict[str, Any]]


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: int
    ease: int = Field(ge=1, le=4)
    client_id: str | None = Field(default=None, max_length=200)
    answered_at: float | None = None
    elapsed_s: float | None = Field(default=None, ge=0)
    time_ms: int = Field(default=0, ge=0)


class ReviewsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviews: list[ReviewIn] = Field(min_length=1, max_length=1000)


class ReviewsResponse(BaseModel):
    results: list[dict[str, Any]]


class WantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decks: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=lambda: list(KINDS))
    limit: int = Field(default=60, ge=1, le=1000)
    max_new_per_day: int | None = Field(default=None, ge=0)
    fields: Literal["compact", "full"] = "compact"
    render: Literal["text", "html"] = "text"


class ExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviews: list[ReviewIn] = Field(default_factory=list, max_length=1000)
    want: WantIn = WantIn()
    sync: Literal["auto", "never"] = "auto"
    sync_timeout_seconds: float = Field(default=20.0, ge=0, le=120)


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force_full: Literal["upload", "download"] | None = None
    confirm: str | None = None
    wait: bool = True
