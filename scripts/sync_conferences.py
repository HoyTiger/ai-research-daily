#!/usr/bin/env python3
"""Build full conference paper indexes from official proceedings."""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import html
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "conferences.json"
TOPICS_CONFIG = ROOT / "config" / "topics.json"
OUTPUT_DIR = ROOT / "public" / "data" / "conferences"


class FetchError(RuntimeError):
    pass


@dataclasses.dataclass
class ConferencePaper:
    id: str
    title: str
    authors: list[str]
    url: str
    pdf_url: str
    venue: str
    track: str
    year: int
    source: str
    doi: str = ""
    signal: str = ""
    abstract: str = ""
    topics: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def paper_from_dict(value: dict[str, Any]) -> ConferencePaper:
    return ConferencePaper(
        id=value["id"],
        title=value["title"],
        authors=list(value.get("authors", [])),
        url=value["url"],
        pdf_url=value.get("pdf_url", ""),
        venue=value["venue"],
        track=value["track"],
        year=int(value["year"]),
        source=value["source"],
        doi=value.get("doi", ""),
        signal=value.get("signal", ""),
        abstract=value.get("abstract", ""),
        topics=list(value.get("topics", [])),
    )


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def clean_markup(value: str) -> str:
    return clean_text(re.sub(r"<[^>]+>", " ", html.unescape(value or "")))


def clean_latex(value: str | None) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"\\(?:textit|textbf|emph|mathrm|mathbf|mathit)\s*", "", text)
    text = text.replace("\\&", "&").replace("\\%", "%").replace("\\_", "_")
    text = text.replace("--", "–").replace("~", " ")
    text = re.sub(r"[{}]", "", text)
    return clean_text(text)


def normalized_title(value: str) -> str:
    return re.sub(r"[^\w]+", "", value.casefold(), flags=re.UNICODE)


def classify_topics(
    paper: ConferencePaper, topic_keywords: dict[str, list[str]]
) -> list[str]:
    title = paper.title.casefold()
    body = f"{paper.title} {paper.abstract}".casefold()
    scores: list[tuple[int, str]] = []
    for topic, keywords in topic_keywords.items():
        score = 0
        for keyword in keywords:
            needle = keyword.casefold()
            if needle in title:
                score += 3
            elif needle in body:
                score += 1
        if score:
            scores.append((score, topic))
    return [
        topic
        for _, topic in sorted(scores, key=lambda item: -item[0])[:3]
    ]


def tag_papers(
    papers: list[ConferencePaper], topic_keywords: dict[str, list[str]]
) -> None:
    for paper in papers:
        paper.topics = classify_topics(paper, topic_keywords)


