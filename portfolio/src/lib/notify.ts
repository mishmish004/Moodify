// Emails a plain-text copy of a form message through Cloudflare Email Routing. Optional: it runs
// only when the NOTIFY_EMAIL send_email binding (wrangler.jsonc) and NOTIFY_FROM are both set.

/** Strips line breaks so a visitor can't add headers of their own */
const oneLine = (s: string) => s.replace(/[\r\n]+/g, ' ').trim();

function base64(s: string): string {
  const bytes = new TextEncoder().encode(s);
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

/** RFC 2047 encoded-word, so non-ASCII subjects survive */
const encodeHeader = (s: string) => (/^[\x20-\x7e]*$/.test(s) ? s : `=?UTF-8?B?${base64(s)}?=`);

export function buildMessage(opts: { from: string; to: string; replyTo?: string; subject: string; text: string; date?: Date; id?: string }): string {
  const domain = opts.from.split('@')[1] ?? 'mish4mish.com';
  const body = base64(opts.text).replace(/.{76}/g, '$&\r\n');
  return [
    `From: mish4mish.com <${oneLine(opts.from)}>`,
    `To: <${oneLine(opts.to)}>`,
    ...(opts.replyTo ? [`Reply-To: <${oneLine(opts.replyTo)}>`] : []),
    `Subject: ${encodeHeader(oneLine(opts.subject))}`,
    `Date: ${(opts.date ?? new Date()).toUTCString()}`,
    `Message-ID: <${opts.id ?? crypto.randomUUID()}@${domain}>`,
    'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=utf-8',
    'Content-Transfer-Encoding: base64',
    '',
    body,
  ].join('\r\n');
}

interface SendEmail { send: (message: unknown) => Promise<void> }

export async function notify(binding: SendEmail | undefined, from: string | undefined, to: string, msg: { replyTo?: string; subject: string; text: string }): Promise<void> {
  if (!binding || !from) return;
  const { EmailMessage } = await import('cloudflare:email');
  await binding.send(new EmailMessage(from, to, buildMessage({ from, to, ...msg })));
}
