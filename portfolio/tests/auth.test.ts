// node --test tests/ : the Google sign-in gate, with Google's token endpoint faked out.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import { FLOW_COOKIE, gate, isPublic, readConfig, safeNext, SESSION_COOKIE, type AuthConfig, type AuthDeps } from '../src/lib/auth.ts';

const SITE = 'https://mish4mish.com';
const CONFIG: AuthConfig = { clientId: 'client-123.apps.googleusercontent.com', clientSecret: 'shh', secret: 'x'.repeat(40) };
const NOW = Date.UTC(2026, 9, 24, 12);
const DAY = 24 * 60 * 60 * 1000;

const get = (path: string, cookie?: string, init: RequestInit = {}) =>
  new Request(SITE + path, { ...init, headers: { ...(cookie ? { Cookie: cookie } : {}), ...init.headers } });

const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString('base64url');
const idToken = (claims: Record<string, unknown>) => `${b64({ alg: 'RS256' })}.${b64(claims)}.sig`;

/** Google's token endpoint, answering with an ID token built from the nonce it was sent */
function fakeGoogle(claims: (nonce: string) => Record<string, unknown>, status = 200) {
  const calls: URLSearchParams[] = [];
  let nonce = '';
  const fn = (async (_url: string | URL | Request, init: RequestInit = {}) => {
    calls.push(new URLSearchParams(String(init.body)));
    if (status !== 200) return new Response('{"error":"invalid_grant"}', { status });
    return Response.json({ access_token: 'at', id_token: idToken(claims(nonce)) });
  }) as typeof fetch;
  return { fn, calls, setNonce: (n: string) => { nonce = n; } };
}

const goodClaims = (nonce: string): Record<string, unknown> => ({
  iss: 'https://accounts.google.com', aud: CONFIG.clientId, sub: '1234567890', email: 'ada@gmail.com',
  email_verified: true, exp: NOW / 1000 + 3600, nonce,
});

/** Name=value of each Set-Cookie on a response */
function cookies(res: Response): Record<string, { value: string; header: string }> {
  const out: Record<string, { value: string; header: string }> = {};
  for (const header of res.headers.getSetCookie()) {
    const [pair] = header.split(';');
    const i = pair.indexOf('=');
    out[pair.slice(0, i)] = { value: pair.slice(i + 1), header };
  }
  return out;
}

/** /auth/google, then Google's redirect back to /auth/callback. Returns both responses. */
async function signIn(next = '/about', claims = goodClaims, deps: AuthDeps = {}) {
  const google = fakeGoogle(claims);
  const d: AuthDeps = { fetch: google.fn, now: () => NOW, ...deps };
  const started = (await gate(get(`/auth/google?next=${encodeURIComponent(next)}`), CONFIG, d))!;
  const to = new URL(started.headers.get('location')!);
  google.setNonce(to.searchParams.get('nonce')!);
  const flow = cookies(started)[FLOW_COOKIE].value;
  const back = (await gate(get(`/auth/callback?state=${to.searchParams.get('state')}&code=the-code`, `${FLOW_COOKIE}=${flow}`), CONFIG, d))!;
  return { started, to, back, google };
}

async function sessionCookie() {
  const { back } = await signIn();
  return `${SESSION_COOKIE}=${cookies(back)[SESSION_COOKIE].value}`;
}

describe('signed-out visitors', () => {
  test('every page goes to the sign-in page, which brings them back afterwards', async () => {
    const res = (await gate(get('/yapping/some-post?x=1'), CONFIG))!;
    assert.equal(res.status, 303);
    assert.equal(res.headers.get('location'), '/sign-in?next=%2Fyapping%2Fsome-post%3Fx%3D1');
    assert.equal(res.headers.get('cache-control'), 'no-store');
    const home = (await gate(get('/'), CONFIG))!;
    assert.equal(home.headers.get('location'), '/sign-in');
  });

  test('static files, feeds and form endpoints are closed too', async () => {
    for (const path of ['/index.html', '/images/me.jpg', '/_astro/index.astro_astro_type_script_index_0_lang.D7Au12gZ.js', '/_astro/photo.Ab12.webp', '/yapping/rss.xml', '/sitemap-index.xml', '/robots.txt']) {
      const res = await gate(get(path), CONFIG);
      assert.equal(res?.status, 303, path);
    }
    const post = await gate(get('/api/contact', undefined, { method: 'POST', body: '{}' }), CONFIG);
    assert.equal(post?.status, 303);
    assert.equal(post?.headers.get('location'), '/sign-in');
  });

  test('only the sign-in page, its styles and fonts, and the icon are open', async () => {
    for (const path of ['/sign-in', '/favicon.svg', '/_astro/Base.Cr5VhXrB.css', '/_astro/fonts/181a29443484632f.woff2']) {
      assert.equal(await gate(get(path), CONFIG), null, path);
    }
    for (const path of ['/_astro/fonts/../about.html', '/_astro/fonts/..%2Fabout.html', '/_astro/x.css/../../about', '/_astro/a%2F..%2Fb.css', '/sign-in/x', '/auth/other']) {
      assert.equal(isPublic(new URL(SITE + path).pathname), false, path);
    }
  });

  test('a tampered, expired or foreign session cookie counts as signed out', async () => {
    const good = await sessionCookie();
    const [payload, sig] = good.split('=')[1].split('.');
    const forged = `${SESSION_COOKIE}=${b64({ sub: 'me', exp: NOW + DAY })}.${sig}`;
    for (const cookie of [forged, `${SESSION_COOKIE}=${payload}`, `${SESSION_COOKIE}=${payload}.${sig}x`, `${SESSION_COOKIE}=garbage`]) {
      assert.equal((await gate(get('/about', cookie), CONFIG, { now: () => NOW }))?.status, 303, cookie);
    }
    assert.equal((await gate(get('/about', good), CONFIG, { now: () => NOW + 31 * DAY }))?.status, 303);
    assert.equal((await gate(get('/about', good), { ...CONFIG, secret: 'y'.repeat(40) }, { now: () => NOW }))?.status, 303);
  });

  test('the sign-in flow cookie can’t be used as a session', async () => {
    const started = (await gate(get('/auth/google'), CONFIG, { now: () => NOW }))!;
    const flow = cookies(started)[FLOW_COOKIE].value;
    assert.equal((await gate(get('/about', `${SESSION_COOKIE}=${flow}`), CONFIG, { now: () => NOW }))?.status, 303);
  });
});

