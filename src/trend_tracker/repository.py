import json
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .http import HttpClient
from .models import BetaUser, MatchResult, SourceItem, Track
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
            existing = self._request(
                "GET",
                "tracks",
                {
                    "beta_user_id": "is.null",
                    "name": "eq.{0}".format(track["name"]),
                    "select": "id,goal",
                    "limit": 1,
                },
            ) or []
            if existing:
                if existing[0].get("goal") != track["goal"]:
                    self._request(
                        "PATCH",
                        "tracks",
                        {"id": "eq.{0}".format(existing[0]["id"])},
                        {
                            "goal": track["goal"],
                            "compiled_goal": "",
                            "goal_spec": {},
                            "embedding": None,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                        "return=minimal",
                    )
                continue
            self._request("POST", "tracks", payload={"name": track["name"], "goal": track["goal"]}, prefer="return=minimal")
            count += 1
        return count

    def seed_beta_users(self, users: Iterable[Dict[str, Any]]) -> int:
        users = list(users)
        created = 0
        imported_user_ids = []
        for seed in users:
            rows = self._request(
                "GET",
                "beta_users",
                {"email": "eq.{0}".format(seed["email"]), "select": "id", "limit": 1},
            ) or []
            payload = {
                "email": seed["email"],
                "display_name": seed.get("display_name") or "",
                "timezone": seed.get("timezone") or "Asia/Shanghai",
                "wechat_enabled": bool(seed.get("wechat_enabled")),
            }
            if rows:
                user_id = rows[0]["id"]
                self._request(
                    "PATCH",
                    "beta_users",
                    {"id": "eq.{0}".format(user_id)},
                    payload,
                    "return=minimal",
                )
            else:
                inserted = self._request(
                    "POST",
                    "beta_users",
                    payload={**payload, "active": False},
                    prefer="return=representation",
                ) or []
                if not inserted:
                    raise RuntimeError("Supabase did not return the beta user id")
                user_id = inserted[0]["id"]
                created += 1
            imported_user_ids.append(str(user_id))
            self._seed_beta_tracks(str(user_id), seed.get("tracks") or [])

        # Change active recipients only after every user and Track was imported.
        # A failed one-time import therefore cannot pause the existing audience.
        imported_user_id_set = set(imported_user_ids)
        for user_id in imported_user_ids:
            self._request(
                "PATCH",
                "beta_users",
                {"id": "eq.{0}".format(user_id)},
                {"active": True},
                "return=minimal",
            )
        active_rows = self._request(
            "GET",
            "beta_users",
            {"active": "eq.true", "select": "id"},
        ) or []
        for row in active_rows:
            user_id = str(row["id"])
            if user_id not in imported_user_id_set:
                self._request(
                    "PATCH",
                    "beta_users",
                    {"id": "eq.{0}".format(user_id)},
                    {"active": False},
                    "return=minimal",
                )
        return created

    def _seed_beta_tracks(self, beta_user_id: str, tracks: Iterable[Dict[str, str]]) -> None:
        tracks = list(tracks)
        imported_track_ids = []
        for track in tracks:
            rows = self._request(
                "GET",
                "tracks",
                {
                    "beta_user_id": "eq.{0}".format(beta_user_id),
                    "name": "eq.{0}".format(track["name"]),
                    "select": "id,goal",
                    "limit": 1,
                },
            ) or []
            if rows:
                track_id = str(rows[0]["id"])
                update_payload: Dict[str, Any] = {}
                if rows[0].get("goal") != track["goal"]:
                    update_payload.update(
                        {
                            "goal": track["goal"],
                            "compiled_goal": "",
                            "goal_spec": {},
                            "embedding": None,
                        }
                    )
                if update_payload:
                    self._request(
                        "PATCH",
                        "tracks",
                        {"id": "eq.{0}".format(track_id)},
                        update_payload,
                        "return=minimal",
                    )
                imported_track_ids.append(track_id)
                continue
            inserted = self._request(
                "POST",
                "tracks",
                payload={
                    "beta_user_id": beta_user_id,
                    "name": track["name"],
                    "goal": track["goal"],
                    "active": False,
                },
                prefer="return=representation",
            ) or []
            if not inserted:
                raise RuntimeError("Supabase did not return the beta Track id")
            imported_track_ids.append(str(inserted[0]["id"]))

        imported_track_id_set = set(imported_track_ids)
        for track_id in imported_track_ids:
            self._request(
                "PATCH",
                "tracks",
                {"id": "eq.{0}".format(track_id)},
                {"active": True},
                "return=minimal",
            )
        active_rows = self._request(
            "GET",
            "tracks",
            {
                "beta_user_id": "eq.{0}".format(beta_user_id),
                "active": "eq.true",
                "select": "id",
            },
        ) or []
        for row in active_rows:
            track_id = str(row["id"])
            if track_id not in imported_track_id_set:
                self._request(
                    "PATCH",
                    "tracks",
                    {"id": "eq.{0}".format(track_id)},
                    {"active": False},
                    "return=minimal",
                )

    def get_beta_users(self) -> List[BetaUser]:
        rows = self._request(
            "GET",
            "beta_users",
            {
                "active": "eq.true",
                "select": "id,email,display_name,timezone,wechat_enabled",
                "order": "created_at.asc",
            },
        ) or []
        return [
            BetaUser(
                id=str(row["id"]),
                email=str(row["email"]),
                display_name=str(row.get("display_name") or ""),
                timezone=str(row.get("timezone") or "Asia/Shanghai"),
                wechat_enabled=bool(row.get("wechat_enabled")),
            )
            for row in rows
        ]

    def has_beta_users(self) -> bool:
        rows = self._request(
            "GET",
            "beta_users",
            {
                "select": "id",
                "limit": 1,
            },
        ) or []
        return bool(rows)

    def get_tracks(self, beta_user_id: Optional[str] = None) -> List[Track]:
        query: Dict[str, Any] = {
            "active": "eq.true",
            "select": "id,beta_user_id,name,goal,compiled_goal,goal_spec,embedding",
            "order": "created_at.asc",
            "beta_user_id": (
                "eq.{0}".format(beta_user_id) if beta_user_id else "is.null"
            ),
        }
        rows = self._request("GET", "tracks", query) or []
        return [
            Track(
                id=row["id"],
                name=row["name"],
                goal=row["goal"],
                embedding=parse_vector(row.get("embedding")),
                beta_user_id=row.get("beta_user_id"),
                compiled_goal=str(row.get("compiled_goal") or ""),
                goal_spec=row.get("goal_spec") or {},
            )
            for row in rows
        ]

    def update_compiled_goal(
        self, track_id: str, compiled_goal: str, goal_spec: Dict[str, Any]
    ) -> None:
        self._request(
            "PATCH",
            "tracks",
            {"id": "eq.{0}".format(track_id)},
            {
                "compiled_goal": compiled_goal,
                "goal_spec": goal_spec,
                "embedding": None,
            },
            "return=minimal",
        )

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

    def get_feedback_examples(
        self, beta_user_id: str, track_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        if not beta_user_id or not track_id:
            return []
        return self._request(
            "GET",
            "feedback",
            {
                "beta_user_id": "eq.{0}".format(beta_user_id),
                "track_id": "eq.{0}".format(track_id),
                "select": "value,note,items(title,summary)",
                "order": "created_at.desc",
                "limit": max(1, min(limit, 50)),
            },
        ) or []

    def get_latest_digest_since(
        self, since: datetime, beta_user_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        query: Dict[str, Any] = {
            "generated_at": "gte.{0}".format(since.isoformat()),
            "select": "id,generated_at,content,metadata",
            "order": "generated_at.desc",
            "limit": 1,
            "beta_user_id": (
                "eq.{0}".format(beta_user_id) if beta_user_id else "is.null"
            ),
        }
        rows = self._request(
            "GET",
            "digests",
            query,
        ) or []
        return rows[0] if rows else None

    def save_digest(
        self,
        generated_at: datetime,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        beta_user_id: Optional[str] = None,
    ) -> int:
        rows = self._request(
            "POST",
            "digests",
            payload={
                "generated_at": generated_at.isoformat(),
                "content": content,
                "metadata": metadata or {},
                "beta_user_id": beta_user_id,
            },
            prefer="return=representation",
        ) or []
        if not rows:
            raise RuntimeError("Supabase did not return the saved digest id")
        return int(rows[0]["id"])

    def record_digest_items(
        self, digest_id: int, matches: Iterable[MatchResult]
    ) -> None:
        rows = []
        for position, match in enumerate(matches, 1):
            rows.append(
                {
                    "digest_id": digest_id,
                    "track_id": match.track_id,
                    "item_id": match.item_id,
                    "position": position,
                    "section": "highlight" if position <= 3 else "related",
                }
            )
        if rows:
            self._request(
                "POST",
                "digest_items",
                {"on_conflict": "digest_id,track_id,item_id"},
                rows,
                "resolution=merge-duplicates,return=minimal",
            )

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
