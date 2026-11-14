#!/usr/bin/env python3
"""Fill missing conference abstracts from official paper detail pages."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from sync_conferences import fetch_ranged


ROOT = Path(__file__).resolve().parents[1]
CONFERENCES_DIR = ROOT / "public" / "data" / "conferences"
CONFERENCE_CONFIG = ROOT / "config" / "conferences.json"
CACHE_PATH = ROOT / "data" / "conference-abstract-cache.json"


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def load_cache() -> dict[str, dict[str, Any]]:
    if not CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_cache(cache: dict[str, dict[str, Any]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def extract_abstract(page: str, url: str) -> str:
    soup = BeautifulSoup(page, "html.parser")
    host = urllib.parse.urlparse(url).netloc.casefold()
    value = ""
    if "openaccess.thecvf.com" in host:
        node = soup.select_one("#abstract")
        value = node.get_text(" ", strip=True) if node else ""
    elif "aclanthology.org" in host:
        node = soup.select_one(".acl-abstract span")
        value = node.get_text(" ", strip=True) if node else ""
    elif "ojs.aaai.org" in host:
        node = soup.select_one("section.item.abstract")
        if node:
            heading = node.select_one("h2")
            if heading:
                heading.extract()
            value = node.get_text(" ", strip=True)
    elif "ijcai.org" in host:
        node = soup.select_one(".article__abstract")
        if node:
            eyebrow = node.select_one(".eyebrow")
            if eyebrow:
                eyebrow.extract()
            value = node.get_text(" ", strip=True)
    elif "proceedings.neurips.cc" in host or "papers.nips.cc" in host:
        node = soup.select_one(".paper-abstract")
        value = node.get_text(" ", strip=True) if node else ""
    elif "iclr.cc" in host:
        node = soup.select_one(".abstract-text-inner")
        value = node.get_text(" ", strip=True) if node else ""

    if not value:
        meta = soup.find("meta", attrs={"name": "citation_abstract"})
        if meta:
            value = str(meta.get("content") or "")
    if not value:
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                payload = json.loads(script.string or "{}")
                if isinstance(payload, dict) and payload.get("abstract"):
                    value = str(payload["abstract"])
                    break
            except json.JSONDecodeError:
                continue
    value = re.sub(r"^abstract\s*[:.\-]?\s*", "", clean_text(value), flags=re.I)
    return value[:6000]


def fetch_abstract(
    url: str,
    request_config: dict[str, Any],
) -> tuple[str, str]:
    retries = 2
    last_error: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": request_config["user_agent"],
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                page = response.read(8 * 1024 * 1024).decode(
                    "utf-8", errors="replace"
                )
            abstract = extract_abstract(page, url)
            return ("found", abstract) if abstract else ("not_found", "")
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.0 + attempt)
    return "error", str(last_error)


def bulk_acl_abstracts(
    source_url: str,
    request_config: dict[str, Any],
) -> dict[str, str]:
    page = fetch_ranged(source_url, request_config)
    soup = BeautifulSoup(page, "html.parser")
    abstracts: dict[str, str] = {}
    for card in soup.select(".abstract-collapse[id^='abstract-']"):
        raw_id = str(card.get("id", ""))[len("abstract-") :]
        parts = raw_id.split("--")
        if len(parts) != 3:
            continue
        anthology_id = ".".join(parts)
        body = card.select_one(".card-body")
        value = clean_text(body.get_text(" ", strip=True) if body else "")
        if value:
            abstracts[f"https://aclanthology.org/{anthology_id}/"] = value[:6000]
    return abstracts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", help="Conference slug to enrich.")
    parser.add_argument("--limit", type=int, help="Maximum pages to request per run.")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = json.loads(CONFERENCE_CONFIG.read_text(encoding="utf-8"))
    request_config = config["request"]
    selected = set(args.only or [])
    cache = load_cache()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    requested = 0
    found = 0

    conferences = [
        item
        for item in config["conferences"]
        if not selected or item["slug"] in selected
    ]
    for conference in conferences:
        data_path = CONFERENCES_DIR / f"{conference['slug']}.json"
        if not data_path.exists():
            continue
        data = json.loads(data_path.read_text(encoding="utf-8"))
        papers = data.get("papers", [])
        missing = [
            paper
            for paper in papers
            if not paper.get("abstract")
            and (
                args.refresh
                or paper["url"] not in cache
                or cache.get(paper["url"], {}).get("status") == "error"
            )
        ]
        if not missing:
            for paper in papers:
                cached = cache.get(paper["url"], {})
                if not paper.get("abstract") and cached.get("status") == "found":
                    paper["abstract"] = cached.get("abstract", "")
            continue
        print(
            f"[abstracts] {conference['name']}: {len(missing)} pages missing",
            flush=True,
        )

        if conference["source"] == "bibtex" and "aclanthology.org" in conference["source_url"] and not args.limit:
            try:
                bulk = bulk_acl_abstracts(conference["source_url"], request_config)
                for url, abstract in bulk.items():
                    cache[url] = {
                        "status": "found",
                        "abstract": abstract,
                        "fetched_at": now,
                        "method": "bulk_event_page",
                    }
                found += sum(1 for paper in missing if paper["url"] in bulk)
                print(
                    f"[abstracts] {conference['name']}: "
                    f"{len(bulk)} abstracts from event page",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"[warn] Bulk abstract extraction failed for "
                    f"{conference['name']}: {exc}",
                    file=sys.stderr,
                )

        pending = [
            paper
            for paper in missing
            if cache.get(paper["url"], {}).get("status") != "found"
        ]
        if args.limit is not None:
            pending = pending[: max(0, args.limit - requested)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(fetch_abstract, paper["url"], request_config): paper
                for paper in pending
            }
            for future in concurrent.futures.as_completed(futures):
                paper = futures[future]
                status, value = future.result()
                cache[paper["url"]] = {
                    "status": status,
                    "abstract": value if status == "found" else "",
                    "fetched_at": now,
                    "method": "detail_page",
                }
                requested += 1
                if status == "found":
                    found += 1
                if requested % 100 == 0:
                    if not args.dry_run:
                        save_cache(cache)
                    print(
                        f"[abstracts] requested {requested}, found {found}",
                        flush=True,
                    )

        for paper in papers:
            cached = cache.get(paper["url"], {})
            if not paper.get("abstract") and cached.get("status") == "found":
                paper["abstract"] = cached.get("abstract", "")
        if not args.dry_run:
            data_path.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            save_cache(cache)

    if not args.dry_run:
        save_cache(cache)
    print(
        f"[abstracts] complete: {requested} page requests, "
        f"{found} new abstracts, {len(cache)} cache entries",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
