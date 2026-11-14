# AI Research Signal

一个自动同步 AI、大模型、多模态方向论文与技术报告的静态博客。每天生成一期研究简报，并部署到 GitHub Pages。

## 已实现

- arXiv `cs.AI`、`cs.CL`、`cs.CV`、`cs.LG`、`cs.MA`、`stat.ML` 官方 RSS。
- OpenReview 的 ICLR、ICML、NeurIPS 已录用论文检索。
- Crossref 的 Nature Machine Intelligence、TPAMI、IJCV、TMLR 期刊更新。
- Hugging Face Daily Papers。
- OpenAI、Google DeepMind、Microsoft Research、Meta Engineering、Google AI 官方 RSS。
- 跨来源、跨期去重，来源均衡精选，主题分类和启发式排序。
- 每日 JSON 数据、Markdown 原稿、静态 HTML、归档检索和来源健康状态。
- RSS 订阅、响应式页面与 GitHub Pages 项目子路径支持。
- 论文卡片自带“精读并存档”按钮，通过 GitHub Issue 建立可长期迭代的中文精读档案。
- 独立顶会论文全量库，支持标题、作者和 Track 检索，覆盖 CVPR、ICLR、ICML、AAAI、IJCAI、ACL、EMNLP、NeurIPS，并收录高质量预印本。
- 对全量论文做多标签主题归类，汇总大模型、多模态、Agent、推理、训练系统、RAG、评测安全、具身智能等方向。
- 对官方目录未给出摘要的论文，按论文详情页回填摘要并持久化缓存，后续只请求缺失项。
- 默认使用离线 Argos 模型生成中文标题与摘要；配置自己的 OpenAI 兼容接口后可获得更好的中文研究导读。
- GitHub Actions 每日北京时间 07:17 自动同步、构建、提交并部署。

## 本地运行

需要 Node.js 22、pnpm 11 和 Python 3.12+。

```bash
pnpm install
pnpm run sync
pnpm run dev
```

打开 `http://localhost:4321`。生产构建：

```bash
pnpm test
pnpm run build
pnpm run preview
```

只验证抓取而暂不写文件：

```bash
python3 scripts/sync_papers.py --dry-run --no-llm
```

指定日期重新生成：

```bash
python3 scripts/sync_papers.py --date 2026-11-14 --no-llm
```

同步全部会议论文集和高质量预印本：

```bash
python -m pip install -r requirements-conferences.txt
pnpm run sync:conferences
```

## 发布到 GitHub Pages

1. 新建 GitHub 仓库，把本目录作为仓库根目录推送。
2. 打开仓库的 `Settings > Pages`，将 Source 设为 `GitHub Actions`。
3. 确保 Actions 默认具有 `Read and write permissions`。
4. 在 `Actions` 中手动运行一次 `Sync and deploy research digest`。

首次推送可以执行：

```bash
cd ai-research-daily
git init -b main
git add .
git commit -m "feat: launch AI research daily"
git remote add origin https://github.com/<username>/<repository>.git
git push -u origin main
```

注意：必须把 `ai-research-daily/` 本身作为 GitHub 仓库根目录；如果把它作为当前工作区大仓库的子目录推送，`.github/workflows` 不会按预期运行。

工作流会自动处理项目页路径，因此仓库既可以命名为 `username.github.io`，也可以命名为任意项目名。部署后地址通常是：

```text
https://<username>.github.io/<repository>/
```

如果使用自定义域名，在 `astro.config.mjs` 或工作流中修改 `SITE_URL` 即可。

## 个性化

修改 [src/site.ts](src/site.ts) 中的作者名与 GitHub 地址：

```ts
export const site = {
  name: "Research Signal",
  chineseName: "AI 研究信号",
  author: "你的名字",
  githubUrl: "https://github.com/your-name/your-repo",
};
```

数据源、主题关键词、排序权重和每期条目数在 [config/sources.json](config/sources.json) 中维护。新增 RSS 只需追加到 `sources.rss.feeds`；新增 OpenReview 会议只需增加一个 `group`。

