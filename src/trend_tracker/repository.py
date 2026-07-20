import json
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .http import HttpClient
from .models import SourceItem, Track
from .utils import parse_datetime, parse_vector


class SupabaseRepository:
    def __init__(self, url: str, key: str, http: Optional[HttpClient] = None) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.http = http or HttpClient()
        self.headers = {
            "apikey": key,
            "Content-Type": "application/json",
        }
        # Supabase's new sb_secret_* keys are opaque API keys, not JWTs. The
        # gateway derives the service_role from the apikey header. Legacy
        # service_role JWTs still need to be sent as a Bearer token.
        if not key.startswith("sb_secret_"):
            self.headers["Authorization"] = "Bearer {0}".format(key)

    def _rest_url(self, path: str, query: Optional[Dict[str, Any]] = None) -> str:
        url = "{0}/rest/v1/{1}".format(self.url, path.lstrip("/"))
        if query:
            url += "?" + urllib.parse.urlencode(query, safe="(),.*:")
        return url

    def _request(self, method: str, path: str, query: Optional[Dict[str, Any]] = None, payload: Any = None, prefer: str = "") -> Any:
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        raw = self.http.request(method, self._rest_url(path, query), headers=headers, payload=payload, timeout=60)
        return json.loads(raw.decode("utf-8")) if raw else None

    def seed_tracks(self, tracks: Iterable[Dict[str, str]]) -> int:
        count = 0
        for track in tracks:
            existing = self._request("GET", "tracks", {"name": "eq.{0}".format(track["name"]), "select": "id,goal", "limit": 1}) or []
            if existing:
                if existing[0].get("goal") != track["goal"]:
                    self._request(
                        "PATCH",
                        "tracks",
                        {"id": "eq.{0}".format(existing[0]["id"])},
                        {"goal": track["goal"], "embedding": None, "updated_at": datetime.now(timezone.utc).isoformat()},
                        "return=minimal",
                    )
                continue
            self._request("POST", "tracks", payload={"name": track["name"], "goal": track["goal"]}, prefer="return=minimal")
            count += 1
        return count

    def get_tracks(self) -> List[Track]:
        rows = self._request("GET", "tracks", {"active": "eq.true", "select": "id,name,goal,embedding", "order": "created_at.asc"}) or []
        return [Track(id=row["id"], name=row["name"], goal=row["goal"], embedding=parse_vector(row.get("embedding"))) for row in rows]

    def update_track_embedding(self, track_id: str, embedding: List[float]) -> None:
        self._request(
            "PATCH",
            "tracks",
            {"id": "eq.{0}".format(track_id)},
            {"embedding": embedding},
            "return=minimal",
        )

    def upsert_items(self, items: List[SourceItem]) -> int:
        if not items:
            return 0
        total = 0
        for start in range(0, len(items), 100):
            records = []
            for item in items[start : start + 100]:
                records.append(
                    {
                        "source_key": item.source_key,
                        "external_id": item.external_id,
                        "title": item.title,
                        "url": item.url,
                        "product_url": item.product_url or None,
                        "summary": item.summary,
                        "author": item.author or None,
                        "published_at": item.published_at.isoformat(),
                        "fingerprint": item.fingerprint,
                        "metadata": item.metadata,
                        "last_seen_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            self._request(
                "POST",
                "items",
                {"on_conflict": "source_key,external_id"},
                records,
                "resolution=merge-duplicates,return=minimal",
            )
            total += len(records)
        return total

    def get_recent_items(self, since: datetime) -> List[SourceItem]:
        rows: List[Dict[str, Any]] = []
        offset = 0
        while True:
            page = self._request(
                "GET",
                "items",
                {
                    "published_at": "gte.{0}".format(since.isoformat()),
                    "select": "id,source_key,external_id,title,url,product_url,summary,author,published_at,metadata,fingerprint,embedding",
                    "order": "published_at.desc",
                    "limit": 1000,
                    "offset": offset,
                },
            ) or []
            rows.extend(page)
            if len(page) < 1000:
                break
            offset += 1000
        return [
            SourceItem(
                id=row["id"],
                source_key=row["source_key"],
                external_id=row["external_id"],
                title=row["title"],
                url=row["url"],
                product_url=row.get("product_url") or "",
                summary=row.get("summary") or "",
                author=row.get("author") or "",
                published_at=parse_datetime(row["published_at"]),
                metadata=row.get("metadata") or {},
                fingerprint=row.get("fingerprint") or "",
                embedding=parse_vector(row.get("embedding")),
            )
            for row in rows
        ]

    def update_item_embedding(self, item_id: str, embedding: List[float]) -> None:
        self._request("PATCH", "items", {"id": "eq.{0}".format(item_id)}, {"embedding": embedding}, "return=minimal")

    def upsert_matches(self, records: List[Dict[str, Any]]) -> None:
        if not records:
            return
        for start in range(0, len(records), 100):
            self._request(
                "POST",
                "matches",
                {"on_conflict": "track_id,item_id"},
                records[start : start + 100],
                "resolution=merge-duplicates,return=minimal",
            )

    def upsert_analysis(self, track_id: str, item_id: str, content: str, model: str) -> None:
        self._request(
            "POST",
            "analyses",
            {"on_conflict": "track_id,item_id"},
            {"track_id": track_id, "item_id": item_id, "content": content, "model": model},
            "resolution=merge-duplicates,return=minimal",
        )

    def get_latest_digest_since(self, since: datetime) -> Optional[Dict[str, Any]]:
        rows = self._request(
            "GET",
            "digests",
            {
                "generated_at": "gte.{0}".format(since.isoformat()),
                "select": "id,generated_at,content,metadata",
                "order": "generated_at.desc",
                "limit": 1,
            },
        ) or []
        return rows[0] if rows else None

    def save_digest(self, generated_at: datetime, content: str, metadata: Optional[Dict[str, Any]] = None) -> int:
        rows = self._request(
            "POST",
            "digests",
            payload={
                "generated_at": generated_at.isoformat(),
                "content": content,
                "metadata": metadata or {},
            },
            prefer="return=representation",
        ) or []
        if not rows:
            raise RuntimeError("Supabase did not return the saved digest id")
        return int(rows[0]["id"])

    def update_digest_metadata(self, digest_id: int, metadata: Dict[str, Any]) -> None:
        self._request(
            "PATCH",
            "digests",
            {"id": "eq.{0}".format(digest_id)},
            {"metadata": metadata},
            "return=minimal",
        )

    def log_fetch(self, source_key: str, status: str, item_count: int, error: str = "") -> None:
        self._request(
            "POST",
            "fetch_runs",
            payload={"source_key": source_key, "status": status, "item_count": item_count, "error": error[:2000] or None},
            prefer="return=minimal",
        )

    def cleanup(self) -> Dict[str, Any]:
        return self._request("POST", "rpc/cleanup_trend_tracker", payload={}) or {}

    def storage_status(self) -> Dict[str, Any]:
        return self._request("POST", "rpc/trend_tracker_storage_status", payload={}) or {}
