from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SourceItem:
    source_key: str
    external_id: str
    title: str
    url: str
    summary: str = ""
    product_url: str = ""
    author: str = ""
    published_at: datetime = field(default_factory=utc_now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    id: Optional[str] = None
    embedding: Optional[List[float]] = None

    def text_for_matching(self) -> str:
        parts = [self.title, self.summary]
        tags = self.metadata.get("tags") or self.metadata.get("topics") or []
        if isinstance(tags, list):
            parts.append(" ".join(str(tag) for tag in tags[:20]))
        return "\n".join(part.strip() for part in parts if part and part.strip())[:12000]


@dataclass
class Track:
    id: str
    name: str
    goal: str
    embedding: Optional[List[float]] = None


@dataclass
class MatchResult:
    track_id: str
    item_id: str
    score: float
    semantic_score: float
    tier: str
    reason: str
    item: SourceItem
    analysis: str = ""