describe('signing in with Google', () => {
  test('/auth/google sends the visitor to Google with PKCE, state and nonce', async () => {
    const { started, to } = await signIn();
    assert.equal(started.status, 303);
    assert.equal(to.origin + to.pathname, 'https://accounts.google.com/o/oauth2/v2/auth');
    const q = to.searchParams;
    assert.equal(q.get('client_id'), CONFIG.clientId);
    assert.equal(q.get('redirect_uri'), 'https://mish4mish.com/auth/callback');
    assert.equal(q.get('response_type'), 'code');
    assert.equal(q.get('scope'), 'openid email');
    assert.equal(q.get('code_challenge_method'), 'S256');
    assert.match(q.get('code_challenge')!, /^[\w-]{43}$/);
    assert.match(q.get('state')!, /^[\w-]{43}$/);
    assert.match(q.get('nonce')!, /^[\w-]{43}$/);
    const flow = cookies(started)[FLOW_COOKIE].header;
    assert.match(flow, /; Path=\/auth; Max-Age=600; HttpOnly; SameSite=Lax; Secure$/);
  });

  test('coming back from Google sets a 30-day session and returns to the page they asked for', async () => {
    const { back, google } = await signIn('/about?x=1');
    assert.equal(back.status, 303);
    assert.equal(back.headers.get('location'), '/about?x=1');
    const set = cookies(back);
    assert.match(set[SESSION_COOKIE].header, /; Path=\/; Max-Age=2592000; HttpOnly; SameSite=Lax; Secure$/);
    assert.match(set[FLOW_COOKIE].header, /^mish_oauth=; Path=\/auth; Max-Age=0/);

    // The code is exchanged with the client secret and the PKCE verifier
    const sent = google.calls[0];
    assert.equal(sent.get('code'), 'the-code');
    assert.equal(sent.get('client_secret'), 'shh');
    assert.equal(sent.get('redirect_uri'), 'https://mish4mish.com/auth/callback');
    assert.equal(sent.get('grant_type'), 'authorization_code');
    assert.match(sent.get('code_verifier')!, /^[\w-]{43}$/);

    const session = `${SESSION_COOKIE}=${set[SESSION_COOKIE].value}`;
    for (const path of ['/', '/about', '/_astro/x.js', '/images/me.jpg']) {
      assert.equal(await gate(get(path, session), CONFIG, { now: () => NOW }), null, path);
    }
    assert.equal(await gate(get('/api/contact', session, { method: 'POST', body: '{}' }), CONFIG, { now: () => NOW }), null);
  });

  test('a signed-in visitor on /sign-in goes straight on', async () => {
    const res = await gate(get('/sign-in?next=%2Fnow', await sessionCookie()), CONFIG, { now: () => NOW });
    assert.equal(res?.headers.get('location'), '/now');
  });

  test('backing out on Google’s screen says it was cancelled, and keeps where they were going', async () => {
    const started = (await gate(get('/auth/google?next=%2Fabout'), CONFIG, { now: () => NOW }))!;
    const flow = `${FLOW_COOKIE}=${cookies(started)[FLOW_COOKIE].value}`;
    const state = new URL(started.headers.get('location')!).searchParams.get('state');
    const res = (await gate(get(`/auth/callback?error=access_denied&state=${state}`, flow), CONFIG, { now: () => NOW }))!;
    assert.equal(res.headers.get('location'), '/sign-in?error=cancelled&next=%2Fabout');
    assert.equal(cookies(res)[SESSION_COOKIE], undefined);
  });

  test('a callback with the wrong state, no flow cookie or a stale flow is refused', async () => {
    const google = fakeGoogle(goodClaims);
    const started = (await gate(get('/auth/google'), CONFIG, { now: () => NOW }))!;
    const flow = `${FLOW_COOKIE}=${cookies(started)[FLOW_COOKIE].value}`;
    const state = new URL(started.headers.get('location')!).searchParams.get('state');
    const cases: [string, string | undefined, number][] = [
      [`/auth/callback?state=wrong&code=c`, flow, NOW],
      [`/auth/callback?state=${state}&code=c`, undefined, NOW],
      [`/auth/callback?state=${state}`, flow, NOW],
      [`/auth/callback?state=${state}&code=c`, flow, NOW + 11 * 60 * 1000],
    ];
    for (const [path, cookie, now] of cases) {
      const res = (await gate(get(path, cookie), CONFIG, { fetch: google.fn, now: () => now }))!;
      assert.equal(res.headers.get('location'), '/sign-in?error=expired', path);
      assert.equal(cookies(res)[SESSION_COOKIE], undefined);
    }
    assert.equal(google.calls.length, 0);
  });

  test('an ID token for another app, issuer or sign-in, or an expired one, is refused', async () => {
    const bad: ((n: string) => Record<string, unknown>)[] = [
      (n) => ({ ...goodClaims(n), aud: 'someone-else' }),
      (n) => ({ ...goodClaims(n), iss: 'https://evil.example' }),
      (n) => ({ ...goodClaims(n), nonce: `${n}x` }),
      (n) => ({ ...goodClaims(n), exp: NOW / 1000 - 1 }),
      (n) => ({ ...goodClaims(n), sub: undefined }),
    ];
    for (const claims of bad) {
      const logs: string[] = [];
      const { back } = await signIn('/', claims, { log: (m) => logs.push(m) });
      assert.equal(back.headers.get('location'), '/sign-in?error=failed');
      assert.equal(cookies(back)[SESSION_COOKIE], undefined);
      assert.deepEqual(logs, ['google id token rejected']);
    }
  });

  test('an error from Google’s token endpoint is a failed sign-in, logged', async () => {
    const google = fakeGoogle(goodClaims, 400);
    const logs: string[] = [];
    const started = (await gate(get('/auth/google'), CONFIG, { now: () => NOW }))!;
    const state = new URL(started.headers.get('location')!).searchParams.get('state');
    const res = (await gate(get(`/auth/callback?state=${state}&code=c`, `${FLOW_COOKIE}=${cookies(started)[FLOW_COOKIE].value}`), CONFIG, { fetch: google.fn, now: () => NOW, log: (m) => logs.push(m) }))!;
    assert.equal(res.headers.get('location'), '/sign-in?error=failed');
    assert.deepEqual(logs, ['google token exchange failed']);
  });

  test('signing out clears the session', async () => {
    const res = (await gate(get('/auth/sign-out', await sessionCookie()), CONFIG))!;
    assert.equal(res.headers.get('location'), '/sign-in?signed-out');
    assert.match(cookies(res)[SESSION_COOKIE].header, /^mish_session=; Path=\/; Max-Age=0/);
  });

  test('on plain-http localhost (npm run dev) the cookies drop Secure', async () => {
    const res = (await gate(new Request('http://localhost:4321/auth/google'), CONFIG))!;
    assert.equal(new URL(res.headers.get('location')!).searchParams.get('redirect_uri'), 'http://localhost:4321/auth/callback');
    assert.doesNotMatch(cookies(res)[FLOW_COOKIE].header, /Secure/);
  });
});

