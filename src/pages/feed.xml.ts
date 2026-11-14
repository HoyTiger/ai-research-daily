import type { APIRoute } from "astro";
import { getCollection } from "astro:content";
import { withBase } from "../lib/url";

function escapeXml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");
}

export const GET: APIRoute = async ({ site }) => {
  const posts = (await getCollection("posts", ({ data }) => !data.draft)).sort(
    (a, b) => b.data.date.valueOf() - a.data.date.valueOf(),
  );
  const home = new URL(withBase(), site);
  const items = posts
    .map((post) => {
      const link = new URL(withBase(`issues/${post.id}/`), site);
      return `<item>
  <title>${escapeXml(post.data.title)}</title>
  <link>${escapeXml(link.toString())}</link>
  <guid isPermaLink="true">${escapeXml(link.toString())}</guid>
  <pubDate>${post.data.date.toUTCString()}</pubDate>
  <description>${escapeXml(post.data.description)}</description>
</item>`;
    })
    .join("\n");

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>AI Research Signal</title>
  <link>${escapeXml(home.toString())}</link>
  <description>每日 AI、大模型与多模态论文、顶会、顶刊和技术报告。</description>
  <language>zh-CN</language>
  <lastBuildDate>${(posts[0]?.data.date ?? new Date()).toUTCString()}</lastBuildDate>
  ${items}
</channel>
</rss>`;

  return new Response(xml, {
    headers: {
      "Content-Type": "application/rss+xml; charset=utf-8",
      "Cache-Control": "public, max-age=1800",
    },
  });
};
