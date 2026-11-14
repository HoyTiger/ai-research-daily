from __future__ import annotations

import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sync_papers  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