Crossref 的匿名请求可能被限流。建议在仓库 Variables 中设置可选 `CROSSREF_MAILTO` 为你的联系邮箱，Crossref 会将它用于礼貌访问标识；该值不会传入浏览器端。

## 中文报告与 AI 导读

GitHub Actions 默认安装并缓存开源的 Argos 英中翻译模型，把当期标题和摘要整理成中文，不需要额外配置 Key。离线翻译完全在 Action Runner 内执行，页面同时保留英文原始标题和原文链接。

首次运行需要下载约 70MB 的英中模型，后续会从 Actions 缓存恢复。机器翻译可能出现术语偏差，因此页面保留原文；配置自己的大模型接口可以显著改善中文导读质量。

如果你希望使用自己的模型服务，在 GitHub 仓库设置：

- Secret `LLM_API_KEY`：接口密钥。
- Variable `LLM_BASE_URL`：OpenAI 兼容 API 根地址，例如 `https://api.openai.com/v1`。
- Variable `LLM_MODEL`：支持 JSON 输出的模型名称。

系统只会把当期条目的标题、摘要和会议名称发送到配置的模型服务。每个模型的名称与可用参数可能不同，发布前应先用 `workflow_dispatch` 手动运行验证。未配置模型时，线上任务使用离线翻译兜底。

## 内容结构

- `config/sources.json`：来源、主题和排序配置。
- `scripts/sync_papers.py`：抓取、清洗、去重、排序和 Markdown 生成。
- `data/issues/YYYY-MM-DD.json`：结构化日报，用于来源状态和无障碍页面数据。
- `src/content/posts/YYYY-MM-DD.md`：可版本管理、可手工修订的日报原稿。
- `src/pages/`：首页、归档、关于和日报详情页。
- `src/content/deep-reads/`：待读、精读中和已完成的论文精读档案。
- `data/deep-reads/`：精读任务的结构化元数据。
- `public/data/conferences/`：会议库按会议拆分的全量论文 JSON。
- `config/conferences.json`：会议来源、分卷和预印本筛选规则。
- `config/topics.json`：主题关键词和分类规则。
- `public/data/conferences/topics.json`：跨会议主题汇总与各会议主题分布。
- `data/conference-abstract-cache.json`：按论文 URL 缓存的官方摘要和提取状态。

会议库每周自动更新一次。完整的 CVPR、ACL、EMNLP、ICML、AAAI 等 proceedings
可能包含数千篇论文，因此不会塞进每日精选；每日精选仍由 `sync_papers.py` 生成。
会议库会为论文赋予最多三个主题标签；官方元数据不足、无法可靠匹配的论文不会强行归入“其他”，而是单独统计为未归类。

摘要回填可单独运行：

```bash
python scripts/enrich_conference_abstracts.py --only cvpr-2026 --workers 6
```

首次运行会访问缺失摘要的官方论文页；ACL 和 EMNLP 会优先从官方会议总页批量提取。提取结果会缓存，因此周更通常只需要处理新论文。

生成器默认覆盖同一天的文件。再次运行同一天不会把当天内容当作历史内容去重。

## 精读论文

在任意日报卡片底部点击“精读并存档”，GitHub 会打开预填论文信息的 Issue 表单。提交 Issue 后：

1. `deep-read` 工作流读取论文元数据。
2. 自动生成 `src/content/deep-reads/deep-read-<issue>.md`。
3. 发布到 `/deep-reads/` 精读档案，并在 Issue 中回复档案地址。

生成的精读档案包含问题定义、方法、实验证据、局限、迁移价值和检查清单。直接在 Markdown 中填写内容，并把 `status` 从 `queued` 改为 `reading` 或 `complete`，推送后即可更新状态和公开页面。

## 版权与边界

本项目代码使用 MIT License。论文标题、摘要、作者信息和商标归原作者与出版机构所有。站点只做元数据聚合，不复制论文全文，并为每条内容提供原始来源链接。

“精选”是基于公开元数据与启发式规则的编辑排序，不构成对论文质量、结论或可复现性的认证。
