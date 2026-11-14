#!/usr/bin/env python3
"""Collect AI research updates and publish a daily Markdown issue."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import dataclasses
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "sources.json"
ISSUES_DIR = ROOT / "data" / "issues"
POSTS_DIR = ROOT / "src" / "content" / "posts"
TECHNICAL_RSS_TERMS = {
    "agent",
    "alignment",
    "architecture",
    "audio",
    "benchmark",
    "biology",
    "dataset",
    "diffusion",
    "evaluation",
    "generation",
    "inference",
    "language model",
    "model",
    "multimodal",
    "protein",
    "reasoning",
    "research",
    "retrieval",
    "robot",
    "safety",
    "science",
    "training",
    "video",
    "vision",
    "weather",
}


class FetchError(RuntimeError):
    """Raised when a remote source cannot be fetched or decoded."""


@dataclasses.dataclass
class Paper:
    id: str
    title: str
    summary: str
    authors: list[str]
    url: str
    pdf_url: str
    source_id: str
    source_name: str
    kind: str
    published_at: str
    venue: str
    tags: list[str]
    score: float = 0.0
    zh_title: str = ""
    zh_summary: str = ""
    ai_tldr: str = ""
    ai_why: str = ""
    external_id: str = ""

    def dedupe_keys(self) -> set[str]:
        keys = {
            "title:" + normalized_title(self.title),
            "url:" + normalized_url(self.url),
        }
        if self.external_id:
            keys.add("external:" + self.external_id.lower())
        arxiv_id = extract_arxiv_id(self.url or self.external_id)
        if arxiv_id:
            keys.add("arxiv:" + arxiv_id)
        doi = extract_doi(self.url or self.external_id)
        if doi:
            keys.add("doi:" + doi)
        return {key for key in keys if not key.endswith(":")}

    def to_dict(self, include_keys: bool = True) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["published_at"] = serialize_datetime(self.published_at)
        if include_keys:
            value["dedupe_keys"] = sorted(self.dedupe_keys())
        return value


@dataclasses.dataclass
class AdapterResult:
    items: list[Paper]
    errors: list[str]


@dataclasses.dataclass
class SourceHealth:
    source_id: str
    source_name: str
    status: str
    item_count: int
    errors: list[str]


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def normalized_title(value: str) -> str:
    value = re.sub(r"[^\w]+", "", clean_text(value).casefold(), flags=re.UNICODE)
    return value[:220]


def normalized_url(value: str) -> str:
    if not value:
        return ""
    value = urllib.parse.urldefrag(value.strip())[0].rstrip("/")
    value = re.sub(r"\.pdf$", "", value, flags=re.I)
    value = re.sub(r"v\d+$", "", value)
    return value.casefold()


def extract_arxiv_id(value: str) -> str:
    match = re.search(r"(\d{4}\.\d{4,5})(?:v\d+)?", value or "")
    return match.group(1) if match else ""


def extract_doi(value: str) -> str:
    match = re.search(r"(10\.\d{4,9}/[^\s?#]+)", value or "", flags=re.I)
    return match.group(1).rstrip(".,;)").lower() if match else ""


def parse_datetime(value: str | int | float | None) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
    raw = str(value).strip()
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m"):
        try:
            return dt.datetime.strptime(raw, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def serialize_datetime(value: str) -> str:
    parsed = parse_datetime(value)
    return parsed.isoformat() if parsed else ""


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", prefix.casefold()).strip("-")[:28]
    return f"{slug}-{digest}"


def safe_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value or "")
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    return ""


def fetch_url(url: str, request_config: dict[str, Any], accept: str) -> bytes:
    timeout = int(request_config.get("timeout_seconds", 35))
    retries = max(1, int(request_config.get("retries", 2)))
    user_agent = request_config.get(
        "user_agent", "AIResearchDaily/1.0 (+https://github.com/)"
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": accept,
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(12 * 1024 * 1024)
                if response.status >= 400:
                    raise FetchError(f"HTTP {response.status}: {url}")
                return payload
        except urllib.error.HTTPError as exc:
            last_error = exc
            if attempt + 1 < retries:
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                try:
                    delay = max(2.0, float(retry_after))
                except ValueError:
                    delay = 3.5 if exc.code == 429 else 1.2 * (attempt + 1)
                time.sleep(min(delay, 12))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise FetchError(f"{type(last_error).__name__}: {last_error} ({url})")


def fetch_json(url: str, request_config: dict[str, Any]) -> Any:
    payload = fetch_url(url, request_config, "application/json")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError(f"Invalid JSON from {url}: {exc}") from exc


def child_text(element: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in list(element):
        if local_name(child.tag) in wanted:
            text = "".join(child.itertext())
            if text.strip():
                return clean_text(text)
    return ""


def child_link(element: ET.Element) -> str:
    for child in list(element):
        if local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href", "").strip()
        if href:
            return clean_text(href)
        text = "".join(child.itertext()).strip()
        if text:
            return clean_text(text)
    return ""


def feed_authors(element: ET.Element) -> list[str]:
    authors: list[str] = []
    for child in list(element):
        name = local_name(child.tag)
        if name in {"creator", "author"}:
            nested_name = child_text(child, "name")
            text = nested_name or clean_text("".join(child.itertext()))
            if text:
                authors.extend(
                    clean_text(part)
                    for part in re.split(r"\s*(?:,| and |;)\s*", text)
                    if clean_text(part)
                )
    return list(dict.fromkeys(authors))


def parse_feed(payload: bytes, source_name: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FetchError(f"Invalid XML from {source_name}: {exc}") from exc

    entries: list[dict[str, Any]] = []
    for element in root.iter():
        if local_name(element.tag) not in {"item", "entry"}:
            continue
        title = child_text(element, "title")
        link = child_link(element)
        summary = child_text(element, "description", "summary", "content", "encoded")
        published = child_text(element, "pubdate", "published", "updated", "date")
        categories = [
            clean_text("".join(child.itertext()))
            for child in list(element)
            if local_name(child.tag) == "category" and "".join(child.itertext()).strip()
        ]
        entries.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "published": published,
                "authors": feed_authors(element),
                "categories": list(dict.fromkeys(categories)),
            }
        )
    return entries


def fetch_parsed_feed(
    url: str, request_config: dict[str, Any], source_name: str
) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for attempt in range(3):
        payload = fetch_url(
            url,
            request_config,
            "application/rss+xml, application/atom+xml, application/xml",
        )
        try:
            return parse_feed(payload, source_name)
        except FetchError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise FetchError(str(last_error))


def extract_arxiv_abstract(description: str) -> str:
    text = clean_text(description)
    marker = re.search(r"\bAbstract:\s*", text, flags=re.I)
    if marker:
        text = text[marker.end() :]
    text = re.sub(r"Announce Type:\s*\w+\s*", "", text, flags=re.I)
    text = re.sub(r"^arXiv:[^\s]+\s*", "", text, flags=re.I)
    return text.strip()


def classify_topics(text: str, topics: dict[str, list[str]]) -> list[str]:
    haystack = text.casefold()
    matches = [
        topic
        for topic, keywords in topics.items()
        if any(keyword.casefold() in haystack for keyword in keywords)
    ]
    return matches[:3] or ["综合"]


def is_fresh(
    value: str, cutoff: dt.datetime, timezone: ZoneInfo, allow_missing: bool = False
) -> bool:
    parsed = parse_datetime(value)
    if not parsed:
        return allow_missing
    return parsed.astimezone(timezone) >= cutoff


def score_item(
    item: Paper, config: dict[str, Any], now: dt.datetime, timezone: ZoneInfo
) -> float:
    ranking = config.get("ranking", {})
    topics = config.get("topics", {})
    haystack = f"{item.title} {item.summary} {item.venue}".casefold()
    topic_hits = sum(
        1
        for keywords in topics.values()
        if any(keyword.casefold() in haystack for keyword in keywords)
    )
    score = float(ranking.get("source_weights", {}).get(item.source_id, 8))
    score += topic_hits * float(ranking.get("topic_weight", 2.5))
    published = parse_datetime(item.published_at)
    if published:
        age_days = max(
            0.0, (now - published.astimezone(timezone)).total_seconds() / 86400
        )
        score += max(0.0, 8.0 - age_days * 1.5)
    if item.source_id == "openreview" and not any(
        blocked in item.venue.casefold()
        for blocked in ("submitted", "rejected", "withdrawn")
    ):
        score += float(ranking.get("accepted_venue_bonus", 18))
    for term in ranking.get("negative_terms", []):
        if term.casefold() in haystack:
            score -= 20
    return round(score, 2)


def make_paper(
    *,
    title: str,
    summary: str,
    authors: Iterable[str],
    url: str,
    pdf_url: str,
    source_id: str,
    source_name: str,
    kind: str,
    published_at: str,
    venue: str,
    tags: Iterable[str],
    external_id: str = "",
    topics: dict[str, list[str]],
) -> Paper:
    clean_title = clean_text(title)
    clean_summary = re.sub(
        r"^\s*abstract\s*[:.\-–—]?\s*",
        "",
        clean_text(summary),
        flags=re.I,
    )
    unique_authors = list(dict.fromkeys(clean_text(author) for author in authors if clean_text(author)))
    derived_tags = classify_topics(f"{clean_title} {clean_summary} {venue}", topics)
    return Paper(
        id=stable_id(source_id, f"{source_name}:{clean_title}:{url}"),
        title=clean_title,
        summary=clean_summary[:4200],
        authors=unique_authors,
        url=safe_url(clean_text(url)),
        pdf_url=safe_url(clean_text(pdf_url)),
        source_id=source_id,
        source_name=clean_text(source_name),
        kind=kind,
        published_at=serialize_datetime(published_at),
        venue=clean_text(venue),
        tags=list(dict.fromkeys([*derived_tags, *(clean_text(tag) for tag in tags if clean_text(tag))]))[
            :5
        ],
        external_id=clean_text(external_id),
    )


def collect_arxiv(
    source_config: dict[str, Any],
    request_config: dict[str, Any],
    topics: dict[str, list[str]],
    timezone: ZoneInfo,
    now: dt.datetime,
) -> AdapterResult:
    errors: list[str] = []
    papers: list[Paper] = []
    cutoff = now - dt.timedelta(days=8)

    def fetch_feed(feed: dict[str, str]) -> tuple[str, list[Paper]]:
        entries = fetch_parsed_feed(feed["url"], request_config, feed["name"])
        items: list[Paper] = []
        for entry in entries:
            if not entry["title"] or not entry["link"]:
                continue
            if not is_fresh(entry["published"], cutoff, timezone, allow_missing=True):
                continue
            arxiv_id = extract_arxiv_id(entry["link"])
            items.append(
                make_paper(
                    title=entry["title"],
                    summary=extract_arxiv_abstract(entry["summary"]),
                    authors=entry["authors"],
                    url=entry["link"],
                    pdf_url=(
                        f"https://arxiv.org/pdf/{arxiv_id}"
                        if arxiv_id
                        else entry["link"].replace("/abs/", "/pdf/")
                    ),
                    source_id="arxiv",
                    source_name=feed["name"],
                    kind="paper",
                    published_at=entry["published"],
                    venue="arXiv preprint",
                    tags=entry["categories"],
                    external_id=arxiv_id,
                    topics=topics,
                )
            )
        return feed["name"], items

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(fetch_feed, feed): feed for feed in source_config.get("feeds", [])
        }
        for future in concurrent.futures.as_completed(futures):
            feed = futures[future]
            try:
                _, feed_items = future.result()
                papers.extend(feed_items)
            except Exception as exc:  # The source boundary must isolate failures.
                errors.append(f"{feed['name']}: {exc}")

    per_feed_limit = int(source_config.get("max_per_feed", 18))
    grouped: dict[str, list[Paper]] = defaultdict(list)
    for paper in papers:
        grouped[paper.source_name].append(paper)
    selected: list[Paper] = []
    for feed in source_config.get("feeds", []):
        selected.extend(
            sorted(
                grouped.get(feed["name"], []),
                key=lambda paper: paper.published_at,
                reverse=True,
            )[:per_feed_limit]
        )
    return AdapterResult(selected, errors)


def collect_openreview(
    source_config: dict[str, Any],
    request_config: dict[str, Any],
    topics: dict[str, list[str]],
    timezone: ZoneInfo,
) -> AdapterResult:
    errors: list[str] = []
    papers_by_id: dict[str, Paper] = {}
    queries = source_config.get("queries", ["large language model"])
    limit = int(source_config.get("limit_per_query", 6))

    def fetch_query(venue: dict[str, str], term: str) -> tuple[str, list[Paper]]:
        params = urllib.parse.urlencode(
            {
                "term": term,
                "type": "terms",
                "group": venue["group"],
                "source": "forum",
                "limit": limit,
            }
        )
        payload = fetch_json(
            f"https://api2.openreview.net/notes/search?{params}", request_config
        )
        results: list[Paper] = []
        for note in payload.get("notes", []):
            content = note.get("content", {})

            def value(name: str, default: Any = "") -> Any:
                field = content.get(name, default)
                return field.get("value", default) if isinstance(field, dict) else field

            venue_text = clean_text(value("venue"))
            if not venue_text or any(
                status in venue_text.casefold()
                for status in ("submitted", "rejected", "withdrawn", "desk rejected")
            ):
                continue
            note_id = clean_text(note.get("forum") or note.get("id"))
            title = clean_text(value("title"))
            if not note_id or not title:
                continue
            keyword_value = value("keywords", [])
            keywords = keyword_value if isinstance(keyword_value, list) else [keyword_value]
            abstract = value("abstract")
            tldr = clean_text(value("TLDR"))
            summary = clean_text(f"{tldr} {abstract}")
            timestamp = note.get("pdate") or note.get("cdate") or note.get("tcdate")
            published = (
                dt.datetime.fromtimestamp(timestamp / 1000, tz=dt.timezone.utc).isoformat()
                if isinstance(timestamp, (int, float))
                else ""
            )
            authors = value("authors", [])
            if not isinstance(authors, list):
                authors = []
            results.append(
                make_paper(
                    title=title,
                    summary=summary,
                    authors=authors,
                    url=f"https://openreview.net/forum?id={urllib.parse.quote(note_id)}",
                    pdf_url=f"https://openreview.net/pdf?id={urllib.parse.quote(note_id)}",
                    source_id="openreview",
                    source_name=venue["name"],
                    kind="paper",
                    published_at=published,
                    venue=venue_text,
                    tags=keywords,
                    external_id=note_id,
                    topics=topics,
                )
            )
        return venue["name"], results

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(fetch_query, venue, term): (venue, term)
            for venue in source_config.get("venues", [])
            for term in queries
        }
        for future in concurrent.futures.as_completed(futures):
            venue, term = futures[future]
            try:
                _, results = future.result()
                for paper in results:
                    existing = papers_by_id.get(paper.external_id)
                    if not existing or paper.score > existing.score:
                        papers_by_id[paper.external_id] = paper
            except Exception as exc:
                errors.append(f"{venue['name']} / {term}: {exc}")

    papers = sorted(papers_by_id.values(), key=lambda paper: paper.published_at, reverse=True)
    return AdapterResult(papers[: int(source_config.get("max_items", 14))], errors)


def crossref_venue_matches(expected: str, actual: str) -> bool:
    def canonical(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.casefold())

    wanted = canonical(expected)
    found = canonical(actual)
    return bool(found) and (wanted in found or found in wanted)


def collect_crossref(
    source_config: dict[str, Any],
    request_config: dict[str, Any],
    topics: dict[str, list[str]],
    timezone: ZoneInfo,
    now: dt.datetime,
) -> AdapterResult:
    errors: list[str] = []
    papers: list[Paper] = []
    lookback = int(source_config.get("lookback_days", 240))
    from_date = (now - dt.timedelta(days=lookback)).date().isoformat()
    until_date = now.date().isoformat()

    def fetch_journal(journal: dict[str, str]) -> list[Paper]:
        filters = [f"from-pub-date:{from_date}", f"until-pub-date:{until_date}", "type:journal-article"]
        if journal.get("issn"):
            filters.append(f"issn:{journal['issn']}")
        params_value = {
            "query.container-title": journal["query"],
            "filter": ",".join(filters),
            "rows": int(source_config.get("rows_per_journal", 16)),
            "sort": "published",
            "order": "desc",
            "select": "DOI,title,abstract,author,published,URL,container-title,type",
        }
        crossref_mailto = os.environ.get("CROSSREF_MAILTO", "").strip()
        if crossref_mailto:
            params_value["mailto"] = crossref_mailto
        params = urllib.parse.urlencode(params_value)
        payload = fetch_json(f"https://api.crossref.org/works?{params}", request_config)
        results: list[Paper] = []
        for record in payload.get("message", {}).get("items", []):
            titles = record.get("title", [])
            title = titles[0] if isinstance(titles, list) and titles else str(titles)
            containers = record.get("container-title", [])
            container = (
                containers[0]
                if isinstance(containers, list) and containers
                else str(containers)
            )
            if not title or not crossref_venue_matches(journal["query"], container):
                continue
            authors = []
            for author in record.get("author", []):
                name = " ".join(
                    part for part in (author.get("given"), author.get("family")) if part
                )
                if name:
                    authors.append(name)
            published_parts = (
                record.get("published", {}).get("date-parts", [[None]])[0]
            )
            if published_parts and published_parts[0]:
                published = "-".join(
                    f"{part:02d}" if index else str(part)
                    for index, part in enumerate(published_parts)
                    if part is not None
                )
            else:
                published = ""
            doi = clean_text(record.get("DOI"))
            results.append(
                make_paper(
                    title=title,
                    summary=record.get("abstract", ""),
                    authors=authors,
                    url=record.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
                    pdf_url="",
                    source_id="crossref",
                    source_name=journal["name"],
                    kind="journal",
                    published_at=published,
                    venue=container,
                    tags=[journal["name"]],
                    external_id=doi,
                    topics=topics,
                )
            )
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        futures = {
            executor.submit(fetch_journal, journal): journal
            for journal in source_config.get("journals", [])
        }
        for future in concurrent.futures.as_completed(futures):
            journal = futures[future]
            try:
                papers.extend(future.result())
            except Exception as exc:
                errors.append(f"{journal['name']}: {exc}")

    papers = sorted(papers, key=lambda paper: paper.published_at, reverse=True)
    return AdapterResult(papers[: int(source_config.get("max_items", 12))], errors)


def collect_huggingface(
    source_config: dict[str, Any],
    request_config: dict[str, Any],
    topics: dict[str, list[str]],
) -> AdapterResult:
    errors: list[str] = []
    papers: list[Paper] = []
    try:
        payload = fetch_json(source_config["url"], request_config)
        for entry in payload if isinstance(payload, list) else []:
            paper = entry.get("paper", entry) if isinstance(entry, dict) else {}
            title = clean_text(paper.get("title"))
            paper_id = clean_text(
                paper.get("id") or paper.get("paperId") or entry.get("paperId")
            )
            if not title or not paper_id:
                continue
            authors = []
            for author in paper.get("authors", []):
                name = (
                    author.get("name")
                    if isinstance(author, dict)
                    else clean_text(str(author))
                )
                if name:
                    authors.append(name)
            arxiv_id = extract_arxiv_id(paper_id)
            url = (
                f"https://arxiv.org/abs/{arxiv_id}"
                if arxiv_id
                else f"https://huggingface.co/papers/{paper_id}"
            )
            papers.append(
                make_paper(
                    title=title,
                    summary=paper.get("summary", ""),
                    authors=authors,
                    url=url,
                    pdf_url=paper.get("pdfUrl", ""),
                    source_id="huggingface",
                    source_name="Hugging Face Daily",
                    kind="paper",
                    published_at=paper.get("publishedAt") or entry.get("publishedAt", ""),
                    venue="Hugging Face Daily Papers",
                    tags=["community pick"],
                    external_id=paper_id,
                    topics=topics,
                )
            )
    except Exception as exc:
        errors.append(f"Hugging Face Daily Papers: {exc}")
    return AdapterResult(papers[: int(source_config.get("max_items", 10))], errors)


def collect_rss(
    source_config: dict[str, Any],
    request_config: dict[str, Any],
    topics: dict[str, list[str]],
    timezone: ZoneInfo,
    now: dt.datetime,
) -> AdapterResult:
    errors: list[str] = []
    papers: list[Paper] = []
    cutoff = now - dt.timedelta(days=int(source_config.get("lookback_days", 9)))
    negative_terms = {
        "customer story",
        "webinar",
        "podcast",
        "sponsored",
        "hiring",
        *source_config.get("negative_terms", []),
    }
    exclude_url_parts = [
        part.casefold() for part in source_config.get("exclude_url_parts", [])
    ]

    def fetch_feed(feed: dict[str, str]) -> list[Paper]:
        entries = fetch_parsed_feed(feed["url"], request_config, feed["name"])
        results: list[Paper] = []
        for entry in entries:
            if not entry["title"] or not entry["link"]:
                continue
            if not is_fresh(entry["published"], cutoff, timezone, allow_missing=False):
                continue
            haystack = f"{entry['title']} {entry['summary']} {entry['link']}".casefold()
            if any(part in entry["link"].casefold() for part in exclude_url_parts):
                continue
            if any(term in haystack for term in negative_terms):
                continue
            if not any(term in haystack for term in TECHNICAL_RSS_TERMS):
                continue
            results.append(
                make_paper(
                    title=entry["title"],
                    summary=entry["summary"],
                    authors=entry["authors"],
                    url=entry["link"],
                    pdf_url="",
                    source_id="rss",
                    source_name=feed["name"],
                    kind="report",
                    published_at=entry["published"],
                    venue=f"{feed['name']} 官方博客",
                    tags=entry["categories"] or ["technical report"],
                    topics=topics,
                )
            )
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(fetch_feed, feed): feed
            for feed in source_config.get("feeds", [])
        }
        for future in concurrent.futures.as_completed(futures):
            feed = futures[future]
            try:
                results = future.result()
                papers.extend(results[: int(source_config.get("max_per_feed", 8))])
            except Exception as exc:
                errors.append(f"{feed['name']}: {exc}")

    papers = sorted(papers, key=lambda paper: paper.published_at, reverse=True)
    return AdapterResult(papers[: int(source_config.get("max_items", 12))], errors)


def load_past_keys(issue_date: str) -> set[str]:
    keys: set[str] = set()
    for path in ISSUES_DIR.glob("*.json"):
        if path.stem == issue_date:
            continue
        try:
            issue = json.loads(path.read_text(encoding="utf-8"))
            for item in issue.get("items", []):
                keys.update(item.get("dedupe_keys", []))
        except (OSError, json.JSONDecodeError):
            continue
    return keys


def dedupe_items(items: Iterable[Paper], blocked_keys: set[str] | None = None) -> list[Paper]:
    blocked = set(blocked_keys or set())
    seen = set(blocked)
    selected: list[Paper] = []
    for item in sorted(items, key=lambda value: (value.score, value.published_at), reverse=True):
        keys = item.dedupe_keys()
        if keys & seen:
            continue
        selected.append(item)
        seen.update(keys)
    return selected


def select_balanced(items: list[Paper], limit: int) -> list[Paper]:
    buckets: dict[str, list[Paper]] = defaultdict(list)
    for item in sorted(items, key=lambda value: value.score, reverse=True):
        buckets[item.source_id].append(item)
    source_order = sorted(
        buckets,
        key=lambda source_id: buckets[source_id][0].score,
        reverse=True,
    )
    selected: list[Paper] = []
    while len(selected) < limit:
        progressed = False
        for source_id in source_order:
            if buckets[source_id]:
                selected.append(buckets[source_id].pop(0))
                progressed = True
                if len(selected) >= limit:
                    break
        if not progressed:
            break
    return selected


def build_source_health(
    labels: dict[str, str], results: dict[str, AdapterResult]
) -> list[dict[str, Any]]:
    health: list[dict[str, Any]] = []
    for source_id in ("arxiv", "openreview", "crossref", "huggingface", "rss"):
        result = results.get(source_id)
        if not result:
            continue
        if result.items and result.errors:
            status = "partial"
        elif result.items:
            status = "ok"
        elif not result.errors:
            status = "empty"
        else:
            status = "failed"
        health.append(
            dataclasses.asdict(
                SourceHealth(
                    source_id=source_id,
                    source_name=labels.get(source_id, source_id),
                    status=status,
                    item_count=len(result.items),
                    errors=result.errors[:4],
                )
            )
        )
    return health


def enrich_with_llm(items: list[Paper]) -> int:
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key or not items:
        return 0
    base_url = (os.environ.get("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip(
        "/"
    )
    model = os.environ.get("LLM_MODEL") or "gpt-4o-mini"
    endpoint = f"{base_url}/chat/completions"
    by_id = {item.id: item for item in items}
    localized = 0
    batch_size = 7
    for offset in range(0, len(items), batch_size):
        batch = items[offset : offset + batch_size]
        payload_items = [
            {
                "id": item.id,
                "title": item.title,
                "abstract": item.summary[:1400],
                "venue": item.venue,
            }
            for item in batch
        ]
        prompt = (
            "你是严谨的中文 AI 研究编辑。把每条英文研究记录整理成中文。"
            "zh_title 是准确简洁的中文标题；zh_summary 只依据摘要概括问题、方法和主要结果；"
            "why 说明该工作值得关注的原因。不得补充摘要之外的事实、数字或评价，"
            "专有名词和模型名保留英文。只返回 JSON 对象，格式为 "
            "{\"items\":[{\"id\":\"原 id\",\"zh_title\":\"中文标题\","
            "\"zh_summary\":\"2-3句中文摘要\",\"why\":\"一句为何关注\"}]}。"
            "每条 zh_summary 不超过 150 个汉字，why 不超过 60 个汉字。\n\n"
            + json.dumps(payload_items, ensure_ascii=False)
        )
        request_body: dict[str, Any] = {
            "model": model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            body = json.dumps(request_body).encode("utf-8")
            request = urllib.request.Request(
                endpoint,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "AIResearchDaily/1.0",
                },
            )
            with urllib.request.urlopen(request, timeout=90) as response:
                result = json.loads(response.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.I)
                content = re.sub(r"\s*```$", "", content)
            parsed = json.loads(content)
            for entry in parsed.get("items", []):
                item_id = entry.get("id")
                if item_id not in by_id:
                    continue
                item = by_id[item_id]
                item.zh_title = clean_text(entry.get("zh_title"))[:240]
                item.zh_summary = clean_text(entry.get("zh_summary"))[:700]
                item.ai_why = clean_text(entry.get("why"))[:240]
                if item.zh_title and item.zh_summary:
                    localized += 1
        except Exception as exc:
            print(
                f"[warn] Chinese enrichment batch {offset // batch_size + 1} failed: {exc}",
                file=sys.stderr,
            )
    return localized


def localize_with_argos(items: list[Paper]) -> int:
    if os.environ.get("ARGOS_TRANSLATE", "").casefold() not in {"1", "true", "yes"}:
        return 0
    pending = [item for item in items if not (item.zh_title and item.zh_summary)]
    if not pending:
        return 0
    try:
        from argostranslate import translate

        languages = translate.get_installed_languages()
        english = next(language for language in languages if language.code == "en")
        chinese = next(language for language in languages if language.code == "zh")
        translator = english.get_translation(chinese)
    except Exception as exc:
        print(f"[warn] Offline Chinese translation unavailable: {exc}", file=sys.stderr)
        return 0

    glossary = {
        "本文件": "本文",
        "多式联运": "多模态",
        "多种语文模式": "多模态",
        "多模态模式": "多模态模型",
        "代理框架": "智能体框架",
        "代理人": "智能体",
        "多代理": "多智能体",
        "代理机构": "智能体",
        "大型语言模型": "大语言模型",
        "精细调整": "微调",
        "预先培训": "预训练",
        "预训": "预训练",
        "培训": "训练",
        "基因政策": "生成策略",
        "政策": "策略",
        "模具化": "模块化",
        "变形器": "Transformer",
        "空间情报": "空间智能",
        "愿景编码": "视觉编码",
        "综合基准": "合成基准",
        "辉光": "GLU",
    }

    def translate_zh(value: str, item: Paper) -> str:
        result = clean_text(translator.translate(value))
        for source, target in glossary.items():
            result = result.replace(source, target)
        if "Agent" in item.tags:
            result = result.replace("机构", "智能体").replace("代理", "智能体")
        if "多模态" in item.tags:
            result = result.replace("模式", "模态").replace("愿景", "视觉")
        return result

    localized = 0
    for item in pending:
        try:
            if not item.zh_title:
                item.zh_title = translate_zh(item.title, item)[:260]
            if not item.zh_summary:
                source_text = item.summary[:760].strip() or item.title
                item.zh_summary = translate_zh(source_text, item)[:900]
            if item.zh_title and item.zh_summary:
                localized += 1
        except Exception as exc:
            print(f"[warn] Translation failed for {item.id}: {exc}", file=sys.stderr)
    return localized


def article_label(item: Paper) -> str:
    if item.source_id == "openreview":
        return "顶会"
    if item.source_id == "crossref":
        return "顶刊"
    if item.source_id == "rss":
        return "技术报告"
    if item.source_id == "huggingface":
        return "社区精选"
    return "预印本"


def render_authors(item: Paper) -> str:
    if not item.authors:
        return "作者信息见原文"
    shown = item.authors[:6]
    suffix = " 等" if len(item.authors) > len(shown) else ""
    return html.escape("、".join(shown) + suffix)


def display_summary(item: Paper, limit: int) -> str:
    if item.zh_summary:
        return html.escape(item.zh_summary)
    if item.ai_tldr:
        return html.escape(item.ai_tldr)
    if item.summary:
        excerpt = item.summary[:limit].rstrip()
        if len(item.summary) > limit:
            excerpt += "…"
        return html.escape(excerpt)
    return "原始来源未提供摘要，请通过链接查看完整内容。"


def deep_read_issue_url(item: Paper) -> str:
    repository = os.environ.get("GITHUB_REPOSITORY", "HoyTiger/ai-research-daily")
    metadata = {
        "paper_id": item.id,
        "title": item.title,
        "source": item.source_name,
        "venue": item.venue,
        "authors": item.authors[:12],
        "url": item.url,
        "published_at": item.published_at,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(metadata, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    body = (
        f"<!-- deep-read-meta:{encoded} -->\n"
        "## 论文信息\n\n"
        f"- 原标题：{item.title}\n"
        f"- 来源：{item.source_name}\n"
        f"- 会议 / 期刊：{item.venue or '未注明'}\n"
        f"- 原文：[{item.url or '未提供'}]({item.url or ''})\n\n"
        "## 我的精读目标\n\n"
        "- 这篇论文解决什么问题？\n"
        "- 方法相比已有工作的关键差异是什么？\n"
        "- 哪些实验真正支持了结论？\n"
        "- 有哪些局限、反例或未验证假设？\n"
        "- 哪些思路可以迁移到我的工作？\n\n"
        "> 创建 Issue 后，系统会自动生成精读档案并发布到博客。"
    )
    query = urllib.parse.urlencode(
        {
            "title": f"[精读] {item.title}",
            "body": body,
            "labels": "deep-read",
        }
    )
    return f"https://github.com/{repository}/issues/new?{query}"


def published_label(item: Paper, timezone: ZoneInfo) -> str:
    parsed = parse_datetime(item.published_at)
    return parsed.astimezone(timezone).strftime("%Y.%m.%d") if parsed else "日期未标"


def render_card(item: Paper, timezone: ZoneInfo, featured: bool = False, rank: int = 0) -> str:
    class_name = "paper-card paper-card--featured" if featured else "paper-card"
    safe_article_url = safe_url(item.url)
    safe_pdf_url = safe_url(item.pdf_url)
    source_url = html.escape(safe_article_url, quote=True) if safe_article_url else ""
    display_title = item.zh_title or item.title
    title = (
        f'<a href="{source_url}" target="_blank" rel="noopener noreferrer">{html.escape(display_title)}</a>'
        if source_url
        else html.escape(display_title)
    )
    original_title = (
        f'<p class="paper-original-title">{html.escape(item.title)}</p>'
        if item.zh_title and item.zh_title != item.title
        else ""
    )
    tags = "".join(f"<span>{html.escape(tag)}</span>" for tag in item.tags[:4])
    pdf_link = (
        f'<a href="{html.escape(safe_pdf_url, quote=True)}" target="_blank" rel="noopener noreferrer">PDF ↗</a>'
        if safe_pdf_url
        else ""
    )
    source_link = (
        f'<a href="{source_url}" target="_blank" rel="noopener noreferrer">原文 ↗</a>'
        if source_url
        else ""
    )
    deep_read_link = (
        f'<a class="deep-read-action" href="{html.escape(deep_read_issue_url(item), quote=True)}" '
        'target="_blank" rel="noopener noreferrer">精读并存档 ↗</a>'
    )
    rank_markup = f'<span class="paper-rank">{rank:02d}</span>' if featured else ""
    ai_note = (
        f'<p class="paper-why"><span>为何关注</span>{html.escape(item.ai_why)}</p>'
        if item.ai_why
        else ""
    )
    abstract = (
        f'<details><summary>查看原始摘要</summary><p>{html.escape(item.summary)}</p></details>'
        if item.summary
        else ""
    )
    return f"""
