from __future__ import annotations

import sys
import unittest
import base64
import json
import os
import tempfile
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sync_papers  # noqa: E402
import archive_deep_read  # noqa: E402
import sync_conferences  # noqa: E402


class FeedParsingTests(unittest.TestCase):
    def test_parses_rss_with_dc_creator_and_cdata(self) -> None:
        payload = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss xmlns:dc="http://purl.org/dc/elements/1.1/">
          <channel><item>
            <title><![CDATA[A Useful &amp; Tested Model]]></title>
            <link>https://example.com/paper</link>
            <description><![CDATA[<p>An <b>abstract</b>.</p>]]></description>
            <dc:creator>Ada Lovelace, Alan Turing</dc:creator>
            <pubDate>Fri, 13 Nov 2026 08:00:00 GMT</pubDate>
            <category>cs.AI</category>
          </item></channel>
        </rss>"""
        entries = sync_papers.parse_feed(payload, "test")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "A Useful & Tested Model")
        self.assertEqual(entries[0]["summary"], "An abstract .")
        self.assertEqual(entries[0]["authors"], ["Ada Lovelace", "Alan Turing"])
        self.assertEqual(entries[0]["categories"], ["cs.AI"])

    def test_extracts_arxiv_abstract(self) -> None:
        description = (
            "arXiv:2611.12345v1 Announce Type: new\n"
            "Abstract: We study a reliable system.\nSecond line."
        )
        self.assertEqual(
            sync_papers.extract_arxiv_abstract(description),
            "We study a reliable system. Second line.",
        )

    def test_clean_text_removes_scripts_and_entities(self) -> None:
        value = "<script>alert(1)</script><p>Research &amp; results</p>"
        self.assertEqual(sync_papers.clean_text(value), "Research & results")


class SelectionTests(unittest.TestCase):
    def make_paper(
        self,
        title: str,
        source_id: str = "arxiv",
        url: str = "https://example.com/a",
        score: float = 10,
    ) -> sync_papers.Paper:
        return sync_papers.Paper(
            id=sync_papers.stable_id(source_id, title),
            title=title,
            summary="Summary.",
            authors=["A. Author"],
            url=url,
            pdf_url="",
            source_id=source_id,
            source_name=source_id,
            kind="paper",
            published_at="2026-11-13T00:00:00+00:00",
            venue="Test venue",
            tags=["大模型"],
            score=score,
        )

    def test_dedupes_cross_source_by_title(self) -> None:
        first = self.make_paper("A Shared Paper", "arxiv", score=10)
        second = self.make_paper("A shared paper!", "huggingface", score=20)
        result = sync_papers.dedupe_items([first, second])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source_id, "huggingface")

    def test_blocks_items_seen_in_previous_issue(self) -> None:
        paper = self.make_paper("Already Published")
        result = sync_papers.dedupe_items([paper], paper.dedupe_keys())
        self.assertEqual(result, [])

    def test_select_balanced_prefers_source_diversity(self) -> None:
        items = [
            self.make_paper("A1", "arxiv", score=30),
            self.make_paper("A2", "arxiv", score=29),
            self.make_paper("R1", "rss", score=20),
            self.make_paper("O1", "openreview", score=10),
        ]
        selected = sync_papers.select_balanced(items, 3)
        self.assertEqual(
            {item.source_id for item in selected},
            {"arxiv", "rss", "openreview"},
        )

    def test_rejects_non_http_links(self) -> None:
        self.assertEqual(sync_papers.safe_url("javascript:alert(1)"), "")
        self.assertEqual(
            sync_papers.safe_url("https://arxiv.org/abs/2611.12345"),
            "https://arxiv.org/abs/2611.12345",
        )

    def test_render_card_escapes_untrusted_content(self) -> None:
        paper = self.make_paper("<script>alert(1)</script>", url="javascript:alert(1)")
        rendered = sync_papers.render_card(
            paper, ZoneInfo("Asia/Shanghai"), featured=True, rank=1
        )
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("javascript:", rendered)

    def test_deep_read_link_targets_github_issue_form(self) -> None:
        paper = self.make_paper("A Paper Worth Reading")
        url = sync_papers.deep_read_issue_url(paper)
        self.assertTrue(url.startswith("https://github.com/HoyTiger/ai-research-daily/issues/new?"))
        self.assertIn("labels=deep-read", url)
        self.assertIn("A+Paper+Worth+Reading", url)

    def test_crossref_venue_match(self) -> None:
        self.assertTrue(
            sync_papers.crossref_venue_matches(
                "Nature Machine Intelligence", "NATURE MACHINE INTELLIGENCE"
            )
        )
        self.assertFalse(
            sync_papers.crossref_venue_matches(
                "Nature Machine Intelligence", "Machine Learning Research"
            )
        )

    def test_deep_read_issue_metadata_decoding(self) -> None:
        metadata = {"title": "Deep Paper", "authors": ["Ada"], "url": "https://example.com"}
        encoded = base64.urlsafe_b64encode(
            json.dumps(metadata, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        parsed = archive_deep_read.read_metadata(
            f"<!-- deep-read-meta:{encoded} -->\n\n## Details"
        )
        self.assertEqual(parsed, metadata)

    def test_deep_read_archive_writes_markdown_and_json(self) -> None:
        metadata = {
            "title": "Deep Paper",
            "authors": ["Ada"],
            "url": "https://example.com/paper",
            "source": "TMLR",
            "venue": "Accepted by TMLR",
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(metadata, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            content_dir = root / "src" / "content" / "deep-reads"
            data_dir = root / "data" / "deep-reads"
            environment = {
                "ISSUE_NUMBER": "42",
                "ISSUE_TITLE": "[精读] Deep Paper",
                "ISSUE_BODY": f"<!-- deep-read-meta:{encoded} -->",
                "ISSUE_URL": "https://github.com/example/repo/issues/42",
                "ISSUE_CREATED_AT": "2026-11-14T00:00:00+00:00",
            }
            with (
                mock.patch.object(archive_deep_read, "ROOT", root),
                mock.patch.object(archive_deep_read, "CONTENT_DIR", content_dir),
                mock.patch.object(archive_deep_read, "DATA_DIR", data_dir),
                mock.patch.dict(os.environ, environment, clear=False),
            ):
                self.assertEqual(archive_deep_read.main(), 0)
            markdown = (content_dir / "deep-read-42.md").read_text(encoding="utf-8")
            data = json.loads((data_dir / "42.json").read_text(encoding="utf-8"))
            self.assertIn('title: "Deep Paper"', markdown)
            self.assertIn("## 方法拆解", markdown)
            self.assertEqual(data["status"], "queued")
        self.assertEqual(data["issue_number"], 42)


class ConferenceParsingTests(unittest.TestCase):
    def test_parses_cvf_page(self) -> None:
        page = """
        <dt class="ptitle"><br><a href="/content/CVPR2026/html/Test_paper.html">A Test Paper</a></dt>
        <dd>
        <form><input type="hidden" name="query_author" value="Ada Lovelace"></form>
        <form><input type="hidden" name="query_author" value="Alan Turing"></form>
        [<a href="/content/CVPR2026/papers/Test_paper.pdf">pdf</a>]
        </dd>
        """
        papers = sync_conferences.parse_cvf(
            page,
            {
                "slug": "cvpr-2026",
                "name": "CVPR 2026",
                "year": 2026,
            },
            "Main Conference",
        )
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].title, "A Test Paper")
        self.assertEqual(papers[0].authors, ["Ada Lovelace", "Alan Turing"])
        self.assertTrue(papers[0].pdf_url.endswith("Test_paper.pdf"))

    def test_parses_bibtex_fields_with_nested_braces(self) -> None:
        bib = """
        @InProceedings{test2026,
          title = {{LLM} Reasoning: A {Test}},
          author = {Lovelace, Ada and Turing, Alan},
          url = {https://example.com/paper},
          abstract = {We test {nested} braces and a comma, then report results.}
        }
        """
        entries = list(sync_conferences.parse_bib_entries(bib))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "{LLM} Reasoning: A {Test}")
        self.assertIn("nested", entries[0]["abstract"])
        self.assertEqual(
            sync_conferences.parse_bib_authors(entries[0]["author"]),
            ["Ada Lovelace", "Alan Turing"],
        )

    def test_deduplicates_conference_papers(self) -> None:
        paper = sync_conferences.ConferencePaper(
            id="one",
            title="A Paper",
            authors=[],
            url="https://example.com/paper",
            pdf_url="",
            venue="CVPR 2026",
            track="Main",
            year=2026,
            source="CVF",
        )
        duplicate = sync_conferences.dataclasses.replace(paper, id="two")
        self.assertEqual(len(sync_conferences.dedupe_papers([paper, duplicate])), 1)

    def test_parses_ijcai_track_authors_and_pdf(self) -> None:
        page = """
        <div class="section"><div class="section_title"><h3>Main Track</h3></div>
          <div class="subsection"><div class="subsection_title">Agents</div>
          <div class="paper_wrapper">
            <a class="title" href="/proceedings/2026/1">Agent Paper</a>
            <div class="authors"><a>Ada Lovelace</a>, <a>Alan Turing</a></div>
            <div class="paper-row__actions"><a href="/proceedings/2026/0001.pdf">PDF</a></div>
          </div></div>
        </div>
        """
        papers = sync_conferences.parse_ijcai(
            page,
            {"slug": "ijcai-2026", "name": "IJCAI 2026", "year": 2026},
        )
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].track, "Main Track / Agents")
        self.assertEqual(papers[0].authors, ["Ada Lovelace", "Alan Turing"])
        self.assertTrue(papers[0].pdf_url.endswith("0001.pdf"))

    def test_parses_neurips_list_and_derives_pdf(self) -> None:
        page = """
        <ul class="paper-list">
          <li class="conference">
            <a title="paper title" href="/paper_files/paper/2025/hash/abc-Abstract-Conference.html">Paper</a>
            <span class="paper-authors">Ada Lovelace, Alan Turing</span>
            <span class="paper-track-badge">Main Conference Track</span>
          </li>
        </ul>
        """
        papers = sync_conferences.parse_neurips(
            page,
            {"slug": "neurips-2025", "name": "NeurIPS 2025", "year": 2025},
        )
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].authors, ["Ada Lovelace", "Alan Turing"])
        self.assertTrue(papers[0].pdf_url.endswith("abc-Paper-Conference.pdf"))

    def test_parses_huggingface_preprints(self) -> None:
        payload = json.dumps(
            [
                {
                    "paper": {
                        "id": "2611.12345",
                        "title": "A Strong Preprint",
                        "summary": "Summary.",
                        "authors": [{"name": "Ada Lovelace"}],
                        "upvotes": 42,
                    }
                }
            ]
        )
        papers = sync_conferences.parse_huggingface(
            payload,
            {
                "slug": "preprints-2026",
                "name": "高质量预印本 2026",
                "year": 2026,
                "min_upvotes": 5,
                "max_items": 100,
            },
        )
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].url, "https://arxiv.org/abs/2611.12345")
        self.assertEqual(papers[0].track, "Hugging Face Daily")
        self.assertEqual(papers[0].signal, "42 upvotes")

    def test_classifies_and_summarizes_paper_topics(self) -> None:
        paper = sync_conferences.ConferencePaper(
            id="topic-test",
            title="A Multimodal Agent for Visual Reasoning",
            authors=[],
            url="https://example.com/topic",
            pdf_url="",
            venue="Test",
            track="Main",
            year=2026,
            source="Test",
            abstract="We evaluate a multimodal retrieval and reasoning system.",
        )
        topics = sync_conferences.classify_topics(
            paper,
            {
                "多模态": ["multimodal", "vision-language"],
                "Agent": ["agent"],
                "推理": ["reasoning"],
                "检索与知识": ["retrieval"],
            },
        )
        self.assertEqual(topics[:3], ["多模态", "Agent", "推理"])
        paper.topics = topics
        distribution = sync_conferences.topic_distribution([paper])
        self.assertEqual(distribution[0]["count"], 1)
        self.assertNotIn("其他", [item["name"] for item in distribution])


if __name__ == "__main__":
    unittest.main()
