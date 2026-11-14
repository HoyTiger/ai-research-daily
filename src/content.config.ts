import { defineCollection, z } from "astro:content";
import { glob } from "astro/loaders";

const posts = defineCollection({
  loader: glob({ base: "./src/content/posts", pattern: "**/*.md" }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    date: z.coerce.date(),
    type: z.enum(["daily", "note"]).default("note"),
    issue: z.number().optional(),
    total: z.number().optional(),
    sources: z.array(z.string()).default([]),
    draft: z.boolean().default(false),
  }),
});

const deepReads = defineCollection({
  loader: glob({ base: "./src/content/deep-reads", pattern: "**/*.md" }),
  schema: z.object({
    title: z.string(),
    paperUrl: z.string().optional(),
    source: z.string(),
    venue: z.string().optional(),
    authors: z.array(z.string()).default([]),
    date: z.coerce.date(),
    status: z.enum(["queued", "reading", "complete"]).default("queued"),
    issueNumber: z.number(),
    issueUrl: z.string().optional(),
    draft: z.boolean().default(false),
  }),
});

export const collections = { posts, deepReads };