<article class="{class_name}" data-kind="{html.escape(item.kind, quote=True)}" data-source="{html.escape(item.source_id, quote=True)}">
  <header class="paper-head">
    <div class="paper-source">
      {rank_markup}
      <span>{html.escape(article_label(item))}</span>
      <strong>{html.escape(item.source_name)}</strong>
    </div>
    <time datetime="{html.escape(item.published_at, quote=True)}">{published_label(item, timezone)}</time>
  </header>
  <h3>{title}</h3>
  {original_title}
  <p class="paper-authors">{render_authors(item)}</p>
  <p class="paper-summary">{display_summary(item, 680 if featured else 420)}</p>
  {ai_note}
  <div class="paper-tags">{tags}</div>
  {abstract}
  <footer class="paper-actions">
    <span>{html.escape(item.venue or "来源未注明")}</span>
    <div>{pdf_link}{source_link}{deep_read_link}</div>
  </footer>
</article>"""


def issue_description(items: list[Paper], source_count: int) -> str:
    topic_counter = Counter(tag for item in items for tag in item.tags[:1])
    topics = "、".join(topic for topic, _ in topic_counter.most_common(3)) or "AI 研究"
    return f"从 {source_count} 个数据通道收录 {len(items)} 条 AI 研究更新，聚焦{topics}。"


def build_markdown(
    issue_date: str,
    issue_number: int,
    items: list[Paper],
    picks: list[Paper],
    source_health: list[dict[str, Any]],
    timezone: ZoneInfo,
    ai_enabled: bool,
) -> str:
    healthy_sources = len([source for source in source_health if source["item_count"] > 0])
    description = issue_description(items, healthy_sources)
    topic_counts = Counter(tag for item in items for tag in item.tags[:1])
    lead_topics = " / ".join(topic for topic, _ in topic_counts.most_common(4))
    pick_ids = {item.id for item in picks}
    other_items = [item for item in items if item.id not in pick_ids]
    reports = [item for item in other_items if item.kind == "report"]
    papers = [item for item in other_items if item.kind != "report"]
    source_names = list(
        dict.fromkeys(
            item.source_name
            for item in items
            if item.source_name not in {"arXiv"}
        )
    )[:8]
    source_names = ["arXiv", *source_names] if any(item.source_id == "arxiv" for item in items) else source_names
    venue_count = len({item.venue for item in items if item.kind in {"paper", "journal"}})
    report_count = len([item for item in items if item.kind == "report"])
    frontmatter = {
        "title": f"AI Research Signal · {issue_date}",
        "description": description,
        "date": f"{issue_date}T07:17:00+08:00",
        "type": "daily",
        "issue": issue_number,
        "total": len(items),
        "sources": source_names[:10],
        "draft": False,
    }
    frontmatter_text = "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in frontmatter.items()
    )
    picks_markup = "\n".join(
        render_card(item, timezone, featured=True, rank=index)
        for index, item in enumerate(picks, start=1)
    )
    papers_markup = "\n".join(render_card(item, timezone) for item in papers)
    reports_markup = "\n".join(render_card(item, timezone) for item in reports)
    ai_notice = (
        '<p class="ai-notice">中文标题与摘要由离线机器翻译或配置的模型自动生成，仅依据原始摘要；专有名词保留英文，具体结论请以原文为准。</p>'
        if ai_enabled
        else '<p class="ai-notice">本期未启用中文生成；卡片保留原始标题、摘要与结构化元数据。</p>'
    )
    empty_notice = (
        '<p class="empty-note">今天没有发现首次收录的新条目。研究发布并非每天等量发生，稍后会继续同步。</p>'
        if not items
        else ""
    )
    return f"""---
{frontmatter_text}
---

