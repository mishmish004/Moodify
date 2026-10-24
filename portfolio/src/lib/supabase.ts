// Inserts one row through Supabase's REST API with the publishable key. Row-level security lets
// that key insert into the two form tables and nothing else: it can't read, change or delete.

export interface SupabaseConfig { url: string; key: string }

export type InsertResult =
  | { ok: true }
  | { ok: false; duplicate: boolean; status: number; detail: string };

export async function insertRow(cfg: SupabaseConfig, table: string, row: Record<string, unknown>, fetchImpl: typeof fetch = fetch): Promise<InsertResult> {
  const res = await fetchImpl(`${cfg.url.replace(/\/+$/, '')}/rest/v1/${table}`, {
    method: 'POST',
    headers: {
      apikey: cfg.key,
      Authorization: `Bearer ${cfg.key}`,
      'Content-Type': 'application/json',
      // Without this PostgREST would read the row back, which the key isn't allowed to do
      Prefer: 'return=minimal',
    },
    body: JSON.stringify(row),
    signal: AbortSignal.timeout(8000),
  });
  if (res.ok) return { ok: true };
  const detail = await res.text().catch(() => '');
  // 23505 = unique_violation (the same email asking for Moodify access twice)
  return { ok: false, duplicate: res.status === 409 && detail.includes('23505'), status: res.status, detail: detail.slice(0, 500) };
}
