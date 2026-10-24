// Posts a form to its endpoint as JSON. Without JavaScript the same form posts natively and the
// endpoint redirects to /sent or /not-sent.

export type SubmitResult =
  | { ok: true }
  | { ok: false; kind: 'invalid'; errors: Record<string, string> }
  | { ok: false; kind: 'failed' };

declare global {
  interface Window { turnstile?: { reset: (el?: Element | string) => void } }
}

export async function submitForm(form: HTMLFormElement): Promise<SubmitResult> {
  const body = Object.fromEntries(new FormData(form).entries());
  try {
    const res = await fetch(form.action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
    });
    const data = (await res.json().catch(() => ({}))) as { ok?: boolean; errors?: Record<string, string> };
    if (res.ok && data.ok) return { ok: true };
    if (res.status === 400 && data.errors) return { ok: false, kind: 'invalid', errors: data.errors };
    return { ok: false, kind: 'failed' };
  } catch {
    return { ok: false, kind: 'failed' };
  } finally {
    // A Turnstile token works once; get a fresh one for the next try
    const widget = form.querySelector('.cf-turnstile');
    if (widget && window.turnstile) window.turnstile.reset(widget);
  }
}

/** Show or clear one field's error, wired through aria-invalid and aria-describedby */
export function setFieldError(form: HTMLFormElement, name: string, message: string | undefined) {
  const field = form.elements.namedItem(name) as HTMLInputElement | HTMLTextAreaElement | null;
  const slot = form.querySelector<HTMLElement>(`[data-error-for="${name}"]`);
  if (field) field.setAttribute('aria-invalid', String(Boolean(message)));
  if (slot) {
    slot.textContent = message ?? '';
    slot.hidden = !message;
  }
}
