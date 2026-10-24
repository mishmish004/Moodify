import rss from '@astrojs/rss';
import type { APIRoute } from 'astro';
import { incidentPosts } from '../../lib/content';

export const GET: APIRoute = async (context) => {
  const posts = await incidentPosts();
  return rss({
    title: 'amish — the incident log',
    description: 'Write-ups of real production issues: the symptoms, the dead ends, the root cause, and the fix.',
    site: context.site!,
    items: posts.map((p) => ({
      title: p.data.title,
      description: p.data.summary,
      pubDate: p.data.date,
      link: `/yapping/${p.id}`,
      categories: [p.data.category, ...p.data.tags],
    })),
  });
};
