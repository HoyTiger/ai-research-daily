# AI Research Daily 设计说明

## 目标

建立一个部署在 GitHub Pages 的个人研究博客，每天自动收集 AI、大模型与多模态方向的论文、顶会论文和技术报告，生成一期可直接阅读的中文研究简报。

## 核心假设

- 博客使用静态站点，不需要常驻服务器。
- 默认同步过程不需要任何 API Key。
- 可选的 LLM 中文精炼由 OpenAI 兼容接口完成，Key 只存放在 GitHub Secrets。
- 网站只发布标题、作者、来源链接和原始摘要摘录，不复制论文全文。
- 单个来源失败时保留其他来源，并在页面中显示同步状态。

## 架构

1. `scripts/sync_papers.py` 并发拉取 arXiv、OpenReview、Crossref、Hugging Face Daily Papers 和官方技术博客 RSS。
2. 各适配器统一输出论文条目，经过字段清洗、主题分类、启发式排序和跨期去重。
3. 可选 LLM 精炼只为编辑精选生成受原文约束的中文导读。
4. 同步器生成 `data/issues/YYYY-MM-DD.json` 和 `src/content/posts/YYYY-MM-DD.md`。
5. Astro 在构建时读取 Markdown，生成首页、日报详情、归档和关于页面。
6. GitHub Actions 每日北京时间 07:17 同步、构建并部署到 GitHub Pages，同时提交当天生成内容。

## 内容源

- arXiv 官方 RSS：AI、CL、CV、LG、MA、stat.ML。
- OpenReview：ICLR、ICML、NeurIPS 的已录用论文检索。
- Crossref：Nature Machine Intelligence、TPAMI、IJCV、TMLR。
- Hugging Face Daily Papers。
- OpenAI、Google DeepMind、Microsoft Research、Meta Engineering、Google AI 官方 RSS。

来源均通过 `config/sources.json` 配置。新增来源不需要修改页面。

## 视觉方向

采用研究编辑部与实验记录册结合的风格：暖白纸张、墨绿黑正文、朱橙信号色、衬线标题与等宽数据标签。首页突出当日期号、收录数量与跨来源分布；论文卡片采用明确的来源、主题和原始摘要层级。

视觉验收覆盖 1440px、1024px、768px 和 375px，检查横向滚动、键盘焦点、文字溢出、图片替代与 reduced-motion。

## 验证

- Python 单元测试覆盖 RSS/Atom 解析、文本清洗、去重与 Markdown 转义。
- 本地执行一次真实同步，确认至少一个公开来源成功并生成一期日报。
- Astro 生产构建通过。
- 真实浏览器检查首页、日报详情、归档筛选和移动端布局。

