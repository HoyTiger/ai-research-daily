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
- 可选 OpenAI 兼容接口，只根据原始摘要生成中文导读。
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

## 可选 AI 导读

不配置 Key 也能完整运行；没有 Key 时显示原始摘要摘录。启用 AI 导读时，在 GitHub 仓库设置：

- Secret `LLM_API_KEY`：接口密钥。
- Variable `LLM_BASE_URL`：OpenAI 兼容 API 根地址，例如 `https://api.openai.com/v1`。
- Variable `LLM_MODEL`：支持 JSON 输出的模型名称。

系统只会把精选条目的标题、摘要和会议名称发送到配置的模型服务。每个模型的名称与可用参数可能不同，发布前应先用 `workflow_dispatch` 手动运行验证。

## 内容结构

- `config/sources.json`：来源、主题和排序配置。
- `scripts/sync_papers.py`：抓取、清洗、去重、排序和 Markdown 生成。
- `data/issues/YYYY-MM-DD.json`：结构化日报，用于来源状态和无障碍页面数据。
- `src/content/posts/YYYY-MM-DD.md`：可版本管理、可手工修订的日报原稿。
- `src/pages/`：首页、归档、关于和日报详情页。

生成器默认覆盖同一天的文件。再次运行同一天不会把当天内容当作历史内容去重。

## 版权与边界

本项目代码使用 MIT License。论文标题、摘要、作者信息和商标归原作者与出版机构所有。站点只做元数据聚合，不复制论文全文，并为每条内容提供原始来源链接。

“精选”是基于公开元数据与启发式规则的编辑排序，不构成对论文质量、结论或可复现性的认证。
