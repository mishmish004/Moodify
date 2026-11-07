// Draws the link-preview card (1200×630) for every write-up into public/og/<file>.png, so a link
// posted on X, LinkedIn or WhatsApp unfolds into a picture of the post. Runs before `astro build`
// and `astro dev`. Satori lays the card out with the site's own fonts (bundled in src/og/fonts,
// all under the SIL Open Font License) and sharp turns it into a PNG.
import { mkdir, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import satori from 'satori';
import sharp from 'sharp';
import { parse } from 'yaml';

const ROOT = new URL('..', import.meta.url).pathname;
const POSTS = join(ROOT, 'src/content/posts');
const FONTS = join(ROOT, 'src/og/fonts');
const OUT = join(ROOT, 'public/og');
const SHOW_DRAFTS = process.env.PUBLIC_SHOW_DRAFTS === 'true' || process.argv.includes('--drafts');

const C = { ground: '#FDFDFC', surface: '#FFFFFF', ink: '#1C1B1B', ink2: '#2A2927', muted: '#5F5E5A', line: '#E7E5E0', chip: '#F3F1EC', tile: '#EFEDE8' };
const DEVA = '०१२३४५६७८९';
const deva = (n) => String(n).padStart(2, '0').replace(/\d/g, (d) => DEVA[Number(d)]);
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const month = new Intl.DateTimeFormat('en-US', { month: 'short', year: 'numeric', timeZone: 'UTC' });

// Tiny element helper so the layout reads like markup
const h = (type, style, ...children) => ({ type, props: { style: { display: 'flex', ...style }, children: children.flat().filter((c) => c !== null && c !== false) } });

async function readPosts() {
  const files = (await readdir(POSTS)).filter((f) => f.endsWith('.md') && !f.startsWith('_'));
  const posts = [];
  for (const file of files) {
    const text = await readFile(join(POSTS, file), 'utf8');
    const m = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
    if (!m) continue;
    const data = parse(m[1]) ?? {};
    if (data.draft && !SHOW_DRAFTS) continue;
    posts.push({ slug: file.replace(/\.md$/, ''), ...data, kind: data.kind ?? 'incident', date: new Date(data.date), tags: data.tags ?? [] });
  }
  return posts;
}

function card(post, number) {
  const titleSize = post.title.length <= 18 ? 116 : post.title.length <= 34 ? 88 : 66;
  const label = post.kind === 'incident' ? 'The incident log' : 'Off the record';
  const mono = { fontFamily: 'Geist Mono', fontSize: 22, letterSpacing: 1.5, color: C.muted };
  return h('div', { width: '100%', height: '100%', boxSizing: 'border-box', background: C.ground, padding: '52px 64px', flexDirection: 'column', fontFamily: 'Hanken' },
    // Header: the logo pill and the domain
    h('div', { width: '100%', justifyContent: 'space-between', alignItems: 'center' },
      h('div', { background: C.surface, border: `2px solid ${C.line}`, borderRadius: 999, padding: '8px 26px', fontSize: 32, fontWeight: 600, color: C.ink }, 'amish'),
      h('div', mono, 'MISH4MISH.COM'),
    ),
    // The post, with its number set large beside it like the page's section numbers
    h('div', { flexGrow: 1, width: '100%', alignItems: 'center', justifyContent: 'space-between' },
      h('div', { flexDirection: 'column', width: 860 },
        h('div', { ...mono, marginBottom: 18 }, `${label} · ${cap(post.category ?? '')} · ${month.format(post.date)}`.toUpperCase()),
        h('div', { fontSize: titleSize, lineHeight: 1.02, letterSpacing: -titleSize * 0.025, color: C.ink }, post.title),
        h('div', { marginTop: 20, fontSize: 34, lineHeight: 1.35, color: C.muted }, post.summary ?? ''),
      ),
      h('div', { fontFamily: 'Tiro', fontSize: 230, lineHeight: 1, color: C.tile }, number),
    ),
    // Tags
    h('div', { width: '100%', gap: 12, flexWrap: 'wrap' },
      post.tags.slice(0, 5).map((t) => h('div', { background: C.chip, color: C.ink2, borderRadius: 999, padding: '8px 18px', fontFamily: 'Geist Mono', fontSize: 22 }, t)),
    ),
  );
}

const font = async (file) => readFile(join(FONTS, file));
const fonts = [
  { name: 'Hanken', data: await font('HankenGrotesk-Regular.ttf'), weight: 400, style: 'normal' },
  { name: 'Hanken', data: await font('HankenGrotesk-SemiBold.ttf'), weight: 600, style: 'normal' },
  { name: 'Geist Mono', data: await font('GeistMono-Medium.ttf'), weight: 400, style: 'normal' },
  { name: 'Tiro', data: await font('TiroDevanagariHindi-Regular.ttf'), weight: 400, style: 'normal' },
];

const posts = await readPosts();
// Numbered in the order they were written, within each list
const numbers = new Map();
for (const kind of ['incident', 'off-the-record']) {
  posts.filter((p) => p.kind === kind).sort((a, b) => a.date - b.date).forEach((p, i) => numbers.set(p.slug, deva(i + 1)));
}

await rm(OUT, { recursive: true, force: true });
await mkdir(OUT, { recursive: true });
for (const post of posts) {
  const svg = await satori(card(post, numbers.get(post.slug)), { width: 1200, height: 630, fonts });
  const png = await sharp(Buffer.from(svg)).png({ compressionLevel: 9 }).toBuffer();
  await writeFile(join(OUT, `${post.slug}.png`), png);
}
console.log(`og: drew ${posts.length} card${posts.length === 1 ? '' : 's'} into public/og/`);
