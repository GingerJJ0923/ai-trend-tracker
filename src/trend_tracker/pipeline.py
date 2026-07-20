import json
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Tuple
from zoneinfo import ZoneInfo

from .ai import AIService, candidate_scores
from .config import Settings
from .connectors import collect_source
from .http import HttpClient
from .models import MatchResult, SourceItem, Track
from .report import build_report, send_email, send_smtp_email, send_wechat, write_report
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
        output_language=settings.output_language,
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
            if repository:
                repository.upsert_items(items)
                repository.log_fetch(key, "success", len(items))
            successful_sources += 1
            preview.extend(items[:3])
            print("{0}: collected {1} items".format(key, len(items)))
        except Exception as exc:
            print("{0}: FAILED: {1}".format(key, exc))
            if repository:
                try:
                    repository.log_fetch(key, "failed", 0, str(exc))
                except Exception as log_exc:
                    print("{0}: could not write failure log to Supabase: {1}".format(key, log_exc))
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


def _configured_delivery_channels(settings: Settings) -> List[str]:
    channels = []
    email_ready = bool(
        settings.digest_to
        and (
            (settings.smtp_username and settings.smtp_password)
            or (settings.resend_key and settings.digest_from)
        )
    )
    if email_ready:
        channels.append("email")
    if settings.serverchan_sendkey:
        channels.append("wechat")
    return channels


def _run_with_retries(action: Callable[[], None], attempts: int = 3) -> Tuple[bool, str, int]:
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            action()
            return True, "", attempt
        except Exception as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    return False, last_error, attempts


def _deliver_report(
    settings: Settings,
    http: HttpClient,
    repository: SupabaseRepository,
    digest_id: int,
    metadata: Dict,
    subject: str,
    report: str,
) -> None:
    delivery = dict(metadata.get("delivery") or {})
    failures = []

    def record(channel: str, success: bool, error: str, attempts: int) -> None:
        delivery[channel] = {
            "status": "success" if success else "failed",
            "attempts": attempts,
            "attempted_at": datetime.now(timezone.utc).isoformat(),
            "error": error[:1000] if error else None,
        }
        metadata["delivery"] = delivery
        repository.update_digest_metadata(digest_id, metadata)
        if not success:
            failures.append("{0}: {1}".format(channel, error))

    if "email" in _configured_delivery_channels(settings) and delivery.get("email", {}).get("status") != "success":
        if settings.smtp_username and settings.smtp_password:
            email_action = lambda: send_smtp_email(
                settings.smtp_host,
                settings.smtp_port,
                settings.smtp_username,
                settings.smtp_password,
                settings.digest_to,
                subject,
                report,
            )
        else:
            email_action = lambda: send_email(
                http,
                settings.resend_key,
                settings.digest_from,
                settings.digest_to,
                subject,
                report,
            )
        record("email", *_run_with_retries(email_action))

    if "wechat" in _configured_delivery_channels(settings) and delivery.get("wechat", {}).get("status") != "success":
        record(
            "wechat",
            *_run_with_retries(lambda: send_wechat(http, settings.serverchan_sendkey, subject, report)),
        )

    if failures:
        raise RuntimeError("Digest delivery failed after retries: " + "; ".join(failures))


def digest(settings: Settings) -> str:
    http, repository, ai = make_services(settings)
    try:
        local_timezone = ZoneInfo(settings.report_timezone)
    except (KeyError, ValueError):
        local_timezone = timezone.utc
    local_now = datetime.now(local_timezone)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    subject = "AI 趋势日报｜{0}".format(local_now.strftime("%Y-%m-%d"))
    required_channels = _configured_delivery_channels(settings)

    existing_digest = None if settings.force_digest else repository.get_latest_digest_since(day_start)
    if existing_digest:
        report = str(existing_digest.get("content") or "")
        metadata = dict(existing_digest.get("metadata") or {})
        delivery = metadata.get("delivery")
        path = write_report(report)
        if not delivery:
            print("Today's legacy digest already exists; assuming it was delivered and skipping duplicates.")
            return str(path)
        if all(delivery.get(channel, {}).get("status") == "success" for channel in required_channels):
            print("Today's digest has already reached every configured channel; skipping duplicate delivery.")
            return str(path)
        _deliver_report(
            settings,
            http,
            repository,
            int(existing_digest["id"]),
            metadata,
            subject,
            report,
        )
        print("Retried only the delivery channels that had not succeeded.")
        return str(path)

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
    report = build_report(
        generated_at,
        len(items),
        tracks,
        matches_by_track,
        trends_by_track,
        top_items=settings.report_top_items,
        timezone_name=settings.report_timezone,
    )
    path = write_report(report)
    metadata = {
        "scanned_count": len(items),
        "track_count": len(tracks),
        "delivery": {channel: {"status": "pending"} for channel in required_channels},
    }
    digest_id = repository.save_digest(
        generated_at,
        report,
        metadata,
    )
    _deliver_report(settings, http, repository, digest_id, metadata, subject, report)
    cleanup_result = repository.cleanup()
    storage_status = repository.storage_status()
    print("Report written to {0}".format(path))
    print("Cleanup result: {0}".format(cleanup_result))
    print("Database storage: {0}".format(storage_status))
    return str(path)
