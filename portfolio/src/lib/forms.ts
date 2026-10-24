// Validation for the two forms. The same rules run in the browser (for instant messages) and on
// the Worker (the only check that counts). The database repeats the limits as constraints.

export const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export const LIMITS = { name: 200, email: 254, message: 5000, note: 500 } as const;

export const TOPICS = ['A role', 'A project', 'A bug on this site', 'Just saying hi'] as const;
export type Topic = (typeof TOPICS)[number];

export const MESSAGES = {
  name: 'Add your name so I know who to thank.',
  email: 'I’ll need an email to reply to, like name@example.com.',
  message: 'Write a line or two first.',
  spotifyEmail: 'Add the email you use for Spotify, like name@gmail.com.',
  tooLong: (n: number) => `Keep it under ${n.toLocaleString('en')} characters.`,
} as const;

export interface ContactInput { topic: Topic | null; name: string; email: string; message: string }
export interface AccessInput { name: string | null; email: string; note: string | null }

export type Result<T, K extends string> =
  | { ok: true; value: T }
  | { ok: false; errors: Partial<Record<K, string>> };

const text = (v: unknown) => (typeof v === 'string' ? v.trim() : '');

export function validateContact(raw: Record<string, unknown>): Result<ContactInput, 'name' | 'email' | 'message' | 'topic'> {
  const name = text(raw.name);
  const email = text(raw.email);
  const message = text(raw.message);
  const topicRaw = text(raw.topic);
  const errors: Partial<Record<'name' | 'email' | 'message' | 'topic', string>> = {};

  if (!name) errors.name = MESSAGES.name;
  else if (name.length > LIMITS.name) errors.name = MESSAGES.tooLong(LIMITS.name);
  if (!EMAIL_RE.test(email) || email.length > LIMITS.email) errors.email = MESSAGES.email;
  if (!message) errors.message = MESSAGES.message;
  else if (message.length > LIMITS.message) errors.message = MESSAGES.tooLong(LIMITS.message);
  const topic = (TOPICS as readonly string[]).includes(topicRaw) ? (topicRaw as Topic) : null;
  if (topicRaw && !topic) errors.topic = 'Pick one of the topics, or none.';

  if (Object.keys(errors).length) return { ok: false, errors };
  return { ok: true, value: { topic, name, email, message } };
}

export function validateAccess(raw: Record<string, unknown>): Result<AccessInput, 'name' | 'email' | 'note'> {
  const name = text(raw.name);
  const email = text(raw.email);
  const note = text(raw.note);
  const errors: Partial<Record<'name' | 'email' | 'note', string>> = {};

  if (name.length > LIMITS.name) errors.name = MESSAGES.tooLong(LIMITS.name);
  if (!EMAIL_RE.test(email) || email.length > LIMITS.email) errors.email = MESSAGES.spotifyEmail;
  if (note.length > LIMITS.note) errors.note = MESSAGES.tooLong(LIMITS.note);

  if (Object.keys(errors).length) return { ok: false, errors };
  return { ok: true, value: { name: name || null, email, note: note || null } };
}
