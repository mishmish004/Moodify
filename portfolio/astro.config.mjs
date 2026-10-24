// mish4mish.com: the portfolio. Every page is prerendered to static HTML at build time;
// only the two form endpoints under /api run on the Worker at request time.
import cloudflare from '@astrojs/cloudflare';
import sitemap from '@astrojs/sitemap';
import { defineConfig, envField, fontProviders } from 'astro/config';

export default defineConfig({
  site: 'https://mish4mish.com',
  trailingSlash: 'never',
  build: { format: 'file' },
  // Sharp resizes images at build time, so the Worker needs no Images binding
  adapter: cloudflare({ imageService: 'compile' }),
  // No logins on this site, so no session cookie and no KV namespace
  session: false,
  integrations: [
    // The secret page stays out of the sitemap; finding it is the point
    sitemap({ filter: (page) => !page.includes('/good-morning') }),
  ],
  markdown: { syntaxHighlight: false },
  // Downloaded from Google Fonts at build time and served from this site, so visitors never
  // make a request to Google. Aptos Display is used where it is installed (see global.css).
  fonts: [
    {
      provider: fontProviders.google(),
      name: 'Hanken Grotesk',
      cssVariable: '--font-hanken',
      weights: [400, 500, 600],
      styles: ['normal'],
      subsets: ['latin', 'latin-ext'],
      fallbacks: ['ui-sans-serif', 'system-ui', 'sans-serif'],
    },
    {
      provider: fontProviders.google(),
      name: 'Geist Mono',
      cssVariable: '--font-mono',
      weights: [400, 500],
      styles: ['normal'],
      subsets: ['latin'],
      fallbacks: ['ui-monospace', 'monospace'],
    },
    {
      provider: fontProviders.google(),
      name: 'Tiro Devanagari Hindi',
      cssVariable: '--font-tiro',
      weights: [400],
      styles: ['normal', 'italic'],
      subsets: ['devanagari', 'latin'],
      fallbacks: ['serif'],
    },
    {
      provider: fontProviders.google(),
      name: 'Anton',
      cssVariable: '--font-anton',
      weights: [400],
      styles: ['normal'],
      subsets: ['latin'],
      fallbacks: ['Impact', 'sans-serif'],
    },
  ],
  env: {
    schema: {
      // Server secrets: read on the Worker at request time (wrangler secret put / dashboard)
      SUPABASE_URL: envField.string({ context: 'server', access: 'secret', optional: true }),
      SUPABASE_PUBLISHABLE_KEY: envField.string({ context: 'server', access: 'secret', optional: true }),
      TURNSTILE_SECRET_KEY: envField.string({ context: 'server', access: 'secret', optional: true }),
      NOTIFY_FROM: envField.string({ context: 'server', access: 'secret', optional: true }),
      // Public, baked into the HTML at build time (dashboard: Build > Variables)
      PUBLIC_TURNSTILE_SITE_KEY: envField.string({ context: 'client', access: 'public', optional: true }),
      PUBLIC_SHOW_DRAFTS: envField.boolean({ context: 'client', access: 'public', default: false }),
    },
  },
});
