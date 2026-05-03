"""Common data models — every scraper produces `Post` instances."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Source = Literal["reddit", "twitter"]


@dataclass(frozen=True, slots=True)
class Post:
    """Normalized social-media post.

    `id` (= "{source}:{native_id}") is the dedup key in storage.
    """
    source: Source
    native_id: str
    author: str | None
    content: str
    url: str
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.native_id}"

    def to_db_row(self) -> dict[str, Any]:
        ts = self.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return {
            "id": self.id,
            "source": self.source,
            "native_id": self.native_id,
            "author": self.author,
            "content": self.content,
            "url": self.url,
            "created_at": ts.astimezone(timezone.utc).isoformat(),
            "metadata": json.dumps(self.metadata, default=str),
        }