def stable_id(slug: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def request_bytes(
    url: str,
    request_config: dict[str, Any],
    *,
    range_start: int | None = None,
    range_end: int | None = None,
    timeout: int | None = None,
) -> tuple[bytes, int | None]:
    retries = max(1, int(request_config.get("retries", 4)))
    user_agent = request_config.get("user_agent", "AIResearchDaily/1.0")
    last_error: Exception | None = None
    for attempt in range(retries):
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html, application/xhtml+xml, text/x-bibtex, text/plain, */*",
            "Accept-Encoding": "identity",
        }
        if range_start is not None:
            end = range_end if range_end is not None else range_start
            headers["Range"] = f"bytes={range_start}-{end}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout or int(request_config.get("timeout_seconds", 120)),
            ) as response:
                data = response.read()
                total: int | None = None
                content_range = response.headers.get("Content-Range", "")
                match = re.search(r"/(\d+)$", content_range)
                if match:
                    total = int(match.group(1))
                return data, total
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 416 and range_start is not None:
                return b"", None
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            try:
                delay = max(2.0, float(retry_after))
            except ValueError:
                delay = 2.0 * (attempt + 1)
            if attempt + 1 < retries:
                time.sleep(min(delay, 12))
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.IncompleteRead,
            OSError,
            ValueError,
        ) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2.0 * (attempt + 1))
    raise FetchError(f"{type(last_error).__name__}: {last_error} ({url})")


def fetch_complete(url: str, request_config: dict[str, Any]) -> str:
    payload, _ = request_bytes(url, request_config)
    return payload.decode("utf-8", errors="replace")


def fetch_ranged(url: str, request_config: dict[str, Any]) -> str:
    chunk_size = int(request_config.get("range_chunk_bytes", 262144))
    first, total = request_bytes(
        url, request_config, range_start=0, range_end=chunk_size - 1
    )
    data = bytearray(first)
    if not total:
        return first.decode("utf-8", errors="replace")
    while len(data) < total:
        start = len(data)
        end = min(start + chunk_size - 1, total - 1)
        piece, _ = request_bytes(
            url, request_config, range_start=start, range_end=end
        )
        if not piece:
            break
        data.extend(piece)
    if len(data) != total:
        raise FetchError(f"Incomplete ranged download: {len(data)}/{total} bytes ({url})")
    return bytes(data).decode("utf-8", errors="replace")


def parse_cvf(
    page: str, conference: dict[str, Any], track: str
) -> list[ConferencePaper]:
    blocks = re.split(r'(?=<dt class="ptitle">)', page)
    papers: list[ConferencePaper] = []
    for block in blocks:
        title_match = re.search(
            r'<dt class="ptitle"><br><a href="([^"]+)">(.*?)</a></dt>',
            block,
            flags=re.I | re.S,
        )
        if not title_match:
            continue
        path = html.unescape(title_match.group(1))
        title = clean_markup(title_match.group(2))
        if not title:
            continue
        authors = list(
            dict.fromkeys(
                clean_markup(author)
                for author in re.findall(
                    r'name="query_author" value="([^"]+)"', block, flags=re.I
                )
                if clean_markup(author)
            )
        )
        pdf_match = re.search(r'href="([^"]+_paper\.pdf)"', block, flags=re.I)
        paper_url = urllib.parse.urljoin("https://openaccess.thecvf.com", path)
        pdf_url = (
            urllib.parse.urljoin("https://openaccess.thecvf.com", pdf_match.group(1))
            if pdf_match
            else ""
        )
        papers.append(
            ConferencePaper(
                id=stable_id(conference["slug"], paper_url or title),
                title=title,
                authors=authors,
                url=paper_url,
                pdf_url=pdf_url,
                venue=conference["name"],
                track=track,
                year=int(conference["year"]),
                source="CVF Open Access",
            )
        )
    return papers


def parse_bib_entries(text: str) -> Iterable[dict[str, str]]:
    position = 0
    while True:
        start_match = re.search(r"@\w+\s*\{", text[position:])
        if not start_match:
            break
        open_index = position + start_match.end() - 1
        depth = 0
        cursor = open_index
        while cursor < len(text):
            char = text[cursor]
            if char == "{" and (cursor == 0 or text[cursor - 1] != "\\"):
                depth += 1
            elif char == "}" and (cursor == 0 or text[cursor - 1] != "\\"):
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        if depth != 0:
            break
        entry = text[open_index + 1 : cursor]
        key, _, field_text = entry.partition(",")
        fields = parse_bib_fields(field_text)
        fields["_key"] = clean_text(key)
        yield fields
        position = cursor + 1


def parse_bib_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    index = 0
    while index < len(text):
        while index < len(text) and (text[index].isspace() or text[index] == ","):
            index += 1
        name_match = re.match(r"([A-Za-z][A-Za-z0-9_-]*)\s*=", text[index:])
        if not name_match:
            next_comma = text.find(",", index)
            if next_comma == -1:
                break
            index = next_comma + 1
            continue
        name = name_match.group(1).casefold()
        index += name_match.end()
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        if text[index] == "{":
            depth = 0
            start = index + 1
            while index < len(text):
                if text[index] == "{" and (index == 0 or text[index - 1] != "\\"):
                    depth += 1
                elif text[index] == "}" and (index == 0 or text[index - 1] != "\\"):
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            fields[name] = text[start:index]
            index += 1
        elif text[index] == '"':
            start = index + 1
            index += 1
            while index < len(text):
                if text[index] == '"' and text[index - 1] != "\\":
                    break
                index += 1
            fields[name] = text[start:index]
            index += 1
        else:
            next_comma = text.find(",", index)
            if next_comma == -1:
                next_comma = len(text)
            fields[name] = text[index:next_comma].strip()
            index = next_comma + 1
    return fields


def parse_bib_authors(value: str) -> list[str]:
    authors: list[str] = []
    for raw_author in re.split(r"\s+and\s+", clean_latex(value)):
        raw_author = clean_text(raw_author)
        if not raw_author or raw_author.casefold() == "others":
            continue
        if "," in raw_author:
            family, given = [part.strip() for part in raw_author.split(",", 1)]
            authors.append(clean_text(f"{given} {family}"))
        else:
            authors.append(raw_author)
    return list(dict.fromkeys(author for author in authors if author))


def parse_bibtex(
    value: str, conference: dict[str, Any], default_track: str
) -> list[ConferencePaper]:
    papers: list[ConferencePaper] = []
    for fields in parse_bib_entries(value):
        if "title" not in fields or not clean_text(fields.get("author")):
            continue
        title = clean_latex(fields.get("title"))
        if not title:
            continue
        url = clean_text(fields.get("url") or fields.get("pdf"))
        if not url:
            continue
        pdf_url = clean_text(fields.get("pdf"))
        if not pdf_url and "aclanthology.org" in url:
            pdf_url = url.rstrip("/") + ".pdf"
        venue_name = clean_latex(fields.get("booktitle")) or conference["name"]
        track = default_track or ("Findings" if "findings" in url.casefold() else "Main Conference")
        papers.append(
            ConferencePaper(
                id=stable_id(conference["slug"], url),
                title=title,
                authors=parse_bib_authors(fields.get("author", "")),
                url=url,
                pdf_url=pdf_url,
                venue=conference["name"],
                track=track or venue_name,
                year=int(conference["year"]),
                source=(
                    "PMLR"
                    if "proceedings.mlr.press" in url or "raw.githubusercontent.com/mlresearch" in pdf_url
                    else "ACL Anthology"
                ),
                doi=clean_text(fields.get("doi")),
                abstract=clean_latex(fields.get("abstract"))[:1800],
            )
        )
    return papers


def parse_iclr_virtual(
    page: str, conference: dict[str, Any]
) -> list[ConferencePaper]:
    soup = BeautifulSoup(page, "html.parser")
    papers: list[ConferencePaper] = []
    seen_urls: set[str] = set()
    for link in soup.select('a[href*="/virtual/2026/poster/"]'):
        title = clean_markup(link.get_text(" ", strip=True))
        href = link.get("href")
        if not title or not href:
            continue
        url = urllib.parse.urljoin("https://iclr.cc", href)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        papers.append(
            ConferencePaper(
                id=stable_id(conference["slug"], url),
                title=title,
                authors=[],
                url=url,
                pdf_url="",
                venue=conference["name"],
                track=conference.get("default_track", "Accepted Papers"),
                year=int(conference["year"]),
                source="ICLR Virtual",
            )
        )
    return papers


def parse_ijcai(
    page: str, conference: dict[str, Any]
) -> list[ConferencePaper]:
    soup = BeautifulSoup(page, "html.parser")
    papers: list[ConferencePaper] = []
    for block in soup.select(".paper_wrapper"):
        title_link = block.select_one("a.title")
        if not title_link:
            continue
        title = clean_markup(title_link.get_text(" ", strip=True))
        href = title_link.get("href")
        if not title or not href:
            continue
        section = block.find_previous(class_="section_title")
        subsection = block.find_previous(class_="subsection_title")
        track_parts = [
            clean_markup(element.get_text(" ", strip=True))
            for element in (section, subsection)
            if element
        ]
        track = " / ".join(dict.fromkeys(track_parts)) or "Main Track"
        authors = [
            clean_markup(link.get_text(" ", strip=True))
            for link in block.select(".authors a")
            if clean_markup(link.get_text(" ", strip=True))
        ]
        pdf_link = next(
            (
                link.get("href")
                for link in block.select(".paper-row__actions a")
                if str(link.get("href", "")).endswith(".pdf")
            ),
            "",
        )
        papers.append(
            ConferencePaper(
                id=stable_id(
                    conference["slug"],
                    urllib.parse.urljoin("https://www.ijcai.org", href),
                ),
                title=title,
                authors=list(dict.fromkeys(authors)),
                url=urllib.parse.urljoin("https://www.ijcai.org", href),
                pdf_url=(
                    urllib.parse.urljoin("https://www.ijcai.org", str(pdf_link))
                    if pdf_link
                    else ""
                ),
                venue=conference["name"],
                track=track,
                year=int(conference["year"]),
                source="IJCAI Proceedings",
            )
        )
    return papers


def parse_neurips(
    page: str, conference: dict[str, Any]
) -> list[ConferencePaper]:
    soup = BeautifulSoup(page, "html.parser")
    papers: list[ConferencePaper] = []
    for item in soup.select("ul.paper-list > li"):
        title_link = item.select_one('a[title="paper title"]')
        if not title_link:
            continue
        title = clean_markup(title_link.get_text(" ", strip=True))
        href = title_link.get("href")
        if not title or not href:
            continue
        url = urllib.parse.urljoin("https://proceedings.neurips.cc", href)
        hash_match = re.search(r"/hash/([^/]+)", href)
        pdf_url = ""
        if hash_match:
            paper_name = hash_match.group(1).replace("-Abstract-", "-Paper-")
            paper_name = re.sub(r"\.html$", ".pdf", paper_name)
            pdf_url = f"https://proceedings.neurips.cc/paper_files/paper/{conference['year']}/file/{paper_name}"
        authors_text = item.select_one(".paper-authors")
        authors = (
            [
                clean_markup(author)
                for author in re.split(r",\s*", authors_text.get_text(" ", strip=True))
                if clean_markup(author)
            ]
            if authors_text
            else []
        )
        badge = item.select_one(".paper-track-badge")
        papers.append(
            ConferencePaper(
                id=stable_id(conference["slug"], url),
                title=title,
                authors=authors,
                url=url,
                pdf_url=pdf_url,
                venue=conference["name"],
                track=(
                    clean_markup(badge.get_text(" ", strip=True))
                    if badge
                    else "Main Conference"
                ),
                year=int(conference["year"]),
                source="NeurIPS Proceedings",
            )
        )
    return papers


def parse_aaai_issue_links(
    archive_page: str, conference: dict[str, Any]
) -> list[dict[str, str]]:
    soup = BeautifulSoup(archive_page, "html.parser")
    issues: list[dict[str, str]] = []
    for summary in soup.select(".obj_issue_summary"):
        series = summary.select_one(".series")
        title_link = summary.select_one("h2 a.title")
        if not series or not title_link:
            continue
        if not clean_markup(series.get_text(" ", strip=True)).startswith(
            conference.get("volume_prefix", "")
        ):
            continue
        href = title_link.get("href")
        if href:
            issues.append(
                {
                    "name": clean_markup(title_link.get_text(" ", strip=True)),
                    "url": str(href),
                }
            )
    return issues


def parse_aaai_issue(
    page: str,
    conference: dict[str, Any],
    issue: dict[str, str],
) -> list[ConferencePaper]:
    soup = BeautifulSoup(page, "html.parser")
    papers: list[ConferencePaper] = []
    for summary in soup.select(".obj_article_summary"):
        title_link = summary.select_one("h3.title a")
        if not title_link:
            continue
        title = clean_markup(title_link.get_text(" ", strip=True))
        href = title_link.get("href")
        if not title or not href:
            continue
        authors_box = summary.select_one(".authors")
        authors = (
            [
                clean_markup(author)
                for author in re.split(
                    r",\s*", authors_box.get_text(" ", strip=True)
                )
                if clean_markup(author)
            ]
            if authors_box
            else []
        )
        pdf_link = summary.select_one("a.obj_galley_link.pdf")
        papers.append(
            ConferencePaper(
                id=stable_id(conference["slug"], str(href)),
                title=title,
                authors=authors,
                url=str(href),
                pdf_url=str(pdf_link.get("href")) if pdf_link else "",
                venue=conference["name"],
                track=issue["name"],
                year=int(conference["year"]),
                source="AAAI OJS",
            )
        )
    return papers


def parse_huggingface(
    payload: str, conference: dict[str, Any]
) -> list[ConferencePaper]:
    entries = json.loads(payload)
    if not isinstance(entries, list):
        return []
    papers: list[ConferencePaper] = []
    minimum = int(conference.get("min_upvotes", 5))
    for entry in entries:
        paper_data = entry.get("paper", entry) if isinstance(entry, dict) else {}
        title = clean_text(paper_data.get("title"))
        paper_id = clean_text(paper_data.get("id"))
        upvotes = int(paper_data.get("upvotes") or 0)
        if not title or not paper_id or upvotes < minimum:
            continue
        arxiv_match = re.search(r"(\d{4}\.\d{4,5})", paper_id)
        arxiv_id = arxiv_match.group(1) if arxiv_match else paper_id
        url = (
            f"https://arxiv.org/abs/{arxiv_id}"
            if arxiv_match
            else f"https://huggingface.co/papers/{paper_id}"
        )
        authors = []
        for author in paper_data.get("authors", []):
            name = (
                author.get("name")
                if isinstance(author, dict)
                else clean_text(str(author))
            )
            if name:
                authors.append(clean_text(name))
        papers.append(
            ConferencePaper(
                id=stable_id(conference["slug"], paper_id),
                title=title,
                authors=list(dict.fromkeys(authors)),
                url=url,
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_match else "",
                venue=conference["name"],
                track="Hugging Face Daily",
                year=int(conference["year"]),
                source="Hugging Face Daily",
                signal=f"{upvotes} upvotes",
                abstract=clean_text(
                    paper_data.get("ai_summary") or paper_data.get("summary")
                )[:1800],
            )
        )
    return sorted(
        papers,
        key=lambda paper: int(paper.signal.split(" ", 1)[0] or 0),
        reverse=True,
    )[: int(conference.get("max_items", 100))]


def dedupe_papers(papers: Iterable[ConferencePaper]) -> list[ConferencePaper]:
    seen: set[str] = set()
    result: list[ConferencePaper] = []
    for paper in papers:
        keys = {
            paper.url.rstrip("/").casefold(),
            normalized_title(paper.title),
        }
        if keys & seen:
            continue
        seen.update(keys)
        result.append(paper)
    return result


def topic_distribution(papers: list[ConferencePaper]) -> list[dict[str, Any]]:
    counts = Counter(topic for paper in papers for topic in paper.topics)
    total = max(1, len(papers))
    return [
        {
            "name": topic,
            "count": count,
            "share": round(count / total * 100, 1),
        }
        for topic, count in counts.most_common()
    ]


def conference_summary(
    conference: dict[str, Any],
    papers: list[ConferencePaper],
    topic_counts: list[dict[str, Any]],
) -> str:
    track_count = len({paper.track for paper in papers})
    leading = "、".join(
        f"{item['name']}（{item['count']}篇，{item['share']}%）"
        for item in topic_counts[:3]
    )
    classified = sum(1 for paper in papers if paper.topics)
    unclassified = len(papers) - classified
    return (
        f"{conference['name']} 共收录 {len(papers)} 篇论文，覆盖 {track_count} 个 Track。"
        f"主题信号最集中的方向为{leading}。同一论文可以属于多个主题，"
        f"因此主题计数不用于计算论文总量；本期有 {unclassified} 篇未能通过关键词明确归类。"
    )


def collect_conference(
    conference: dict[str, Any], request_config: dict[str, Any]
) -> tuple[list[ConferencePaper], list[str]]:
    errors: list[str] = []
    papers: list[ConferencePaper] = []
    if conference["source"] == "cvf":
        def fetch_day(day: str) -> list[ConferencePaper]:
            url = f"{conference['source_url']}?day={urllib.parse.quote(day)}"
            return parse_cvf(
                fetch_complete(url, request_config),
                conference,
                f"Main Conference / {day}",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(fetch_day, day): day for day in conference["days"]
            }
            for future in concurrent.futures.as_completed(futures):
                day = futures[future]
                try:
                    papers.extend(future.result())
                except Exception as exc:
                    errors.append(f"{day}: {exc}")
    elif conference["source"] == "bibtex":
        volumes = conference.get("volumes") or [
            {
                "name": conference.get("default_track", "Main Conference"),
                "url": conference["bib_url"],
            }
        ]

        def fetch_volume(volume: dict[str, str]) -> list[ConferencePaper]:
            return parse_bibtex(
                fetch_ranged(volume["url"], request_config),
                conference,
                volume.get("name", "Main Conference"),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(volumes))) as executor:
            futures = {
                executor.submit(fetch_volume, volume): volume for volume in volumes
            }
            for future in concurrent.futures.as_completed(futures):
                volume = futures[future]
                try:
                    papers.extend(future.result())
                except Exception as exc:
                    errors.append(f"{volume.get('name', volume['url'])}: {exc}")
    elif conference["source"] == "iclr_virtual":
        try:
            papers = parse_iclr_virtual(
                fetch_complete(conference["source_url"], request_config), conference
            )
        except Exception as exc:
            errors.append(str(exc))
    elif conference["source"] == "ijcai":
        try:
            papers = parse_ijcai(
                fetch_complete(conference["source_url"], request_config), conference
            )
        except Exception as exc:
            errors.append(str(exc))
    elif conference["source"] == "neurips":
        try:
            papers = parse_neurips(
                fetch_complete(conference["source_url"], request_config), conference
            )
        except Exception as exc:
            errors.append(str(exc))
    elif conference["source"] == "aaai_ojs":
        try:
            archive_page = fetch_complete(conference["source_url"], request_config)
            issues = parse_aaai_issue_links(archive_page, conference)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = {
                    executor.submit(
                        lambda issue=issue: parse_aaai_issue(
                            fetch_complete(issue["url"], request_config),
                            conference,
                            issue,
                        )
                    ): issue
                    for issue in issues
                }
                for future in concurrent.futures.as_completed(futures):
                    issue = futures[future]
                    try:
                        papers.extend(future.result())
                    except Exception as exc:
                        errors.append(f"{issue['name']}: {exc}")
        except Exception as exc:
            errors.append(str(exc))
    elif conference["source"] == "huggingface":
        try:
            papers = parse_huggingface(
                fetch_complete(conference["api_url"], request_config), conference
            )
        except Exception as exc:
            errors.append(str(exc))
    else:
        raise ValueError(f"Unknown conference source: {conference['source']}")
    return dedupe_papers(papers), errors


def build_index_entry(
    conference: dict[str, Any],
    papers: list[ConferencePaper],
    errors: list[str],
    generated_at: str,
) -> dict[str, Any]:
    track_counts = Counter(paper.track for paper in papers)
    topics = topic_distribution(papers)
    return {
        "slug": conference["slug"],
        "name": conference["name"],
        "full_name": conference["full_name"],
        "year": conference["year"],
        "kind": conference.get("kind", "conference"),
        "source_url": conference["source_url"],
        "source": {
            "cvf": "CVF Open Access",
            "bibtex": (
                "PMLR" if conference["slug"] == "icml-2026" else "ACL Anthology"
            ),
            "iclr_virtual": "ICLR Virtual",
            "aaai_ojs": "AAAI OJS",
            "ijcai": "IJCAI Proceedings",
            "neurips": "NeurIPS Proceedings",
            "huggingface": "Hugging Face Daily",
        }.get(conference["source"], conference["source"]),
        "paper_count": len(papers),
        "tracks": [
            {"name": name, "count": count}
            for name, count in track_counts.most_common()
        ],
        "topics": topics,
        "summary": conference_summary(conference, papers, topics),
        "generated_at": generated_at,
        "errors": errors,
        "status": "partial" if errors else "complete",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--only", help="Only sync one conference slug.")
    parser.add_argument("--limit", type=int, help="Limit papers per conference for testing.")
    parser.add_argument(
        "--from-existing",
        action="store_true",
        help="Re-tag and summarize existing JSON without downloading proceedings.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    topic_keywords = json.loads(TOPICS_CONFIG.read_text(encoding="utf-8"))
    request_config = config.get("request", {})
    conferences = [
        conference
        for conference in config["conferences"]
        if not args.only or conference["slug"] == args.only
    ]
    if not conferences:
        print(f"Unknown conference slug: {args.only}", file=sys.stderr)
        return 2

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    index_path = OUTPUT_DIR / "index.json"
    existing_index: dict[str, Any] = {"conferences": []}
    if index_path.exists():
        try:
            existing_index = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    index_by_slug = {
        item["slug"]: item for item in existing_index.get("conferences", [])
    }
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    failures = 0

    for conference in conferences:
        if args.from_existing:
            data_path = OUTPUT_DIR / f"{conference['slug']}.json"
            if not data_path.exists():
                print(
                    f"[conference] {conference['name']}: missing {data_path.name}",
                    file=sys.stderr,
                )
                failures += 1
                continue
            old_data = json.loads(data_path.read_text(encoding="utf-8"))
            papers = [
                paper_from_dict(value) for value in old_data.get("papers", [])
            ]
            errors = list(old_data.get("errors", []))
            print(
                f"[conference] {conference['name']}: re-summarizing existing data",
                flush=True,
            )
        else:
            print(f"[conference] {conference['name']}: fetching", flush=True)
            try:
                papers, errors = collect_conference(conference, request_config)
            except Exception as exc:
                papers, errors = [], [str(exc)]
        tag_papers(papers, topic_keywords)
        if args.limit:
            papers = papers[: args.limit]
        if not papers:
            failures += 1
            print(
                f"[conference] {conference['name']}: FAILED {errors[:2]}",
                file=sys.stderr,
                flush=True,
            )
            continue
        entry = build_index_entry(conference, papers, errors, generated_at)
        index_by_slug[conference["slug"]] = entry
        output = {
            **entry,
            "papers": [paper.to_dict() for paper in papers],
        }
        print(
            f"[conference] {conference['name']}: {len(papers)} papers, "
            f"{len(errors)} errors",
            flush=True,
        )
        if not args.dry_run:
            (OUTPUT_DIR / f"{conference['slug']}.json").write_text(
                json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

    if failures == len(conferences):
        print("No conference data was collected.", file=sys.stderr)
        return 1
    ordered = sorted(
        index_by_slug.values(),
        key=lambda item: (item.get("year", 0), item.get("name", "")),
        reverse=True,
    )
    index_data = {
        "generated_at": generated_at,
        "total_papers": sum(item.get("paper_count", 0) for item in ordered),
        "conferences": ordered,
    }
    global_topic_counts: Counter[str] = Counter()
    topic_by_conference: dict[str, list[dict[str, Any]]] = {}
    for conference in ordered:
        for topic in conference.get("topics", []):
            global_topic_counts[topic["name"]] += topic["count"]
            topic_by_conference.setdefault(topic["name"], []).append(
                {
                    "slug": conference["slug"],
                    "name": conference["name"],
                    "count": topic["count"],
                }
            )
    total_papers = max(1, index_data["total_papers"])
    global_topics = [
        {
            "name": topic,
            "count": count,
            "share": round(count / total_papers * 100, 1),
            "conferences": sorted(
                topic_by_conference.get(topic, []),
                key=lambda item: item["count"],
                reverse=True,
            ),
        }
        for topic, count in global_topic_counts.most_common()
    ]
    index_data["topics"] = global_topics
    # Count papers with at least one topic by examining each data file.
    classified_total = 0
    for conference in ordered:
        data_path = OUTPUT_DIR / f"{conference['slug']}.json"
        if not data_path.exists():
            continue
        conference_data = json.loads(data_path.read_text(encoding="utf-8"))
        classified_total += sum(
            1 for paper in conference_data.get("papers", []) if paper.get("topics")
        )
    index_data["classified_papers"] = classified_total
    index_data["unclassified_papers"] = max(
        0, index_data["total_papers"] - classified_total
    )
    leading_topics = "、".join(
        f"{topic['name']}（{topic['count']}篇）" for topic in global_topics[:5]
    )
    index_data["summary"] = (
        f"全部论文库共 {index_data['total_papers']} 篇，"
        f"其中 {classified_total} 篇获得至少一个主题标签，"
        f"{index_data['unclassified_papers']} 篇暂未明确归类。"
        f"主题信号最强的方向为{leading_topics}。"
        "主题支持多标签，因此各主题计数之和大于论文总数。"
    )
    if args.dry_run:
        print(json.dumps(index_data, ensure_ascii=False, indent=2))
    else:
        index_path.write_text(
            json.dumps(index_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (OUTPUT_DIR / "topics.json").write_text(
            json.dumps(
                {
                    "generated_at": generated_at,
                    "total_papers": index_data["total_papers"],
                    "summary": index_data["summary"],
                    "topics": global_topics,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[write] {(OUTPUT_DIR / 'topics.json').relative_to(ROOT)}", flush=True)
        print(f"[write] {index_path.relative_to(ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
