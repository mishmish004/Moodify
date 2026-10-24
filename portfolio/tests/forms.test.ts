// node --test tests/ : the form endpoints with Supabase, Turnstile and email faked out.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import { handleForm, type Deps } from '../src/lib/endpoint.ts';
import { validateAccess, validateContact } from '../src/lib/forms.ts';
import { buildMessage } from '../src/lib/notify.ts';
import { accessSpec, contactSpec } from '../src/lib/specs.ts';

const SUPABASE = { url: 'https://abc.supabase.co/', key: 'sb_publishable_test' };
const good = { topic: 'A role', name: 'Ada', email: 'ada@example.com', message: 'Hello there' };

type Call = { url: string; init: RequestInit };
function fakeFetch(respond: (url: string, init: RequestInit) => Response) {
  const calls: Call[] = [];
  const fn = (async (input: string | URL | Request, init: RequestInit = {}) => {
    const url = String(input);
    calls.push({ url, init });
    return respond(url, init);
  }) as typeof fetch;
  return { fn, calls };
}

const created = () => new Response(null, { status: 201 });
const jsonReq = (body: unknown) =>
  new Request('https://mish4mish.com/api/contact', { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body) });
const formReq = (body: Record<string, string>) =>
  new Request('https://mish4mish.com/api/contact', { method: 'POST', body: new URLSearchParams(body) });

function deps(extra: Partial<Deps> = {}) {
  const f = fakeFetch(created);
  return { f, d: { supabase: SUPABASE, fetch: f.fn, ...extra } as Deps };
}

describe('contact endpoint', () => {
  test('a valid message is saved with the publishable key and answers ok', async () => {
    const { f, d } = deps();
    const res = await handleForm(jsonReq(good), contactSpec, d);
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { ok: true });
    assert.equal(f.calls.length, 1);
    const { url, init } = f.calls[0];
    assert.equal(url, 'https://abc.supabase.co/rest/v1/contact_messages');
    const h = new Headers(init.headers);
    assert.equal(h.get('apikey'), 'sb_publishable_test');
    assert.equal(h.get('authorization'), 'Bearer sb_publishable_test');
    assert.equal(h.get('prefer'), 'return=minimal');
    assert.deepEqual(JSON.parse(String(init.body)), { topic: 'A role', name: 'Ada', email: 'ada@example.com', message: 'Hello there' });
  });

  test('missing fields come back as 400 with the page’s own messages, and nothing is saved', async () => {
    const { f, d } = deps();
    const res = await handleForm(jsonReq({ name: ' ', email: 'nope', message: '' }), contactSpec, d);
    assert.equal(res.status, 400);
    const body = (await res.json()) as { ok: boolean; errors: Record<string, string> };
    assert.equal(body.ok, false);
    assert.deepEqual(Object.keys(body.errors).sort(), ['email', 'message', 'name']);
    assert.equal(f.calls.length, 0);
  });

  test('the honeypot field quietly drops bots', async () => {
    const { f, d } = deps();
    const res = await handleForm(jsonReq({ ...good, company: 'Spam Inc' }), contactSpec, d);
    assert.equal(res.status, 200);
    assert.equal(f.calls.length, 0);
  });

  test('a plain form post (no JavaScript) is redirected to /sent or /not-sent', async () => {
    const ok = await handleForm(formReq(good), contactSpec, deps().d);
    assert.equal(ok.status, 303);
    assert.equal(ok.headers.get('location'), '/sent');
    const bad = await handleForm(formReq({ ...good, email: 'x' }), contactSpec, deps().d);
    assert.equal(bad.status, 303);
    assert.equal(bad.headers.get('location'), '/not-sent');
  });

  test('without Supabase settings the form says it is unavailable', async () => {
    const res = await handleForm(jsonReq(good), contactSpec, { supabase: null });
    assert.equal(res.status, 503);
  });

  test('a Supabase error is reported as a failure, never as sent', async () => {
    const f = fakeFetch(() => new Response('{"message":"boom"}', { status: 500 }));
    const logs: string[] = [];
    const res = await handleForm(jsonReq(good), contactSpec, { supabase: SUPABASE, fetch: f.fn, log: (m) => logs.push(m) });
    assert.equal(res.status, 502);
    assert.ok(logs.includes('supabase insert failed'));
  });

  test('a body over 20 kB is refused', async () => {
    const { f, d } = deps();
    const res = await handleForm(jsonReq({ ...good, message: 'a'.repeat(30_000) }), contactSpec, d);
    assert.equal(res.status, 400);
    assert.equal(f.calls.length, 0);
  });

  test('an email copy goes out with Reply-To set to the visitor', async () => {
    const sent: { subject: string; replyTo?: string }[] = [];
    const { d } = deps({ sendCopy: async (m) => { sent.push(m); } });
    await handleForm(jsonReq(good), contactSpec, d);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].replyTo, 'ada@example.com');
    assert.match(sent[0].subject, /A role from Ada/);
  });

  test('a failing email copy does not fail the form', async () => {
    const { d } = deps({ sendCopy: async () => { throw new Error('no route'); } });
    const res = await handleForm(jsonReq(good), contactSpec, d);
    assert.equal(res.status, 200);
  });
});

