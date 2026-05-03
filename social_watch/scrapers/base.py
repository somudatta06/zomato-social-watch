"""Abstract scraper interface.

Every concrete scraper:
  - exposes a `name` for logging / db rows
  - implements `fetch()` as an async generator yielding `Post`s
  - optionally implements `health_check()` for fast connectivity probes

The orchestrator handles persistence, dedup, and per-cycle bookkeeping —
scrapers just yield posts.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator

from ..models import Post


@dataclass
class ScraperResult:
    scraper: str
    posts_seen: int = 0
    posts_new: int = 0
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0


class BaseScraper(ABC):
    name: str = "base"

    @abstractmethod
    def fetch(self) -> AsyncIterator[Post]:
        """Async-yield Posts. Subclasses implement as `async def ... yield post`."""
        raise NotImplementedError

    def is_configured(self) -> bool:
        """Return False to make the orchestrator skip this scraper without running it.

        Override for scrapers that need credentials. Default: always configured.
        """
        return True

    async def health_check(self) -> bool:
        """Override for a fast probe that the source is reachable + auth works."""
        return True
