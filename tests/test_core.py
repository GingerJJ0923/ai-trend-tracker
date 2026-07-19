import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from trend_tracker.ai import AIService, candidate_scores
from trend_tracker.connectors import collect_rss
from trend_tracker.config import Settings, supabase_key_role
from trend_tracker.http import HttpClient
from trend_tracker.models import MatchResult, SourceItem, Track
from trend_tracker.pipeline import deduplicate_observations
from trend_tracker.report import build_report
from trend_tracker.repository import SupabaseRepository
from trend_tracker.utils import canonical_url, clean_text, cosine_similarity, item_fingerprint, parse_datetime


class UtilityTests(unittest.TestCase):
    def test_clean_text_strips_markup(self):
        self.assertEqual(clean_text("<p>Hello&nbsp; world</p>"), "Hello world")

    def test_canonical_url_removes_tracking(self):
        value = canonical_url("HTTPS://Example.com/product/?utm_source=x&b=2#section")
        self.assertEqual(value, "https://example.com/product?b=2")

    def test_datetime_normalizes_utc(self):
        value = parse_datetime("2026-07-16T12:00:00+08:00")
        self.assertEqual(value.isoformat(), "2026-07-16T04:00:00+00:00")

    def test_cosine_similarity(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)


class PipelineTests(unittest.TestCase):
    def make_item(self, source, external_id, url, title="AI tool"):
        item = SourceItem(
            id=external_id,
            source_key=source,
            external_id=external_id,
            title=title,
            url=url,
            product_url=url,
            summary="Useful agent workflow",
            published_at=datetime.now(timezone.utc),
        )
        item.fingerprint = item_fingerprint(item.product_url, item.url, item.title)
        return item

    def test_deduplicates_cross_source_url(self):
        one = self.make_item("ph", "1", "https://example.com/tool?utm_source=ph")
        two = self.make_item("hn", "2", "https://example.com/tool")
        result = deduplicate_observations([one, two])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata["related_sources"], ["hn", "ph"])

    def test_embedding_candidate_order(self):
        track = Track(id="t", name="test", goal="agent", embedding=[1.0, 0.0])
        first = self.make_item("a", "1", "https://a.test")
        second = self.make_item("b", "2", "https://b.test")
        first.embedding = [0.9, 0.1]
        second.embedding = [0.0, 1.0]
        ranked = candidate_scores(track, [second, first])
        self.assertEqual(ranked[0][0].id, "1")

    def test_report_contains_evidence(self):
        item = self.make_item("ph", "1", "https://example.com/tool")
        track = Track(id="t", name="Agents", goal="Track useful agents")
        match = MatchResult("t", "1", 88, 0.7, "high", "Directly relevant", item, "Try it on one task.")
        report = build_report(datetime.now(timezone.utc), 10, [track], {"t": [match]}, {"t": "Agent tools are becoming more actionable."})
        self.assertIn("Directly relevant", report)
        self.assertIn("https://example.com/tool", report)
        self.assertIn("Deep analysis", report)


class FakeHttp(HttpClient):
    def __init__(self, text):
        self.text = text

    def get_text(self, url, headers=None):
        return self.text


