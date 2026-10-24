Design the complete set of pages for **Moodify**, a small web app that turns how you feel into a Spotify playlist. It will live at **https://moodify.mish4mish.com**, a separate site under my main domain mish4mish.com. Every feature gets its own page and URL under that host (paths listed below).

## What Moodify is

- A private app for me and up to four friends (Spotify allows at most 5 users for apps like this). Everyone knows each other.
- You type how you feel or what you're doing, in any words: "chill summer morning, then 30 min workout, then instantly lock in for coding". Moodify reads it into a plan with sections, lengths and rules, picks songs that fit your taste, and saves a private playlist to your Spotify account.
- It learns from your Spotify listening, your likes, your 1–10 ratings and your written reviews.
- Friends can share their taste, make **Moodify Blends** together, and hear a playlist "the way a friend would hear it". Sharing happens only if each person switches it on.

## Who uses it, and the tone

- Mostly on phones, sometimes on a laptop.
- Friends of different backgrounds, including people who prefer literal, predictable interfaces: some are autistic, some write very detailed prompts, some type one emoji. The interface must feel calm and predictable:
  - clear labels
  - no surprise motion
  - nothing flashing
  - the same layout patterns everywhere
- Writing is plain and warm, in sentence case. Short sentences. No hype, no jargon.
- Some prompts are emotional ("I feel numb today"). Never make light of a mood. For a playlist made from a distressing prompt, the page shows one calm support line ("If things feel heavy right now, you don't have to carry it alone. Free, confidential support, wherever you are: findahelpline.com"). It is never a warning banner.

## Hard constraints

**How it's built.** Pages are server-rendered HTML with one CSS file and small vanilla JavaScript per page; charts are hand-drawn SVG. There is no React or other framework, and no external scripts or CDNs. Design only what plain HTML, CSS and SVG can do. Use CSS custom properties for every colour and size so the design maps straight onto stylesheet tokens.

**Phone first.**
- Design at 390 px wide first, then 1280 px.
- Inputs use 16 px text, so iOS doesn't zoom in on focus.
- Tap targets are at least 44 px; 16 px side gutters.
- No horizontal scrolling.

**Light and dark mode.** Both are designed, not automatically inverted.

**Accessibility.**
- WCAG AA contrast everywhere.
- Visible focus states, and everything works from the keyboard.
- Every animation has a `prefers-reduced-motion` version that simply shows the end state.
- Colour is never the only way to tell things apart.

**Spotify's brand rules.** These are a hard requirement, not optional:
- **Spotify logo:**
  - Use only Spotify's official logo and icon, never redrawn, recoloured, rotated or stretched. Black on light backgrounds, white on dark.
  - Full logo at least 70 px wide; icon alone at least 21 px, and only where the full logo doesn't fit. Clear space around it of half the icon's height.
- **Link back on everything:**
  - Every playlist shown has an **"OPEN SPOTIFY"** button with the full logo.
  - Every track row links to the track on open.spotify.com, with the Spotify icon.
  - Track and artist names appear exactly as Spotify gives them. Long names wrap; they are never cut off for good.
- **No co-branding:** Moodify's own identity must not look like Spotify's. No green circular logo, no Spotify green as Moodify's brand colour, and nothing implying Spotify endorses Moodify.
- **No album artwork for now:** we don't fetch it. If you add a slot for it later, the artwork is never cropped or overlaid.

**Privacy.** Friends never see anyone's prompt text, reviews, ratings, or playlists marked hidden or made from a distressing prompt. They see only playlist titles, and taste data from people who switched sharing on.

## Visual direction

Propose a visual identity for Moodify that is clearly its own:
- a wordmark
- a colour system (light and dark tokens)
- type (system fonts are fine; one web font at most)
- radii, spacing and elevation.

It should feel like a mood ring: soft, personal, a little playful, but calm and very readable. Energy (1–10) is a recurring idea: a playlist's sections move between energy levels. You may use energy as a gentle visual motif, such as a subtle gradient or a waveform line, provided it never carries information by colour alone and passes contrast. Start with a small design system:
- tokens
- buttons (primary, quiet, destructive)
- text inputs and a large textarea
- select, switch and checkbox
- notice and support line
- list row with the Spotify icon
- section heading with a start time
- chips for people
- 1–10 rating control and like (♥) button
- progress steps
- the chart components below.

Apply them to every page.

## Global layout

- **Header:** Moodify wordmark (links to the home page) and the main navigation for logged-in users: New playlist, History, Friends, Map, Activity. Blend lives on Friends.
  - On phones: suggest the best pattern, either a compact header or a bottom tab bar with at most 5 items.
- **Footer:** "Logged in as Amish", a link to the data page, and a small link back to mish4mish.com.
- **Logged-out pages** show only the wordmark.

