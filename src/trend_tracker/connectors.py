import concurrent.futures
import json
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .http import HttpClient
from .models import SourceItem
from .utils import clean_text, item_fingerprint, parse_datetime


def _xml_text(node: ET.Element, names: List[str]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def _atom_link(node: ET.Element, namespace: str = "") -> str:
    candidates = node.findall("{0}link".format(namespace))
    for link in candidates:
        if link.attrib.get("rel", "alternate") == "alternate" and link.attrib.get("href"):
            return link.attrib["href"]
    for link in candidates:
        if link.attrib.get("href"):
            return link.attrib["href"]
    return ""


def collect_rss(config: Dict[str, Any], http: HttpClient, since: datetime) -> List[SourceItem]:
    raw = http.get_text(config["url"])
    root = ET.fromstring(raw)
    max_items = int(config.get("max_items", 100))
    items: List[SourceItem] = []

    if root.tag.endswith("feed"):
        namespace = root.tag[:-4]
        entries = root.findall("{0}entry".format(namespace))
        for entry in entries[:max_items]:
            title = _xml_text(entry, ["{0}title".format(namespace)])
            url = _atom_link(entry, namespace)
            external_id = _xml_text(entry, ["{0}id".format(namespace)]) or url
            summary = _xml_text(entry, ["{0}summary".format(namespace), "{0}content".format(namespace)])
            published = _xml_text(entry, ["{0}published".format(namespace), "{0}updated".format(namespace)])
            author_node = entry.find("{0}author".format(namespace))
            author = _xml_text(author_node, ["{0}name".format(namespace)]) if author_node is not None else ""
            published_at = parse_datetime(published)
            if published_at < since:
                continue
            item = SourceItem(
                source_key=config["key"],
                external_id=external_id,
                title=clean_text(title),
                url=url,
                summary=clean_text(summary),
                author=author,
                published_at=published_at,
            )
            item.fingerprint = item_fingerprint(item.product_url, item.url, item.title)
            items.append(item)
        return items

    channel = root.find("channel") or root
    for entry in channel.findall("item")[:max_items]:
        title = _xml_text(entry, ["title"])
        url = _xml_text(entry, ["link"])
        external_id = _xml_text(entry, ["guid"]) or url
        summary = _xml_text(entry, ["description", "{http://purl.org/rss/1.0/modules/content/}encoded"])
        published = _xml_text(entry, ["pubDate", "{http://purl.org/dc/elements/1.1/}date"])
        published_at = parse_datetime(published)
        if published_at < since:
            continue
        item = SourceItem(
            source_key=config["key"],
            external_id=external_id,
            title=clean_text(title),
            url=url,
            summary=clean_text(summary),
            published_at=published_at,
        )
        item.fingerprint = item_fingerprint(item.product_url, item.url, item.title)
        items.append(item)
    return items


def collect_hackernews(config: Dict[str, Any], http: HttpClient, since: datetime, limit: int) -> List[SourceItem]:
    ids = http.get_json("https://hacker-news.firebaseio.com/v0/newstories.json")[:limit]

    def fetch(item_id: int) -> Optional[SourceItem]:
        row = http.get_json("https://hacker-news.firebaseio.com/v0/item/{0}.json".format(item_id))
        if not row or row.get("deleted") or row.get("dead") or row.get("type") != "story":
            return None
        published = datetime.fromtimestamp(int(row.get("time", 0)), tz=timezone.utc)
        if published < since:
            return None
        hn_url = "https://news.ycombinator.com/item?id={0}".format(item_id)
        product_url = row.get("url") or ""
        title = clean_text(row.get("title", ""))
        summary = clean_text(row.get("text", ""))
        item = SourceItem(
            source_key=config["key"],
            external_id=str(item_id),
            title=title,
            url=hn_url,
            product_url=product_url,
            summary=summary,
            author=row.get("by", ""),
            published_at=published,
            metadata={"score": row.get("score", 0), "comments": row.get("descendants", 0)},
        )
        item.fingerprint = item_fingerprint(item.product_url, item.url, item.title)
        return item

    items: List[SourceItem] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        for item in executor.map(fetch, ids):
            if item:
                items.append(item)
    return items


def collect_github(config: Dict[str, Any], http: HttpClient, since: datetime, token: str) -> List[SourceItem]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer {0}".format(token)
    created_after = since.strftime("%Y-%m-%d")
    items: Dict[str, SourceItem] = {}
    per_query = min(int(config.get("per_query", 40)), 100)
    for base_query in config.get("queries", []):
        query = "{0} created:>={1}".format(base_query, created_after)
        url = "https://api.github.com/search/repositories?{0}".format(
            urllib.parse.urlencode({"q": query, "sort": "stars", "order": "desc", "per_page": per_query})
        )
        payload = http.get_json(url, headers=headers)
        for row in payload.get("items", []):
            published = parse_datetime(row.get("created_at"))
            if published < since:
                continue
            external_id = str(row.get("id"))
            title = row.get("full_name") or row.get("name") or external_id
            metadata = {
                "stars": row.get("stargazers_count", 0),
                "forks": row.get("forks_count", 0),
                "language": row.get("language"),
                "topics": row.get("topics", []),
                "license": (row.get("license") or {}).get("spdx_id"),
            }
            item = SourceItem(
                source_key=config["key"],
                external_id=external_id,
                title=clean_text(title),
                url=row.get("html_url", ""),
                product_url=row.get("homepage") or row.get("html_url", ""),
                summary=clean_text(row.get("description", "")),
                author=(row.get("owner") or {}).get("login", ""),
                published_at=published,
                metadata=metadata,
            )
            item.fingerprint = item_fingerprint(item.product_url, item.url, item.title)
            items[external_id] = item
    return list(items.values())


def collect_huggingface(config: Dict[str, Any], http: HttpClient, since: datetime) -> List[SourceItem]:
    kind = config.get("kind", "models")
    limit = min(int(config.get("limit", 100)), 1000)
    url = "https://huggingface.co/api/{0}?{1}".format(
        kind,
        urllib.parse.urlencode({"sort": "lastModified", "direction": "-1", "limit": limit, "full": "true"}),
    )
    payload = http.get_json(url)
    items: List[SourceItem] = []
    singular = "model" if kind == "models" else "space"
    for row in payload:
        published = parse_datetime(row.get("lastModified") or row.get("createdAt"))
        if published < since:
            continue
        external_id = row.get("id") or row.get("modelId")
        if not external_id:
            continue
        tags = row.get("tags") or []
        pipeline_tag = row.get("pipeline_tag")
        summary_parts = ["Updated {0}".format(singular)]
        if pipeline_tag:
            summary_parts.append("Task: {0}".format(pipeline_tag))
        if tags:
            summary_parts.append("Tags: {0}".format(", ".join(str(tag) for tag in tags[:15])))
        item_url = (
            "https://huggingface.co/{0}".format(external_id)
            if kind == "models"
            else "https://huggingface.co/spaces/{0}".format(external_id)
        )
        item = SourceItem(
            source_key=config["key"],
            external_id=str(external_id),
            title=str(external_id),
            url=item_url,
            product_url=item_url,
            summary=". ".join(summary_parts),
            author=str(external_id).split("/", 1)[0],
            published_at=published,
            metadata={
                "kind": kind,
                "tags": tags[:30],
                "downloads": row.get("downloads", 0),
                "likes": row.get("likes", 0),
                "pipeline_tag": pipeline_tag,
            },
        )
        item.fingerprint = item_fingerprint(item.product_url, item.url, item.title)
        items.append(item)
    return items


def collect_arxiv(config: Dict[str, Any], http: HttpClient, since: datetime) -> List[SourceItem]:
    limit = min(int(config.get("limit", 150)), 500)
    params = {
        "search_query": config.get("query", "cat:cs.AI"),
        "start": 0,
        "max_results": limit,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    raw = http.get_text("https://export.arxiv.org/api/query?{0}".format(urllib.parse.urlencode(params)))
    root = ET.fromstring(raw)
    ns = "{http://www.w3.org/2005/Atom}"
    items: List[SourceItem] = []
    for entry in root.findall("{0}entry".format(ns)):
        published = parse_datetime(_xml_text(entry, ["{0}published".format(ns)]))
        if published < since:
            continue
        external_id = _xml_text(entry, ["{0}id".format(ns)])
        title = clean_text(_xml_text(entry, ["{0}title".format(ns)]))
        summary = clean_text(_xml_text(entry, ["{0}summary".format(ns)]))
        authors = []
        for author_node in entry.findall("{0}author".format(ns)):
            name = _xml_text(author_node, ["{0}name".format(ns)])
            if name:
                authors.append(name)
        categories = [node.attrib.get("term") for node in entry.findall("{0}category".format(ns)) if node.attrib.get("term")]
        url = _atom_link(entry, ns) or external_id
        item = SourceItem(
            source_key=config["key"],
            external_id=external_id,
            title=title,
            url=url,
            product_url=url,
            summary=summary,
            author=", ".join(authors[:5]),
            published_at=published,
            metadata={"categories": categories},
        )
        item.fingerprint = item_fingerprint(item.product_url, item.url, item.title)
        items.append(item)
    return items


def collect_source(
    config: Dict[str, Any],
    http: HttpClient,
    lookback_hours: int,
    github_token: str = "",
    hn_item_limit: int = 120,
) -> List[SourceItem]:
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    source_type = config.get("type")
    if source_type == "rss":
        return collect_rss(config, http, since)
    if source_type == "hackernews":
        return collect_hackernews(config, http, since, hn_item_limit)
    if source_type == "github":
        return collect_github(config, http, since, github_token)
    if source_type == "huggingface":
        return collect_huggingface(config, http, since)
    if source_type == "arxiv":
        return collect_arxiv(config, http, since)
    raise ValueError("Unsupported source type: {0}".format(source_type))
