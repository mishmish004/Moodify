// Google sign-in for the whole site. src/worker.ts runs `gate` on every request before Astro or
// the static files see it, so no page, file or form endpoint is reachable without signing in.
// Any Google account gets in. Kept free of Worker imports so tests can run it with fakes.
//
// OpenID Connect's authorization code flow, with PKCE:
//   /auth/google    sends the visitor to Google; state, nonce and the PKCE verifier wait in a
//                   short-lived signed cookie
//   /auth/callback  swaps the code for an ID token, checks it, sets the session cookie
//   /auth/sign-out  clears the session cookie
// The session is a cookie signed with AUTH_SECRET (HMAC-SHA256). Nothing is stored server-side.

export interface AuthConfig { clientId: string; clientSecret: string; secret: string }

export interface AuthDeps {
  fetch?: typeof fetch;
  now?: () => number;
  log?: (msg: string, extra?: Record<string, unknown>) => void;
}

export interface Session { sub: string; email?: string; exp: number }

interface Flow { state: string; nonce: string; verifier: string; next: string; exp: number }

export const SESSION_COOKIE = 'mish_session';
export const FLOW_COOKIE = 'mish_oauth';
export const SESSION_DAYS = 30;
const SESSION_MS = SESSION_DAYS * 24 * 60 * 60 * 1000;
// Long enough to pick an account and type a password
const FLOW_MS = 10 * 60 * 1000;
const MIN_SECRET = 32;

const GOOGLE_AUTHORIZE = 'https://accounts.google.com/o/oauth2/v2/auth';
const GOOGLE_TOKEN = 'https://oauth2.googleapis.com/token';
const GOOGLE_ISSUERS = ['https://accounts.google.com', 'accounts.google.com'];

/** All three Worker secrets, or null. Without them nobody can sign in, so nobody gets in. */
export function readConfig(env: object): AuthConfig | null {
  const { GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, AUTH_SECRET } = env as Record<string, unknown>;
  if (typeof GOOGLE_CLIENT_ID !== 'string' || !GOOGLE_CLIENT_ID) return null;
  if (typeof GOOGLE_CLIENT_SECRET !== 'string' || !GOOGLE_CLIENT_SECRET) return null;
  if (typeof AUTH_SECRET !== 'string' || AUTH_SECRET.length < MIN_SECRET) return null;
  return { clientId: GOOGLE_CLIENT_ID, clientSecret: GOOGLE_CLIENT_SECRET, secret: AUTH_SECRET };
}

/** What the sign-in page needs before anyone has signed in: itself, its styles and fonts, the icon */
export function isPublic(pathname: string): boolean {
  return pathname === '/sign-in' || pathname === '/favicon.svg' || /^\/_astro\/(fonts\/[\w-]+\.woff2|[\w.-]+\.css)$/.test(pathname);
}

/**
 * The gate. Returns the response for sign-in routes and for anyone not signed in; returns null
 * when the request may go on to the site.
 */
export async function gate(request: Request, config: AuthConfig | null, deps: AuthDeps = {}): Promise<Response | null> {
  const url = new URL(request.url);
  if (url.pathname === '/auth/google') return start(url, config, deps);
  if (url.pathname === '/auth/callback') return finish(request, url, config, deps);
  if (url.pathname === '/auth/sign-out') return redirect('/sign-in?signed-out', [clearCookie(SESSION_COOKIE, '/', url)]);

  const now = deps.now?.() ?? Date.now();
  const session = config ? await readSession(request, config.secret, now) : null;
  if (url.pathname === '/sign-in') return session ? redirect(safeNext(url.searchParams.get('next'), url)) : null;
  if (session || isPublic(url.pathname)) return null;

  // Only a page is worth coming back to; a form post returns to the home page
  const next = request.method === 'GET' || request.method === 'HEAD' ? url.pathname + url.search : '/';
  return redirect(next === '/' ? '/sign-in' : `/sign-in?next=${encodeURIComponent(next)}`);
}