describe('without the Google settings', () => {
  test('nobody gets in, and the sign-in button says sign-in is unavailable', async () => {
    assert.equal((await gate(get('/about'), null))?.status, 303);
    assert.equal(await gate(get('/sign-in'), null), null);
    const logs: string[] = [];
    const res = (await gate(get('/auth/google'), null, { log: (m) => logs.push(m) }))!;
    assert.equal(res.headers.get('location'), '/sign-in?error=unavailable');
    assert.equal(logs.length, 1);
  });

  test('all three secrets are needed, and AUTH_SECRET must be 32+ characters', () => {
    const env = { GOOGLE_CLIENT_ID: 'id', GOOGLE_CLIENT_SECRET: 'secret', AUTH_SECRET: 'z'.repeat(32) };
    assert.deepEqual(readConfig(env), { clientId: 'id', clientSecret: 'secret', secret: 'z'.repeat(32) });
    assert.equal(readConfig({ ...env, GOOGLE_CLIENT_ID: '' }), null);
    assert.equal(readConfig({ ...env, GOOGLE_CLIENT_SECRET: undefined }), null);
    assert.equal(readConfig({ ...env, AUTH_SECRET: 'short' }), null);
    assert.equal(readConfig({}), null);
  });
});

describe('where to go after signing in', () => {
  const base = new URL(SITE);
  test('a path on this site is kept', () => {
    assert.equal(safeNext('/yapping/post?x=1#h', base), '/yapping/post?x=1#h');
  });
  test('other sites and the sign-in routes fall back to the home page', () => {
    for (const next of [null, '', 'about', '//evil.com/x', '/\\evil.com', 'https://evil.com', '/sign-in?next=/about', '/auth/sign-out']) {
      assert.equal(safeNext(next, base), '/', String(next));
    }
  });
});
