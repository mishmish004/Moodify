# mish4mish.com

The portfolio, built from the "Portfolio" design canvas. [Astro](https://astro.build) turns every
page into plain HTML at build time, so it loads fast on phones and needs only a few lines of
JavaScript where something moves. It runs on a Cloudflare Worker. Two small endpoints save the
contact form and Moodify access requests to Supabase.

| Page | URL | Content lives in |
| --- | --- | --- |
| Home: serious-work, dilly-dallying | `/` | `src/content/projects/`, `src/site/pages.ts` (`home`) |
| The incident log ("yapping") | `/yapping`, `/yapping/<file>` | `src/content/posts/*.md` |
| Project case study | `/projects/<file>` | body of `src/content/projects/<file>.md` |
| About, Résumé, Now, Uses, Contact | `/about` … | `src/site/pages.ts` |
| Privacy + "Want to try Moodify?" | `/privacy` | `src/pages/privacy.astro`, `moodifyAccess` in `pages.ts` |
| Secret page (7 taps on the logo) | `/good-morning`, `/good-morning/<file>` | `src/content/songs/*.md`, `life` in `pages.ts` |
| RSS for the incident log | `/yapping/rss.xml` | generated |

## Run it

Node 24 (`.node-version`).

```bash
npm install
npm run dev        # http://localhost:4321, drafts included
npm test           # form endpoint tests
npm run check      # types
npm run build      # what Cloudflare runs
npm run preview    # the build on the real Workers runtime; reads .dev.vars
```

## Writing

- **A write-up:** copy `src/content/posts/sample-incident.md`, rename it (the file name is the URL),
  fill it in, delete `draft: true`, push. Leave out the "At a glance" fields you don't need.
  `kind: off-the-record` lists a post only on the secret page.
- **A case study:** add a Markdown body to a project file in `src/content/projects/`. The "Case
  study" link appears once there is a body. `sample-case-study.md` shows every field.
- **A song:** copy `src/content/songs/sample-song.md`; `rank` orders the top five; `spotify` is
  the ID from `open.spotify.com/track/<ID>`; `start`/`end` set the clip in seconds.
- **Everything else** is in `src/site/pages.ts`. Empty fields are hidden, so the site never shows
  a placeholder. Search that file for `TODO` to see what's still missing.
- **Images** go in `public/images/` and are referenced as `/images/name.jpg`.

Drafts (`draft: true`) show in `npm run dev` and never in production. To check drafts in a
build, set `PUBLIC_SHOW_DRAFTS=true`.

## Supabase (once)

1. Create a project (any region; Mumbai, `ap-south-1`, is closest).
2. SQL Editor → paste and run `supabase/migrations/20261024000000_portfolio_forms.sql`.
   It creates `contact_messages` and `moodify_access_requests`, and lets the publishable key
   **insert only**: it can't read, edit or delete rows.
3. Project Settings → API keys: copy the **Project URL** and the **publishable key**
   (`sb_publishable_…`). The secret / `service_role` key is not needed and should never be added here.

Read messages in Table Editor. For Moodify requests, set `status` to `added` once you've added
the person in the Spotify developer dashboard.

## Cloudflare (once)

1. Workers & Pages → Create → **Import a repository** → this repo.
   - Root directory: `portfolio`
   - Build command: `npm run build`
   - Deploy command: `npx wrangler deploy`
   - Build variable `NODE_VERSION=24` (Astro 7 needs Node 22.23.4 or newer)
2. Worker → Settings → **Variables and secrets** → add as *Secret*:
   `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`.
3. Deploy. `wrangler.jsonc` attaches the Worker to `mish4mish.com` and creates the DNS record.
   Delete any existing A/AAAA/CNAME record for the apex first, or the attach fails.
4. **www → apex:** Rules → Redirect Rules → template *Redirect from WWW to root*. It needs a
   proxied DNS record for `www` (e.g. `AAAA www 100::`, proxied).
5. **Moodify** stays where it is hosted; point `moodify` at it with a CNAME in the same zone.

Every push to the repo's production branch rebuilds and redeploys.

### Optional

- **Spam check (Turnstile):** Turnstile → add a widget for `mish4mish.com`. Put the site key in
  Settings → Build → Variables as `PUBLIC_TURNSTILE_SITE_KEY`, and the secret key as the Worker
  secret `TURNSTILE_SECRET_KEY`. The privacy page mentions Turnstile only when it's on.
- **Email copy of each message:** Email → Email Routing → enable for `mish4mish.com` and verify
  `amish.shah.work@gmail.com` as a destination. Uncomment `send_email` in `wrangler.jsonc` and add
  the secret `NOTIFY_FROM=forms@mish4mish.com`. Without this, messages are saved to Supabase only.