async function readSession(request: Request, secret: string, now: number): Promise<Session | null> {
  const session = await unseal<Session>(readCookie(request, SESSION_COOKIE), secret, 'session');
  return session && typeof session.sub === 'string' && typeof session.exp === 'number' && session.exp > now ? session : null;
}

/**
 * Where to go after signing in: a path on this site, never another origin (`//evil.com`,
 * `/\evil.com`) and never back into the sign-in routes.
 */
export function safeNext(next: string | null, base: URL): string {
  if (!next || !next.startsWith('/')) return '/';
  let target: URL;
  try {
    target = new URL(next, base.origin);
  } catch {
    return '/';
  }
  if (target.origin !== base.origin || target.pathname === '/sign-in' || target.pathname.startsWith('/auth/')) return '/';
  return target.pathname + target.search + target.hash;
}

async function start(url: URL, config: AuthConfig | null, deps: AuthDeps): Promise<Response> {
  if (!config) {
    deps.log?.('GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET or AUTH_SECRET is not set (AUTH_SECRET needs 32+ characters)');
    return redirect('/sign-in?error=unavailable');
  }
  const now = deps.now?.() ?? Date.now();
  const flow: Flow = { state: random(), nonce: random(), verifier: random(), next: safeNext(url.searchParams.get('next'), url), exp: now + FLOW_MS };
  const google = new URL(GOOGLE_AUTHORIZE);
  google.search = new URLSearchParams({
    client_id: config.clientId,
    redirect_uri: callbackUrl(url),
    response_type: 'code',
    scope: 'openid email',
    state: flow.state,
    nonce: flow.nonce,
    code_challenge: await sha256(flow.verifier),
    code_challenge_method: 'S256',
    // Always show the account chooser, so signing out and in again can switch accounts
    prompt: 'select_account',
  }).toString();
  return redirect(google.href, [setCookie(FLOW_COOKIE, await seal(flow, config.secret, 'flow'), FLOW_MS, '/auth', url)]);
}

async function finish(request: Request, url: URL, config: AuthConfig | null, deps: AuthDeps): Promise<Response> {
  const now = deps.now?.() ?? Date.now();
  const flow = config ? await unseal<Flow>(readCookie(request, FLOW_COOKIE), config.secret, 'flow') : null;
  const clearFlow = clearCookie(FLOW_COOKIE, '/auth', url);
  // The flow cookie works once, whatever happens
  const fail = (error: string) =>
    redirect(`/sign-in?error=${error}${flow && flow.next !== '/' ? `&next=${encodeURIComponent(flow.next)}` : ''}`, [clearFlow]);

  if (!config) return fail('unavailable');
  // access_denied when the visitor backs out of Google's screen
  if (url.searchParams.has('error')) return fail('cancelled');
  const code = url.searchParams.get('code');
  if (!flow || flow.exp <= now || !code || url.searchParams.get('state') !== flow.state) return fail('expired');

  const account = await exchange(code, flow, callbackUrl(url), config, deps, now);
  if (!account) return fail('failed');
  const session: Session = { sub: account.sub, email: account.email, exp: now + SESSION_MS };
  return redirect(flow.next, [clearFlow, setCookie(SESSION_COOKIE, await seal(session, config.secret, 'session'), SESSION_MS, '/', url)]);
}

