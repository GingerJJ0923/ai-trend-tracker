import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from .ai import AIService, candidate_scores
from .config import Settings
from .connectors import collect_source
from .http import HttpClient
from .models import MatchResult, SourceItem, Track
from .report import build_report, send_email, write_report
from .repository import SupabaseRepository


def make_services(settings: Settings):
    settings.require_supabase()
    http = HttpClient()
    repository = SupabaseRepository(settings.supabase_url, settings.supabase_key, http)
    ai = AIService(
        chat_api_key=settings.chat_api_key,
        chat_base_url=settings.chat_base_url,
        embedding_api_key=settings.embedding_api_key,
        embedding_base_url=settings.embedding_base_url,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        ranking_model=settings.ranking_model,
        analysis_model=settings.analysis_model,
        http=http,
    )
    return http, repository, ai


def deduplicate_observations(items: List[SourceItem]) -> List[SourceItem]:
    grouped: Dict[str, SourceItem] = {}
    sources: Dict[str, set] = {}
    for item in sorted(items, key=lambda row: row.published_at, reverse=True):
        key = item.fingerprint or "{0}:{1}".format(item.source_key, item.external_id)
        sources.setdefault(key, set()).add(item.source_key)
        if key not in grouped:
            grouped[key] = item
    for key, item in grouped.items():
        item.metadata = dict(item.metadata)
        item.metadata["related_sources"] = sorted(sources[key])
    return list(grouped.values())


def collect(settings: Settings, dry_run: bool = False) -> int:
    http = HttpClient()
    repository = None
    if not dry_run:
        settings.require_supabase()
        repository = SupabaseRepository(settings.supabase_url, settings.supabase_key, http)
    total = 0
    successful_sources = 0
    preview = []
    for source in settings.sources():
        key = source.get("key", source.get("type", "unknown"))
        try:
            items = collect_source(source, http, settings.lookback_hours, settings.github_token, settings.hn_item_limit)
            unique = {}
            for item in items:
                unique[(item.source_key, item.external_id)] = item
            items = list(unique.values())
            total += len(items)
            successful_sources += 1
            if repository:
                repository.upsert_items(items)
                repository.log_fetch(key, "success", len(items))
            preview.extend(items[:3])
            print("{0}: collected {1} items".format(key, len(items)))
        except Exception as exc:
            print("{0}: FAILED: {1}".format(key, exc))
            if repository:
                repository.log_fetch(key, "failed", 0, str(exc))
    if dry_run:
        print(json.dumps([{"source": item.source_key, "title": item.title, "url": item.url} for item in preview], ensure_ascii=False, indent=2))
    if successful_sources == 0:
        raise RuntimeError("All configured sources failed; inspect the source errors above")
    print("Total collected observations: {0}".format(total))
    return total


def seed_tracks(settings: Settings) -> int:
    _, repository, _ = make_services(settings)
    created = repository.seed_tracks(settings.seed_tracks())
    print("Created {0} new Tracks".format(created))
    return created


def _ensure_embeddings(repository: SupabaseRepository, ai: AIService, tracks: List[Track], items: List[SourceItem]) -> None:
    if not ai.embeddings_enabled:
        return
    missing_tracks = [track for track in tracks if not track.embedding]
    if missing_tracks:
        vectors = ai.embeddings([track.goal for track in missing_tracks])
        for track, vector in zip(missing_tracks, vectors):
            track.embedding = vector
            repository.update_track_embedding(track.id, vector)

    missing_items = [item for item in items if not item.embedding]
    if missing_items:
        vectors = ai.embeddings([item.text_for_matching() for item in missing_items])
        for item, vector in zip(missing_items, vectors):
            item.embedding = vector
            repository.update_item_embedding(str(item.id), vector)


def digest(settings: Settings) -> str:
    http, repository, ai = make_services(settings)
    repository.seed_tracks(settings.seed_tracks())
    tracks = repository.get_tracks()
    if not tracks:
        raise RuntimeError("No active Tracks. Configure TRACKS_JSON or insert a row into the tracks table.")

    since = datetime.now(timezone.utc) - timedelta(hours=settings.lookback_hours)
    observations = repository.get_recent_items(since)
    items = deduplicate_observations(observations)
    _ensure_embeddings(repository, ai, tracks, items)

    matches_by_track: Dict[str, List[MatchResult]] = {}
    trends_by_track: Dict[str, str] = {}
    db_match_records = []
    for track in tracks:
        candidates = candidate_scores(track, items)[: settings.match_candidates]
        matches = ai.rerank(track, candidates)
        relevant = [match for match in matches if match.score >= 50]
        for match in matches:
            db_match_records.append(
                {
                    "track_id": match.track_id,
                    "item_id": match.item_id,
                    "score": round(match.score, 2),
                    "semantic_score": round(match.semantic_score, 6),
                    "tier": match.tier,
                    "reason": match.reason,
                }
            )
        for match in [row for row in relevant if row.score >= 80][: settings.deep_analysis_limit]:
            match.analysis = ai.analyze(track, match)
            repository.upsert_analysis(track.id, match.item_id, match.analysis, settings.analysis_model)
        matches_by_track[track.id] = matches
        trends_by_track[track.id] = ai.trend_summary(track, matches)

    repository.upsert_matches(db_match_records)
    generated_at = datetime.now(timezone.utc)
    report = build_report(generated_at, len(items), tracks, matches_by_track, trends_by_track)
    path = write_report(report)
    repository.save_digest(
        generated_at,
        report,
        {"scanned_count": len(items), "track_count": len(tracks)},
    )
    send_email(
        http,
        settings.resend_key,
        settings.digest_from,
        settings.digest_to,
        "AI Trend Tracker — {0}".format(generated_at.strftime("%Y-%m-%d")),
        report,
    )
    cleanup_result = repository.cleanup()
    storage_status = repository.storage_status()
    print("Report written to {0}".format(path))
    print("Cleanup result: {0}".format(cleanup_result))
    print("Database storage: {0}".format(storage_status))
    return str(path)