## Pages

Status: **Live** means built today and needs a redesign. **Planned (Phase N)** means not built yet; design it from this description.

**1. Login / welcome** (`/login`, Live)
- Headline: "Playlists for the mood you're in".
- A short explanation, and one primary button: "Log in with Spotify".
- A friends-only note: "Your Spotify account has to be added first; ask whoever sent you the link".
- A plain "What happens to your data" section (what goes to OpenAI, what's stored, how Disconnect deletes everything).
- Variant: a notice above the headline, "Moodify can now use more of your Spotify… please log in again. Nothing you saved is lost."

**2. About your taste** (`/onboarding`, Live)
- Seven questions:
  - favourite genres
  - three artists you love
  - the energy you usually want (Low / Medium / High / Depends on the day)
  - are instrumental tracks OK?
  - languages (optional)
  - anything to avoid (optional)
  - songs you already know, or new ones (Mostly new / A mix / Mostly ones I know)
- One Save button.
- Shown on first login, and later via "Change your answers".

**3. Home: the mood box** (`/`, Live)
- Heading: "How are you feeling?"
- A large textarea taking up to 2,000 characters. Placeholder: "e.g. chill summer morning, then 30 min workout, then straight into focus mode. Say it however you like: a word, a list, a paragraph."
- "Make a playlist" button with a busy state ("Making your playlist…").
- Note: "Takes a minute or two".
- Recent playlists (5): title, date, length and track count, Spotify icon. Then "All your playlists".
- Collapsible "What the model is told about your taste", showing a paragraph and the Spotify logo.
- At the bottom, a quiet destructive button: "Disconnect and delete my data". It asks for confirmation.
- Variant: the textarea pre-filled after "Adjust and redo".

**4. Making your playlist** (`/jobs/{id}`, Live)
- The prompt, plus a progress list:
  1. Reading your taste
  2. Reading your prompt
  3. Asking for song ideas
  4. Finding the songs on Spotify (12/30)
  5. Saving the playlist to your Spotify
- The page refreshes itself every 3 seconds and usually takes 1–3 minutes.
- Failure states, each with a title, one sentence and a Back button:
  - No song ideas this time
  - Nothing matched
  - Spotify is asking us to slow down
  - Interrupted
  - Something went wrong
- Planned (Phase 17): on a phone, when the build finishes, show a large "OPEN SPOTIFY" button that tries to open the Spotify app straight away.

**5. The playlist** (`/playlists/{id}`, Live; parts Planned)
- Title, e.g. "Moodify: Summer morning → workout → lock in". Then "19 tracks, 58 min, saved as a private playlist in your Spotify account".
- OPEN SPOTIFY button (full logo). "Play now" button with the note "Needs Premium and Spotify open on one of your devices".
- **"How Moodify read this"** box:
  - A one-sentence summary.
  - Each section with minutes asked and made ("Workout: driving, 30 min asked, 29 min made"), and "(straight in)" when it starts with a hard cut.
  - "Rules kept: no sad songs; no instrumental tracks."
  - "You wrote: …" (the full prompt, muted).
  - An "Adjust and redo" link.
- **Track list under section headings with start times:**
  - "Summer morning · starts at 0:00": Slide (Calvin Harris, Frank Ocean, Migos); Summertime (DJ Jazzy Jeff & The Fresh Prince)
  - "Workout · starts at 14:00": Hatchback (Cochise); FE!N (Travis Scott, Playboi Carti)
  - "Lock in · starts at 42:00": Location (Playboi Carti); YOU'RE THE ONE (KAYTRANADA, Syd)
  - Numbering runs on across sections. Each row: number, title, artists, Spotify icon. The whole row links to Spotify.
- Planned:
  - (Phase 14) a ♥ on every row, and a 1–10 rating row for the playlist.
  - (Phase 15) a "How was it?" review box: a free-text textarea, examples like "the workout part dragged", and a "Fix it with this review" button that builds a version 2 linked to version 1.
- Variants:
  - the support line at the top
  - a single-section playlist, with no headings
  - an older playlist without a saved track list ("Open it in Spotify to see the songs").
- Bottom: "Make another".

**6. History** (`/history`, Live)
- All your playlists, newest first: title, date, length, track count, Spotify icon.
- Planned (Phase 14): rating and number of likes, and sorting by rating.
- Empty state: "Nothing yet. Make your first playlist."

**7. Message page** (Live)
- A generic page: title, one sentence, optional OPEN SPOTIFY button, Back.
- Used for "Queued", "Can't queue (needs Premium and Spotify open on a device)", "Disconnected", login errors ("Not on the list yet", "Login cancelled") and "Not found".

**8. What Moodify remembers** (`/memory`, Planned, Phase 16)
- The memory text exactly as the model sees it.
- The lessons learned from your reviews, each with the review it came from and a delete button.
- Delete one playlist from memory.
- Plain, a little reassuring.

**9. Sharing** (`/sharing`, Planned, Phase 18)
- Three switches, all off by default, each with one sentence on exactly what it shares:
  - Show my taste to friends
  - Let friends include me in Blends
  - Let friends hear music like me
- Also:
  - a note that playlists already in someone's Spotify stay there
  - a "Who used my taste" log ("Sam viewed your taste map · Tue", "Priya made a Blend with you · Mon")
  - "Hide from friends" managed per playlist.

**10. Friends** (`/friends`, Planned, Phases 18 and 21)
- The other people on Moodify who share something (at most 4), each as a card with what they share.
- Friend page (`/friends/{id}`):
  - name, top artists, their taste dimensions, and a taste match with you ("72% match")
  - a prompt box: "Make a playlist the way Sam would hear it"
- That playlist is saved in your account as "Moodify: Sunday cooking (as Sam hears it)".

**11. Moodify Blend** (`/blend`, Planned, Phase 20)
- Pick 1–4 friends as chips. You are always included. Friends who don't allow Blends are shown greyed out with a short reason.
- A prompt box, an optional "lean toward" control (equal by default) and a checkbox: "Make it public so members can open it".
- Result page:
  - title "Moodify Blend: Amish × Sam × Priya: Sunday cooking"
  - each track marked with whose taste it came from (initials) or "common ground"
  - a bar per member showing their share of the minutes.

**12. Taste map** (`/map`, Planned, Phase 23)
- Shows you and friends who share their taste (at most 5 people), in four views behind tabs:
  - **Profiles:** one row per dimension (energy, mood dark↔bright, acoustic↔electronic, still↔danceable, instrumental↔vocal-led, era). One dot per person on a shared axis, with a thin spread bar. The selected person is in the accent colour and everyone else is grey; tap a name to switch.
  - **Track map:** tracks plotted on two chosen dimensions (default energy × mood), one small panel per person. Point size shows how much they play it; tracks people share are ringed. Hovering or tapping a point shows the track and artist with the Spotify icon and link.
  - **Match:** a grid of taste-match scores between people (at most 5 × 5), in one hue, with the number in each cell. Under it, a "common ground" list of shared artists.
  - **Genres:** one horizontal stacked bar per person, top 6 genres plus Other.
- Every chart has a legend, direct labels and a "Show as table" toggle.
- Animation: on load, dots glide from the centre to their places (about 400 ms, eased). Switching axes or person morphs the points to their new positions. Reduced motion: no movement.

**13. Activity** (`/activity`, Planned, Phase 24)
- A feed, newest first:
  - "Sam made *Summer morning → workout → lock in* · 58 min · 3 parts"
  - "Priya made a Blend with you"
  - "Amish made a playlist the way you'd hear it", shown only to that friend.
- A **mood timeline**: one strip per person over the last 30 days. Each playlist is a mark placed by date, sized by length and shaded by average energy (one hue, light to dark). Hover shows the title.
- A **"Replay the week"** control (play, pause, step) that animates the feed and timelines day by day over about 3 seconds. Keyboard accessible; reduced motion steps without animating.

**14. Not found / errors** (Live): a 404 and a generic error, in the same message-page pattern.

## Chart rules (taste map and activity)

- Pick the chart by what the reader needs to do. No radar or spider charts: they distort comparisons and their shape depends on axis order.
- **Colours:**
  - People get categorical colours in a fixed order (person 1 always the same colour), at most 5.
  - Scatter-type charts never show more than 3 colours at once: use one panel per person, or highlight one person in the accent colour and grey the rest.
  - Magnitudes (energy, match score) use one hue from light to dark. Never a rainbow.
- Never two y-axes on one chart.
- Thin marks. Points at least 8 px, with hover and tap areas of at least 24 px.
- Gridlines and axes recede; text is never set in a series colour.
- A legend whenever there are two or more series, plus direct labels where they fit, and a table view for every chart.
- Colour-blind safe in both light and dark mode. Give me the hex values, so I can check them with a colour-blind validator.

## What I'd like back

1. The design system: tokens for light and dark, type scale, spacing, and the components listed above, each with its states (default, hover, focus, disabled, busy, error).
2. Every page above at 390 px. Also at 1280 px for home, the playlist page, the taste map and activity. Light mode for all; dark mode for home, the playlist page and the taste map.
3. The states listed for each page: empty, busy, failure, and the variants.
4. Short hand-off notes per component: CSS variable names, rough HTML structure, and how each animation behaves with and without reduced motion.

Use realistic sample data: me (Amish), friends Sam, Priya and Kabir, and the example playlist above.