/** Trades the code for an ID token and returns the account it names, or null with a log line */
async function exchange(code: string, flow: Flow, redirectUri: string, config: AuthConfig, deps: AuthDeps, now: number) {
  const log = deps.log ?? (() => {});
  let body: { id_token?: unknown };
  try {
    const res = await (deps.fetch ?? fetch)(GOOGLE_TOKEN, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        code,
        client_id: config.clientId,
        client_secret: config.clientSecret,
        redirect_uri: redirectUri,
        grant_type: 'authorization_code',
        code_verifier: flow.verifier,
      }),
      signal: AbortSignal.timeout(8000),
    });
    if (!res.ok) {
      log('google token exchange failed', { status: res.status, detail: (await res.text().catch(() => '')).slice(0, 300) });
      return null;
    }
    body = await res.json();
  } catch (e) {
    log('google unreachable', { error: String(e) });
    return null;
  }

  // The token came straight from Google over TLS, in answer to a request made with the client
  // secret, so its signature needs no separate check (OpenID Connect Core 3.1.3.7, item 6).
  // Its claims still do.
  const claims = typeof body.id_token === 'string' ? jwtClaims(body.id_token) : null;
  const valid = claims
    && typeof claims.sub === 'string' && claims.sub !== ''
    && GOOGLE_ISSUERS.includes(claims.iss as string)
    && claims.aud === config.clientId
    && typeof claims.exp === 'number' && claims.exp * 1000 > now
    && claims.nonce === flow.nonce;
  if (!valid) {
    log('google id token rejected');
    return null;
  }
  return { sub: claims.sub as string, email: typeof claims.email === 'string' ? claims.email : undefined };
}

function jwtClaims(token: string): Record<string, unknown> | null {
  try {
    const claims = JSON.parse(decoder.decode(fromBase64url(token.split('.')[1] ?? '')));
    return claims && typeof claims === 'object' ? claims : null;
  } catch {
    return null;
  }
}

// ---- redirects and cookies ----

const callbackUrl = (url: URL) => `${url.origin}/auth/callback`;

function redirect(to: string, cookies: string[] = []): Response {
  const headers = new Headers({ Location: to, 'Cache-Control': 'no-store' });
  for (const c of cookies) headers.append('Set-Cookie', c);
  return new Response(null, { status: 303, headers });
}

function setCookie(name: string, value: string, maxAgeMs: number, path: string, url: URL): string {
  // Secure everywhere but plain-http localhost in `npm run dev`
  return `${name}=${value}; Path=${path}; Max-Age=${Math.floor(maxAgeMs / 1000)}; HttpOnly; SameSite=Lax${url.protocol === 'https:' ? '; Secure' : ''}`;
}

const clearCookie = (name: string, path: string, url: URL) => setCookie(name, '', 0, path, url);

function readCookie(request: Request, name: string): string | undefined {
  for (const part of (request.headers.get('cookie') ?? '').split(';')) {
    const i = part.indexOf('=');
    if (i > -1 && part.slice(0, i).trim() === name) return part.slice(i + 1).trim();
  }
  return undefined;
}

// ---- signing ----

const encoder = new TextEncoder();
const decoder = new TextDecoder();

function base64url(bytes: Uint8Array): string {
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function fromBase64url(s: string): Uint8Array<ArrayBuffer> {
  return Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0));
}

const random = () => base64url(crypto.getRandomValues(new Uint8Array(32)));

const sha256 = async (s: string) => base64url(new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(s))));

const hmacKey = (secret: string) =>
  crypto.subtle.importKey('raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']);

/**
 * `payload.signature`, both base64url. The purpose is signed too, so the sign-in flow's cookie
 * can never pass as a session.
 */
async function seal(value: unknown, secret: string, purpose: string): Promise<string> {
  const payload = base64url(encoder.encode(JSON.stringify(value)));
  const signature = await crypto.subtle.sign('HMAC', await hmacKey(secret), encoder.encode(`${purpose}.${payload}`));
  return `${payload}.${base64url(new Uint8Array(signature))}`;
}

async function unseal<T>(token: string | undefined, secret: string, purpose: string): Promise<T | null> {
  const [payload, signature, extra] = (token ?? '').split('.');
  if (!payload || !signature || extra !== undefined) return null;
  try {
    const ok = await crypto.subtle.verify('HMAC', await hmacKey(secret), fromBase64url(signature), encoder.encode(`${purpose}.${payload}`));
    return ok ? (JSON.parse(decoder.decode(fromBase64url(payload))) as T) : null;
  } catch {
    return null;
  }
}
