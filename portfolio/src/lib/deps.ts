// Wires the form handler to the real Worker: secrets, the client's IP, the email binding.
import type { APIContext } from 'astro';
import { NOTIFY_FROM, SUPABASE_PUBLISHABLE_KEY, SUPABASE_URL, TURNSTILE_SECRET_KEY } from 'astro:env/server';
import { env } from 'cloudflare:workers';
import { site } from '../site/site';
import type { Deps } from './endpoint';
import { notify } from './notify';

export function workerDeps(ctx: APIContext): Deps {
  const binding = (env as unknown as { NOTIFY_EMAIL?: { send: (m: unknown) => Promise<void> } }).NOTIFY_EMAIL;
  const cf = (ctx.locals as { cfContext?: { waitUntil: (p: Promise<unknown>) => void } }).cfContext;
  return {
    supabase: SUPABASE_URL && SUPABASE_PUBLISHABLE_KEY ? { url: SUPABASE_URL, key: SUPABASE_PUBLISHABLE_KEY } : null,
    turnstileSecret: TURNSTILE_SECRET_KEY,
    ip: ctx.clientAddress,
    sendCopy: binding && NOTIFY_FROM ? (msg) => notify(binding, NOTIFY_FROM, site.email, msg) : undefined,
    waitUntil: cf ? (p) => cf.waitUntil(p) : undefined,
    log: (msg, extra) => console.log(JSON.stringify({ msg, ...extra })),
  };
}