describe('Turnstile', () => {
  test('with a secret set, a missing token is refused before anything is saved', async () => {
    const { f, d } = deps({ turnstileSecret: 'secret' });
    const res = await handleForm(jsonReq(good), contactSpec, d);
    assert.equal(res.status, 403);
    assert.equal(f.calls.length, 0);
  });

  test('a token Cloudflare accepts lets the message through', async () => {
    const f = fakeFetch((url) =>
      url.includes('challenges.cloudflare.com') ? Response.json({ success: true }) : created(),
    );
    const res = await handleForm(jsonReq({ ...good, 'cf-turnstile-response': 'tok' }), contactSpec, {
      supabase: SUPABASE, fetch: f.fn, turnstileSecret: 'secret', ip: '203.0.113.9',
    });
    assert.equal(res.status, 200);
    assert.equal(f.calls.length, 2);
    const sent = f.calls[0].init.body as FormData;
    assert.equal(sent.get('response'), 'tok');
    assert.equal(sent.get('remoteip'), '203.0.113.9');
  });

  test('a token Cloudflare rejects is refused', async () => {
    const f = fakeFetch(() => Response.json({ success: false }));
    const res = await handleForm(jsonReq({ ...good, 'cf-turnstile-response': 'bad' }), contactSpec, {
      supabase: SUPABASE, fetch: f.fn, turnstileSecret: 'secret',
    });
    assert.equal(res.status, 403);
  });
});

describe('Moodify access endpoint', () => {
  test('saves the request; name and note are optional', async () => {
    const { f, d } = deps();
    const res = await handleForm(jsonReq({ email: 'friend@gmail.com' }), accessSpec, d);
    assert.equal(res.status, 200);
    assert.equal(f.calls[0].url, 'https://abc.supabase.co/rest/v1/moodify_access_requests');
    assert.deepEqual(JSON.parse(String(f.calls[0].init.body)), { name: null, email: 'friend@gmail.com', note: null });
  });

  test('asking twice shows the same thanks and sends no second email', async () => {
    const f = fakeFetch(() => new Response('{"code":"23505","message":"duplicate key value"}', { status: 409 }));
    const sent: unknown[] = [];
    const res = await handleForm(jsonReq({ email: 'friend@gmail.com' }), accessSpec, {
      supabase: SUPABASE, fetch: f.fn, sendCopy: async (m) => { sent.push(m); },
    });
    assert.equal(res.status, 200);
    assert.equal(sent.length, 0);
  });

  test('a bad Spotify email gets the Spotify hint', () => {
    const r = validateAccess({ email: 'friend' });
    assert.equal(r.ok, false);
    if (!r.ok) assert.match(r.errors.email!, /Spotify/);
  });
});

describe('validation and email', () => {
  test('topics outside the list are refused; an empty topic is fine', () => {
    assert.equal(validateContact({ ...good, topic: 'Free money' }).ok, false);
    assert.equal(validateContact({ ...good, topic: '' }).ok, true);
  });

  test('line breaks in a header cannot add headers, and non-ASCII subjects are encoded', () => {
    const raw = buildMessage({
      from: 'forms@mish4mish.com', to: 'me@example.com',
      replyTo: 'x@example.com\r\nBcc: victim@example.com',
      subject: 'नमस्ते\nBcc: victim@example.com', text: 'hi',
      date: new Date(0), id: 'fixed',
    });
    const headers = raw.split('\r\n\r\n')[0].split('\r\n');
    assert.ok(!headers.some((h) => h.startsWith('Bcc:')));
    assert.ok(headers.some((h) => h.startsWith('Subject: =?UTF-8?B?')));
    assert.ok(headers.includes('Message-ID: <fixed@mish4mish.com>'));
  });
});
