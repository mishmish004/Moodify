// Write-ups, projects and songs are Markdown files under src/content/. Add a file, push, and the
// site rebuilds. Anything with `draft: true` shows in `npm run dev` but never in production.
import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { z } from 'astro/zod';
import { CATEGORIES } from './lib/categories';

const image = z.object({ src: z.string(), alt: z.string() });

const posts = defineCollection({
  loader: glob({ pattern: '**/[^_]*.md', base: './src/content/posts' }),
  schema: z.object({
    title: z.string(),
    /** The paragraph under the title: what users saw, how long, the root cause in a line */
    summary: z.string(),
    /** One line for lists; falls back to the summary */
    line: z.string().optional(),
    date: z.coerce.date(),
    category: z.enum(CATEGORIES),
    /** 'off-the-record' posts are listed only on the secret page */
    kind: z.enum(['incident', 'off-the-record']).default('incident'),
    featured: z.boolean().default(false),
    /** At a glance; leave all four out to hide the box */
    status: z.string().optional(),
    impact: z.string().optional(),
    duration: z.string().optional(),
    detectedBy: z.string().optional(),
    rootCause: z.string().optional(),
    timeline: z.array(z.object({ time: z.string(), event: z.string() })).default([]),
    tags: z.array(z.string()).default([]),
    /** Slug of the project this broke, to link the two */
    project: z.string().optional(),
    image: image.extend({ caption: z.string().optional() }).optional(),
    draft: z.boolean().default(false),
  }),
});

const projects = defineCollection({
  loader: glob({ pattern: '**/[^_]*.md', base: './src/content/projects' }),
  schema: z.object({
    title: z.string(),
    /** Position in serious-work, from 1 */
    order: z.number(),
    year: z.string().optional(),
    /** What it does, in one or two sentences */
    en: z.string(),
    /** The same in Hindi; the "hindi mai" button only shows when this is set */
    hi: z.string().optional(),
    stack: z.array(z.string()).default([]),
    repo: z.url().optional(),
    live: z.url().optional(),
    image: image.optional(),
    /** Case study details. The case study page exists only when the file has a body. */
    role: z.string().optional(),
    timeline: z.string().optional(),
    screenshot: image.extend({ caption: z.string().optional() }).optional(),
    diagram: image.extend({ caption: z.string().optional() }).optional(),
    results: z.array(z.object({ value: z.string(), label: z.string() })).default([]),
    next: z.array(z.string()).default([]),
    draft: z.boolean().default(false),
  }),
});

const songs = defineCollection({
  loader: glob({ pattern: '**/[^_]*.md', base: './src/content/songs' }),
  schema: z.object({
    title: z.string(),
    artist: z.string().default('Travis Scott'),
    album: z.string().optional(),
    year: z.string().optional(),
    /** Position in the top five, from 1 */
    rank: z.number(),
    /** Track ID from the share link: open.spotify.com/track/<ID> */
    spotify: z.string().optional(),
    /** The part that plays on the secret page, in seconds */
    start: z.number().default(0),
    end: z.number().default(30),
    firstListen: z.string().optional(),
    whyItHits: z.string().optional(),
    favouriteMoment: z.object({ at: z.string(), text: z.string() }).optional(),
    whenIPlayIt: z.string().optional(),
    verdict: z.string().optional(),
    draft: z.boolean().default(false),
  }),
});

export const collections = { posts, projects, songs };
