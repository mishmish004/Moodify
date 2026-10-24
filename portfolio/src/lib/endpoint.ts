// What both form endpoints do with a submission, kept free of Worker imports so tests can run it
// with fakes: read the body, drop bots, validate, check Turnstile, save to Supabase, email a copy.
//
// The browser script posts JSON and gets JSON back. A plain HTML form post (no JavaScript yet, or
// none at all) gets a 303 to /sent or /not-sent instead.
import type { Result } from './forms.ts';
import { insertRow, type SupabaseConfig } from './supabase.ts';
import { verifyTurnstile } from './turnstile.ts';

export interface FormSpec<T> {
  table: string;
  validate: (raw: Record<string, unknown>) => Result<T, string>;
  toRow: (value: T) => Record<string, unknown>;
  /** Subject and body of the email copy */
  email: (value: T) => { subject: string; text: string; replyTo?: string };
}

export interface Deps {
  supabase: SupabaseConfig | null;
  turnstileSecret?: string;
  ip?: string;
  fetch?: typeof fetch;
  sendCopy?: (msg: { subject: string; text: string; replyTo?: string }) => Promise<void>;
  waitUntil?: (p: Promise<unknown>) => void;
  log?: (msg: string, extra?: Record<string, unknown>) => void;
}

const MAX_BODY = 20_000;

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' } });

const redirect = (to: string) => new Response(null, { status: 303, headers: { Location: to, 'Cache-Control': 'no-store' } });

async function readBody(request: Request): Promise<{ raw: Record<string, unknown>; isJson: boolean } | null> {
  const type = request.headers.get('content-type') ?? '';
  const length = Number(request.headers.get('content-length') ?? 0);
  if (length > MAX_BODY) return null;
  try {
    if (type.includes('application/json')) {
      const text = await request.text();
      if (text.length > MAX_BODY) return null;
      const raw = JSON.parse(text);
      return raw && typeof raw === 'object' && !Array.isArray(raw) ? { raw, isJson: true } : null;
    }
    if (type.includes('application/x-www-form-urlencoded') || type.includes('multipart/form-data')) {
      const form = await request.formData();
      const raw: Record<string, unknown> = {};
      form.forEach((v, k) => { if (typeof v === 'string') raw[k] = v; });
      return { raw, isJson: false };
    }
  } catch {
    return null;
  }
  return null;
}

export async function handleForm<T>(request: Request, spec: FormSpec<T>, deps: Deps): Promise<Response> {
  const log = deps.log ?? (() => {});
  const body = await readBody(request);
  const wantsJson = body?.isJson ?? (request.headers.get('accept') ?? '').includes('application/json');
  const fail = (status: number, payload: Record<string, unknown>) => (wantsJson ? json(status, { ok: false, ...payload }) : redirect('/not-sent'));
  const done = () => (wantsJson ? json(200, { ok: true }) : redirect('/sent'));

  if (!body) return fail(400, { error: 'unreadable' });
  const { raw } = body;

  // The hidden "company" field: people never see it, bots fill it. Say thanks, store nothing.
  if (typeof raw.company === 'string' && raw.company.trim() !== '') {
    log('honeypot', { table: spec.table });
    return done();
  }

  const result = spec.validate(raw);
  if (!result.ok) return fail(400, { error: 'invalid', errors: result.errors });

  if (deps.turnstileSecret) {
    const token = typeof raw['cf-turnstile-response'] === 'string' ? raw['cf-turnstile-response'] : '';
    if (!(await verifyTurnstile(deps.turnstileSecret, token, deps.ip, deps.fetch))) {
      log('turnstile rejected', { table: spec.table });
      return fail(403, { error: 'spam-check' });
    }
  }

  if (!deps.supabase) {
    log('SUPABASE_URL or SUPABASE_PUBLISHABLE_KEY is not set');
    return fail(503, { error: 'unavailable' });
  }

  let saved;
  try {
    saved = await insertRow(deps.supabase, spec.table, spec.toRow(result.value), deps.fetch);
  } catch (e) {
    log('supabase unreachable', { table: spec.table, error: String(e) });
    return fail(502, { error: 'unavailable' });
  }
  if (!saved.ok && !saved.duplicate) {
    log('supabase insert failed', { table: spec.table, status: saved.status, detail: saved.detail });
    return fail(502, { error: 'unavailable' });
  }

  // A repeat Moodify request is already on the list; the visitor sees the same thanks
  if (saved.ok && deps.sendCopy) {
    const copy = deps.sendCopy(spec.email(result.value)).catch((e) => log('email copy failed', { error: String(e) }));
    if (deps.waitUntil) deps.waitUntil(copy); else await copy;
  }
  return done();
}