<div class="digest-brief">
  <div class="digest-brief-main">
    <span class="brief-label">EDITOR'S BRIEF</span>
    <p>{html.escape(description)}</p>
    {ai_notice}
  </div>
  <dl class="digest-numbers">
    <div><dt>{len(items):02d}</dt><dd>本期条目</dd></div>
    <div><dt>{venue_count:02d}</dt><dd>论文 / 期刊</dd></div>
    <div><dt>{report_count:02d}</dt><dd>技术报告</dd></div>
  </dl>
</div>

{empty_notice}

<div class="topic-strip" aria-label="本期主题分布">
  <span>TOPIC SIGNAL</span>
  <p>{html.escape(lead_topics or "等待下一批研究信号")}</p>
</div>

<section class="digest-section" aria-labelledby="picks-title">
  <header class="digest-section-head">
    <span>01 / SELECTED</span>
    <div>
      <h2 id="picks-title">今日精选</h2>
      <p>综合来源可信度、主题匹配与时间新鲜度排序，并兼顾不同数据通道。</p>
    </div>
  </header>
  <div class="paper-grid paper-grid--featured">
    {picks_markup}
  </div>
</section>

<section class="digest-section" aria-labelledby="papers-title">
  <header class="digest-section-head">
    <span>02 / PAPERS &amp; JOURNALS</span>
    <div>
      <h2 id="papers-title">其余论文</h2>
      <p>尚未出现在往期简报中的预印本、顶会论文与期刊文章。</p>
    </div>
  </header>
  <div class="paper-grid">
    {papers_markup or '<p class="empty-note">本期没有更多论文条目。</p>'}
  </div>
