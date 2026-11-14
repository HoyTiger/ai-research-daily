#!/usr/bin/env python3
"""Create a versioned deep-read dossier from a GitHub issue."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTENT_DIR = ROOT / "src" / "content" / "deep-reads"
DATA_DIR = ROOT / "data" / "deep-reads"


def read_metadata(body: str) -> dict[str, object]:
    match = re.search(r"<!--\s*deep-read-meta:([A-Za-z0-9_=-]+)\s*-->", body)
    if not match:
        return {}
    try:
        padded = match.group(1) + "=" * (-len(match.group(1)) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return {}


def yaml_scalar(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def main() -> int:
    issue_number = os.environ.get("ISSUE_NUMBER", "").strip()
    issue_title = os.environ.get("ISSUE_TITLE", "").strip()
    issue_body = os.environ.get("ISSUE_BODY", "")
    issue_url = os.environ.get("ISSUE_URL", "").strip()
    issue_created_at = os.environ.get("ISSUE_CREATED_AT", "").strip()
    if not issue_number or not issue_title:
        raise RuntimeError("ISSUE_NUMBER and ISSUE_TITLE are required.")

    title = re.sub(r"^\[精读\]\s*", "", issue_title).strip() or issue_title
    metadata = read_metadata(issue_body)
    paper_url = str(metadata.get("url") or "")
    source = str(metadata.get("source") or "GitHub Issue")
    venue = str(metadata.get("venue") or "")
    authors_value = metadata.get("authors", [])
    authors = (
        [str(author) for author in authors_value]
        if isinstance(authors_value, list)
        else []
    )
    published_at = str(metadata.get("published_at") or "")
    created_at = issue_created_at or dt.datetime.now(dt.timezone.utc).isoformat()

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    markdown_path = CONTENT_DIR / f"deep-read-{issue_number}.md"
    data_path = DATA_DIR / f"{issue_number}.json"
    if markdown_path.exists():
        print(f"Deep-read archive already exists: {markdown_path.relative_to(ROOT)}")
        return 0

    data = {
        "issue_number": int(issue_number),
        "issue_url": issue_url,
        "paper_title": title,
        "paper_url": paper_url,
        "source": source,
        "venue": venue,
        "authors": authors,
        "published_at": published_at,
        "created_at": created_at,
        "status": "queued",
    }
    data_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    frontmatter = [
        ("title", title),
        ("paperUrl", paper_url),
        ("source", source),
        ("venue", venue),
        ("authors", authors),
        ("date", created_at),
        ("status", "queued"),
        ("issueNumber", int(issue_number)),
        ("issueUrl", issue_url),
        ("draft", False),
    ]
    frontmatter_text = "\n".join(
        f"{key}: {yaml_scalar(value)}" for key, value in frontmatter
    )
    body = f"""---
{frontmatter_text}
---

<div class="deep-read-banner">
  <span>STATUS / QUEUED</span>
  <p>这是一份可持续修改的精读档案。补齐下面的章节后，把 frontmatter 中的
  <code>status</code> 改为 <code>reading</code> 或 <code>complete</code>，提交后会自动更新博客。</p>
</div>

## 论文信息

- **原始标题**：{title}
- **来源**：{source}
- **会议 / 期刊**：{venue or "未注明"}
- **作者**：{"、".join(authors[:12]) or "见原文"}
- **原文**：{f"[打开论文]({paper_url})" if paper_url else "未提供"}
- **任务 Issue**：{f"[查看讨论]({issue_url})" if issue_url else "未提供"}

## 一句话结论

待补充：读完摘要、方法和结论后，用一句话写下论文最重要的贡献。

## 它解决什么问题

待补充：问题背景、现有方法的缺口，以及作者明确提出的研究问题。

## 方法拆解

待补充：核心架构、训练目标、关键假设，以及和强基线相比真正改变了什么。

## 实验证据

待补充：数据集、评测指标、主结果、消融实验，以及数字是否真正支持结论。

## 局限与反例

待补充：作者承认的局限、实验未覆盖的场景，以及我认为需要警惕的假设。

## 可迁移到我的工作

待补充：可以复用的方法、工程实现、评测思路，以及下一步验证计划。

## 精读问题

- [ ] 核心贡献是否相对于已有工作足够新？
- [ ] 主要结果是否有充分基线、消融和统计证据？
- [ ] 训练与推理成本是否被完整报告？
- [ ] 哪些结论只在特定数据或模型规模下成立？
- [ ] 是否值得复现，最小复现实验是什么？
"""
    markdown_path.write_text(body, encoding="utf-8")
    print(f"Created {markdown_path.relative_to(ROOT)}")
    print(f"Created {data_path.relative_to(ROOT)}")
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write("created=true\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
