// The Worker entry (`main` in wrangler.jsonc). `run_worker_first` sends every request here before
// a page or file is served, so the Google sign-in gate covers the prerendered pages and static
// files as well as the form endpoints. Everything it lets through goes to Astro as before.
import { handle } from '@astrojs/cloudflare/handler';
import { gate, readConfig } from './lib/auth';

export default {
  async fetch(request, env, ctx) {
    const answer = await gate(request, readConfig(env), {
      log: (msg, extra) => console.log(JSON.stringify({ msg, ...extra })),
    });
    if (answer) return answer;
    // `npm run dev` only: requests for Vite's scripts and styles and for files in public/ arrive
    // here too, and Astro would answer them with a 404. In dev, ASSETS hands them back to Vite.
    if (import.meta.env.DEV && (request.method === 'GET' || request.method === 'HEAD')) {
      const asset = await env.ASSETS.fetch(request);
      if (asset.status !== 404) return asset;
    }
    return handle(request, env, ctx);
  },
} satisfies ExportedHandler<Env>;