</section>

<section class="digest-section" aria-labelledby="reports-title">
  <header class="digest-section-head">
    <span>03 / FIELD NOTES</span>
    <div>
      <h2 id="reports-title">技术报告与实验室动态</h2>
      <p>来自第一方研究机构与工程团队的模型、系统和技术报告。</p>
    </div>
  </header>
  <div class="paper-grid">
    {reports_markup or '<p class="empty-note">本期没有新的技术报告。</p>'}
  </div>
</section>

<aside class="method-note">
  <strong>Method / 口径</strong>
  <p>本期是元数据聚合，不代表论文质量认证。排序使用来源类型、主题关键词和发布日期等启发式信号；OpenReview 仅保留非拒稿、非撤稿记录。标题、摘要与结论以原始链接为准。</p>
</aside>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--date", help="Issue date in YYYY-MM-DD; defaults to today.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and summarize without writing files.")
    parser.add_argument("--no-llm", action="store_true", help="Disable optional LLM enrichment.")
    parser.add_argument("--max-items", type=int, help="Override the configured issue size.")
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    timezone = ZoneInfo(config.get("timezone", "Asia/Shanghai"))
    now = dt.datetime.now(timezone)
    issue_date = args.date or now.date().isoformat()
    try:
        issue_day = dt.date.fromisoformat(issue_date)
    except ValueError:
        print(f"Invalid --date: {issue_date}", file=sys.stderr)
        return 2
    now = dt.datetime.combine(issue_day, now.timetz()).astimezone(timezone)
    request_config = config.get("request", {})
    topics = config.get("topics", {})
    sources = config.get("sources", {})
    labels = {
        "arxiv": "arXiv categories",
        "openreview": "OpenReview venues",
        "crossref": "Crossref journals",
        "huggingface": "Hugging Face Daily",
        "rss": "Official technical blogs",
    }
    results: dict[str, AdapterResult] = {}
    collectors = {
        "arxiv": lambda: collect_arxiv(sources["arxiv"], request_config, topics, timezone, now),
        "openreview": lambda: collect_openreview(sources["openreview"], request_config, topics, timezone),
        "crossref": lambda: collect_crossref(
            sources["crossref"], request_config, topics, timezone, now
        ),
        "huggingface": lambda: collect_huggingface(
            sources["huggingface"], request_config, topics
        ),
        "rss": lambda: collect_rss(sources["rss"], request_config, topics, timezone, now),
    }
    enabled_sources = [
        source_id
        for source_id, source_config in sources.items()
        if source_config.get("enabled", True) and source_id in collectors
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(enabled_sources) or 1) as executor:
        futures = {
            executor.submit(collectors[source_id]): source_id
            for source_id in enabled_sources
        }
        for future in concurrent.futures.as_completed(futures):
            source_id = futures[future]
            try:
                results[source_id] = future.result()
            except Exception as exc:
                results[source_id] = AdapterResult([], [f"Unhandled adapter error: {exc}"])
            print(
                f"[source] {source_id}: {len(results[source_id].items)} items, "
                f"{len(results[source_id].errors)} errors",
                flush=True,
            )

    all_items = [item for result in results.values() for item in result.items]
    if not all_items:
        health = build_source_health(labels, results)
        print(json.dumps(health, ensure_ascii=False, indent=2), file=sys.stderr)
        print("No source returned usable items; refusing to publish an empty issue.", file=sys.stderr)
        return 1

    for item in all_items:
        item.score = score_item(item, config, now, timezone)
    blocked_keys = load_past_keys(issue_date)
    unique_items = dedupe_items(all_items, blocked_keys)
    issue_limit = args.max_items or int(config.get("max_items", 36))
    balanced_picks = select_balanced(
        unique_items, min(int(config.get("editor_picks", 7)), issue_limit)
    )
    pick_keys = {key for item in balanced_picks for key in item.dedupe_keys()}
    remaining = [item for item in unique_items if not (item.dedupe_keys() & pick_keys)]
    items = [*balanced_picks, *remaining][:issue_limit]
    picks = [item for item in items if item.id in {pick.id for pick in balanced_picks}]

    ai_enabled = False
    if not args.no_llm:
        localized_count = enrich_with_llm(items)
        localized_count += localize_with_argos(
            [item for item in items if not (item.zh_title and item.zh_summary)]
        )
        ai_enabled = localized_count > 0
        print(
            f"[zh] {localized_count}/{len(items)} items localized",
            flush=True,
        )

    source_health = build_source_health(labels, results)
    issue_paths = sorted(ISSUES_DIR.glob("*.json")) if ISSUES_DIR.exists() else []
    issue_number = len({path.stem for path in issue_paths} | {issue_date})
    markdown = build_markdown(
        issue_date,
        issue_number,
        items,
        picks,
        source_health,
        timezone,
        ai_enabled,
    )
    issue_data = {
        "issue_date": issue_date,
        "issue_number": issue_number,
        "generated_at": now.isoformat(),
        "description": issue_description(
            items, len([source for source in source_health if source["item_count"] > 0])
        ),
        "ai_enabled": ai_enabled,
        "source_health": source_health,
        "items": [item.to_dict() for item in items],
    }
    print(
        f"[issue] {issue_date}: {len(items)} published, {len(picks)} selected, "
        f"{len(blocked_keys)} historical keys blocked",
        flush=True,
    )
    if args.dry_run:
        print(json.dumps(issue_data, ensure_ascii=False, indent=2)[:5000])
        return 0

    ISSUES_DIR.mkdir(parents=True, exist_ok=True)
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    (ISSUES_DIR / f"{issue_date}.json").write_text(
        json.dumps(issue_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (POSTS_DIR / f"{issue_date}.md").write_text(markdown, encoding="utf-8")
    print(f"[write] {(ISSUES_DIR / f'{issue_date}.json').relative_to(ROOT)}", flush=True)
    print(f"[write] {(POSTS_DIR / f'{issue_date}.md').relative_to(ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