class ConnectorTests(unittest.TestCase):
    def test_atom_feed_parsing(self):
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>item-1</id><title>New AI Agent</title>
            <link href="https://example.com/agent?utm_source=feed" rel="alternate"/>
            <summary><![CDATA[<p>A practical workflow agent.</p>]]></summary>
            <published>2026-07-16T10:00:00Z</published>
          </entry>
        </feed>"""
        config = {"key": "feed", "url": "https://example.com/feed", "max_items": 10}
        since = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
        items = collect_rss(config, FakeHttp(feed), since)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].external_id, "item-1")
        self.assertEqual(items[0].summary, "A practical workflow agent.")


class ConfigTests(unittest.TestCase):
    def test_detects_new_supabase_key_roles(self):
        self.assertEqual(supabase_key_role("sb_secret_example"), "service_role")
        self.assertEqual(supabase_key_role("sb_publishable_example"), "anon")

    def test_detects_legacy_anon_jwt(self):
        payload = base64.urlsafe_b64encode(json.dumps({"role": "anon"}).encode("utf-8")).decode("ascii").rstrip("=")
        self.assertEqual(supabase_key_role("e30.{0}.signature".format(payload)), "anon")

    def test_rejects_publishable_key_as_server_secret(self):
        old = os.environ.copy()
        try:
            os.environ["SUPABASE_URL"] = "https://project.supabase.co"
            os.environ["SUPABASE_SECRET_KEY"] = "sb_publishable_example"
            settings = Settings.from_env()
            with self.assertRaisesRegex(RuntimeError, "anon/publishable"):
                settings.require_supabase()
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_source_config_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            path.write_text(json.dumps([{"type": "rss", "key": "x", "url": "https://example.com/feed"}]), encoding="utf-8")
            old = os.environ.copy()
            try:
                os.environ["SOURCE_CONFIG"] = str(path)
                settings = Settings.from_env()
                self.assertEqual(settings.sources()[0]["key"], "x")
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_provider_neutral_configuration(self):
        old = os.environ.copy()
        try:
            for name in (
                "OPENAI_API_KEY",
                "CHAT_API_KEY",
                "CHAT_BASE_URL",
                "EMBEDDING_API_KEY",
                "EMBEDDING_BASE_URL",
                "EMBEDDING_MODEL",
                "RANKING_MODEL",
                "ANALYSIS_MODEL",
            ):
                os.environ.pop(name, None)
            os.environ.update(
                {
                    "CHAT_API_KEY": "deepseek-key",
                    "CHAT_BASE_URL": "https://api.deepseek.com/",
                    "RANKING_MODEL": "deepseek-v4-flash",
                    "EMBEDDING_API_KEY": "glm-key",
                    "EMBEDDING_BASE_URL": "https://open.bigmodel.cn/api/paas/v4/",
                    "EMBEDDING_MODEL": "embedding-3",
                    "EMBEDDING_DIMENSIONS": "512",
                }
            )
            settings = Settings.from_env()
            self.assertEqual(settings.chat_base_url, "https://api.deepseek.com")
            self.assertEqual(settings.embedding_base_url, "https://open.bigmodel.cn/api/paas/v4")
            self.assertEqual(settings.analysis_model, "deepseek-v4-flash")
            self.assertEqual(settings.embedding_dimensions, 512)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_legacy_openai_configuration_remains_supported(self):
        old = os.environ.copy()
        try:
            for name in (
                "CHAT_API_KEY",
                "CHAT_BASE_URL",
                "EMBEDDING_API_KEY",
                "EMBEDDING_BASE_URL",
                "EMBEDDING_MODEL",
                "RANKING_MODEL",
                "ANALYSIS_MODEL",
            ):
                os.environ.pop(name, None)
            os.environ["OPENAI_API_KEY"] = "legacy-key"
            settings = Settings.from_env()
            self.assertEqual(settings.chat_api_key, "legacy-key")
            self.assertEqual(settings.chat_base_url, "https://api.openai.com/v1")
            self.assertEqual(settings.embedding_model, "text-embedding-3-small")
        finally:
            os.environ.clear()
            os.environ.update(old)


class RepositoryTests(unittest.TestCase):
    def test_new_supabase_secret_key_is_not_sent_as_bearer_jwt(self):
        repository = SupabaseRepository("https://project.supabase.co", "sb_secret_example")
        self.assertEqual(repository.headers["apikey"], "sb_secret_example")
        self.assertNotIn("Authorization", repository.headers)

    def test_legacy_service_role_jwt_is_sent_as_bearer(self):
        repository = SupabaseRepository("https://project.supabase.co", "legacy.jwt.value")
        self.assertEqual(repository.headers["Authorization"], "Bearer legacy.jwt.value")


class RecordingHttp:
    def __init__(self):
        self.calls = []

    def post_json(self, url, payload, headers=None):
        self.calls.append((url, payload, headers))
        if url.endswith("/embeddings"):
            return {"data": [{"index": 0, "embedding": [1.0] + [0.0] * 511}]}
        return {"choices": [{"message": {"content": "{\"results\": []}"}}]}


class AIServiceTests(unittest.TestCase):
    def make_service(self, http=None):
        return AIService(
            chat_api_key="chat-key",
            chat_base_url="https://api.deepseek.com",
            embedding_api_key="embedding-key",
            embedding_base_url="https://open.bigmodel.cn/api/paas/v4",
            embedding_model="embedding-3",
            embedding_dimensions=512,
            ranking_model="deepseek-v4-flash",
            analysis_model="deepseek-v4-pro",
            http=http or RecordingHttp(),
        )

    def test_uses_separate_provider_endpoints(self):
        http = RecordingHttp()
        service = self.make_service(http)
        vectors = service.embeddings(["hello"])
        self.assertEqual(len(vectors[0]), 512)
        self.assertEqual(vectors[0][0], 1.0)
        self.assertEqual(http.calls[0][0], "https://open.bigmodel.cn/api/paas/v4/embeddings")

    def test_parses_fenced_json_from_compatible_provider(self):
        value = AIService._parse_json_content("```json\n{\"analysis\":\"ok\"}\n```")
        self.assertEqual(value["analysis"], "ok")


if __name__ == "__main__":
    unittest.main()
