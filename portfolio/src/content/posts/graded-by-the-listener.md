---
title: "Graded by the Listener"
summary: "Turning Google's ReasoningBank into memory for a playlist agent."
date: 2026-11-07
category: agents
tags: ["ReasoningBank", "Agent memory", "Embeddings", "OpenAI"]
---

Moodify is a small app I built that turns how you feel into a Spotify playlist. Four friends and I use it. You type something like “gym session, running on 4 hours of sleep”, it writes a brief, picks the songs and saves the playlist to your account. Afterwards you can rate it and write a review.

At first, Moodify had no memory of what you thought of its playlists. If a workout playlist was too slow, the next one could be too slow in exactly the same way.

I didn't want to invent agent memory from scratch, so I built it on a published design: Google's ReasoningBank. This post is about what I kept from the paper, what broke when I applied it to music, and the four feedback loops Moodify runs now.

## <span class="sec-num">०१</span>What the paper does

ReasoningBank is memory for agents that do long, multi-step tasks, like browsing the web or fixing code. Most agents start every task from zero: they repeat yesterday's mistakes and forget what worked. The paper's answer is to write down short lessons after every task and read the relevant ones back before the next.

<figure class="diagram">
<span class="diagram-label today">The paper</span>
<div class="diagram-scroll"><svg width="680" height="250" viewBox="0 0 680 250" role="img" aria-label="ReasoningBank's loop: retrieve memory items by similarity, act, let an LLM judge grade the run, extract at most three items, and append them to a memory bank that only grows." style="flex: none; font-family: var(--sans)"><defs><marker id="rb1-g" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#7A7873"/></marker></defs>
<g fill="none" stroke="#7A7873" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M184,60 H260" marker-end="url(#rb1-g)"/><path d="M420,60 H496" marker-end="url(#rb1-g)"/><path d="M576,88 V168" marker-end="url(#rb1-g)"/><path d="M496,196 H420" marker-end="url(#rb1-g)"/><path d="M260,196 H184" marker-end="url(#rb1-g)"/><path d="M104,168 V88" marker-end="url(#rb1-g)"/></g>
<rect x="24" y="32" width="160" height="56" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="260" y="32" width="160" height="56" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="496" y="32" width="160" height="56" rx="10" fill="#FBEDE6" stroke="#A8431E" stroke-width="1.5"/><rect x="496" y="168" width="160" height="56" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="260" y="168" width="160" height="56" rx="10" fill="#FBEDE6" stroke="#A8431E" stroke-width="1.5"/><rect x="24" y="168" width="160" height="56" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/>
<g text-anchor="middle" font-size="14" font-weight="600" fill="#1C1B1B"><text x="104" y="56">Retrieve</text><text x="340" y="56">Act</text><text x="576" y="56">LLM judge</text><text x="576" y="192">Extract</text><text x="340" y="192">Append</text><text x="104" y="192">Memory bank</text></g>
<g text-anchor="middle" font-size="12" fill="#5F5E5A"><text x="104" y="74">by similarity</text><text x="340" y="74">run the task</text><text x="576" y="210">≤ 3 items per run</text><text x="104" y="210">only grows</text></g>
<g text-anchor="middle" font-size="12" fill="#A8431E"><text x="576" y="74">grades its own run</text><text x="340" y="210">nothing is ever edited</text></g>
<g font-size="13" fill="#5F5E5A"><text x="222" y="52" text-anchor="middle">items</text><text x="458" y="52" text-anchor="middle">the run</text><text x="568" y="132" text-anchor="end">success or failure</text><text x="458" y="188" text-anchor="middle">items</text><text x="222" y="188" text-anchor="middle">adds</text><text x="112" y="132">next task</text></g></svg></div>
<figcaption>ReasoningBank's loop. The two stages in rust are the ones that didn't fit Moodify: a model grading its own work, and a memory that can only grow.</figcaption>
</figure>

Three ideas carried straight over:

1. **Lessons, not logs.** A memory item is three short fields: a title, a description of when it applies, and the content, the advice itself. A transcript of a run is long and hard to reuse. “Keep workout sections at energy 8 or more” isn't.
2. **Failures count.** The paper learns from “both successful and failed experiences”. A playlist that missed teaches as much as one that landed.
3. **At most three per run.** A few sharp lessons beat a pile of vague ones.

## <span class="sec-num">०२</span>Where the paper broke

### <span class="sub-num">a)</span>Self-graded outcomes

The paper usually has no answer key, so it asks a model to grade its own run: “LLM-as-a-judge … without any ground-truth reference”. Moodify has something better, the listener's review. But a review is free text in any shape (slang, typos, one word or a paragraph), and a model reading it can still put words in the listener's mouth.

<div class="fix"><span class="fix-label">What worked</span><span>The review is the judge, and every lesson has to quote it. One LLM call reads the review next to everything about the build: the tracks by section, likes, the rating and the build log. It returns a verdict and up to three lessons, each carrying the exact words it rests on. Code checks those words are really in the review, and a lesson without a real quote is dropped.</span></div>

```
review    "the workout part was way too slow, rest was great"
verdict   partly
title     Workout sections need more energy
content   Keep workout sections at energy 8 or more from the first song.
kind      pitfall, lasting · tags: workout, energy
evidence  "the workout part was way too slow"
```

Each lesson also has a kind. Pitfalls and strategies are the paper's failures and successes. Preferences are new: general taste like “too much Drake”, which outranks the taste Moodify reads from Spotify when the two disagree.

### <span class="sub-num">b)</span>Append-only memory

The paper adds new items “with a simple addition operation”. Nothing is ever edited. That works for web tasks, where a strategy that worked stays true. People change their minds. If a listener once wrote “loves long intros” and later “skip the long intros, they bore me”, an append-only memory keeps both, and the next build gets two lessons that contradict each other.

<div class="fix"><span class="fix-label">What worked</span><span>Consolidation with four operations instead of one. A second LLM call compares each new lesson with the existing ones that share a tag or are close in meaning, and picks ADD, UPDATE, SUPERSEDE or NOOP. Newer feedback always wins. A superseded lesson stays in the table, pointing at the one that replaced it.</span></div>

<figure class="diagram">
<span class="diagram-label after">After</span>
<div class="diagram-scroll"><svg width="680" height="290" viewBox="0 0 680 290" role="img" aria-label="A lesson's life in Moodify. A draft is not stored when it has no exact quote or memory already holds it, kept for one playlist when it is only about that playlist, or added or merged into an active lesson. An active lesson is superseded by a newer contradicting lesson, or expires when unused for 180 days." style="flex: none; font-family: var(--sans)"><defs><marker id="rb2-g" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#7A7873"/></marker><marker id="rb2-b" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#1F5FBF"/></marker></defs>
<g fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M126,150 H216" stroke="#7A7873" marker-end="url(#rb2-g)"/><path d="M71,122 V50 H216" stroke="#1F5FBF" marker-end="url(#rb2-b)"/><path d="M71,178 V250 H216" stroke="#1F5FBF" marker-end="url(#rb2-b)"/><path d="M356,140 H420 V90 H548" stroke="#1F5FBF" marker-end="url(#rb2-b)"/><path d="M356,160 H420 V210 H548" stroke="#1F5FBF" marker-end="url(#rb2-b)"/></g>
<rect x="16" y="122" width="110" height="56" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="216" y="122" width="140" height="56" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="216" y="26" width="140" height="48" rx="10" fill="#E8EFFA" stroke="#1F5FBF" stroke-width="1.5"/><rect x="216" y="226" width="140" height="48" rx="10" fill="#E8EFFA" stroke="#1F5FBF" stroke-width="1.5"/><rect x="548" y="62" width="116" height="56" rx="10" fill="#E8EFFA" stroke="#1F5FBF" stroke-width="1.5"/><rect x="548" y="182" width="116" height="56" rx="10" fill="#E8EFFA" stroke="#1F5FBF" stroke-width="1.5"/>
<g text-anchor="middle" font-size="14" font-weight="600" fill="#1C1B1B"><text x="71" y="146">Draft</text><text x="286" y="146">Active</text><text x="286" y="46">Not stored</text><text x="286" y="246">This playlist</text><text x="606" y="86">Superseded</text><text x="606" y="206">Expired</text></g>
<g text-anchor="middle" font-size="12" fill="#5F5E5A"><text x="71" y="164">from a review</text><text x="286" y="164">recalled into builds</text><text x="286" y="63">never in memory</text><text x="286" y="263">only feeds Fix it</text><text x="606" y="104">kept as history</text><text x="606" y="224">not recalled</text></g>
<text x="171" y="142" text-anchor="middle" font-size="13" fill="#5F5E5A">add / update</text>
<g font-size="13" font-weight="500" fill="#1F5FBF"><text x="80" y="28">no exact quote,</text><text x="80" y="42">or already known</text><text x="80" y="242">about one playlist</text><text x="430" y="50">a newer lesson</text><text x="430" y="65">contradicts it</text><text x="430" y="80">or “from now on”</text><text x="430" y="187">unused 180 days</text><text x="430" y="202">or past 40 active</text></g></svg></div>
<figcaption>A lesson's life. In the paper an item is appended and never changes. Here a draft can be dropped, kept for one playlist, added, or folded into an existing lesson, and an active lesson can be superseded or expire. Both stay in the table as a record.</figcaption>
</figure>

A few rules keep it safe:

- Lessons about one playlist (“track 4 didn't fit”) never reach memory. They only feed the Fix it button.
- At most 40 active lessons per listener. Past that, consolidation merges near-duplicates, and as a last resort the least-used lessons expire.
- If the consolidation call fails, every new lesson is simply added, so a grounded lesson is never lost.

### <span class="sub-num">c)</span>Similarity-only recall

The paper recalls memory “using embedding-based similarity search”. With at most 40 lessons per listener, I started with plain tags, plus a word list so “gym session” finds a lesson tagged workout. Tags still missed “leg day”. When I added embeddings, they missed things too: “homework” never reached the focus lesson. And one similarity threshold either missed half the matches or let in up to ten wrong ones.

<div class="fix"><span class="fix-label">What worked</span><span>Both paths, and either one is enough. A lesson is recalled if it shares a tag with the request, or if its similarity is at least 0.36, or 0.15 above the median of that listener's lessons. The relative rule fixed the threshold. If the embeddings call fails, recall falls back to tags and the build carries on.</span></div>

<figure class="diagram">
<span class="diagram-label after">After</span>
<div class="diagram-scroll"><svg width="680" height="262" viewBox="0 0 680 262" role="img" aria-label="Recall has two paths. The tag path maps everyday words to shared tags and checks overlap with lesson tags. The meaning path embeds the text and compares it with lesson vectors. A lesson that fits either way goes into the prompt, up to eight lessons." style="flex: none; font-family: var(--sans)"><defs><marker id="rb3-g" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#7A7873"/></marker><marker id="rb3-b" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#1F5FBF"/></marker></defs>
<g fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M136,120 H156 V58 H176" stroke="#1F5FBF" marker-end="url(#rb3-b)"/><path d="M326,58 H366" stroke="#1F5FBF" marker-end="url(#rb3-b)"/><path d="M516,58 H610 V102" stroke="#1F5FBF" marker-end="url(#rb3-b)"/><path d="M516,202 H610 V158" stroke="#1F5FBF" marker-end="url(#rb3-b)"/><path d="M136,140 H156 V202 H176" stroke="#7A7873" marker-end="url(#rb3-g)"/><path d="M326,202 H366" stroke="#7A7873" marker-end="url(#rb3-g)"/><path d="M441,108 V86" stroke="#7A7873" marker-end="url(#rb3-g)"/><path d="M441,152 V174" stroke="#7A7873" marker-end="url(#rb3-g)"/></g>
<rect x="16" y="102" width="120" height="56" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="176" y="30" width="150" height="56" rx="10" fill="#E8EFFA" stroke="#1F5FBF" stroke-width="1.5"/><rect x="366" y="30" width="150" height="56" rx="10" fill="#E8EFFA" stroke="#1F5FBF" stroke-width="1.5"/><rect x="366" y="108" width="150" height="44" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="176" y="174" width="150" height="56" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="366" y="174" width="150" height="56" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="556" y="102" width="108" height="56" rx="10" fill="#E8EFFA" stroke="#1F5FBF" stroke-width="1.5"/>
<g text-anchor="middle" font-size="14" font-weight="600" fill="#1C1B1B"><text x="76" y="121">Query</text><text x="251" y="54">Words → tags</text><text x="441" y="54">Tag overlap</text><text x="441" y="126">Lessons</text><text x="251" y="198">Embed</text><text x="441" y="198">Close in meaning</text><text x="610" y="121">Either way</text></g>
<g text-anchor="middle" font-size="12" fill="#5F5E5A"><text x="76" y="137">prompt, then</text><text x="76" y="151">brief + sections</text><text x="251" y="72">gym → workout</text><text x="441" y="72">shares a tag</text><text x="441" y="142">tags + vectors</text><text x="251" y="216">3-large, 512 dims</text><text x="441" y="216">≥ 0.36 or median + 0.15</text><text x="610" y="137">≤ 8 lessons</text><text x="610" y="151">into the prompt</text></g>
<g font-size="13" font-weight="500" fill="#1F5FBF"><text x="524" y="50">by tag</text><text x="524" y="194">by meaning</text></g>
<text x="251" y="250" text-anchor="middle" font-size="13" fill="#5F5E5A">if the call fails: tags only</text></svg></div>
<figcaption>Recall. The paper recalls by embedding similarity alone, the lower path. Moodify added the tag path, and a lesson gets in if either path finds it. Each build runs this twice: once on the prompt, then on the brief and on each section alone.</figcaption>
</figure>

## <span class="sec-num">०३</span>Four loops, four clocks

The paper describes one loop. Moodify now runs four, each on a different clock.

<figure class="diagram">
<span class="diagram-label after">Moodify, now</span>
<div class="diagram-scroll"><svg width="680" height="376" viewBox="0 0 680 376" role="img" aria-label="Moodify's four feedback loops. Loop 1 asks one question before building. Loop 2 rebuilds one playlist from its review. Loop 3, the paper's loop, turns reviews into lessons that later builds recall. Loop 4 expires lessons that go unused." style="flex: none; font-family: var(--sans)"><defs><marker id="rb4-g" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#7A7873"/></marker><marker id="rb4-b" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#1F5FBF"/></marker><marker id="rb4-d" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="4" markerHeight="4" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#1C1B1B"/></marker></defs>
<g fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M164,130 H214" stroke="#7A7873" marker-end="url(#rb4-g)"/><path d="M354,130 H404" stroke="#7A7873" marker-end="url(#rb4-g)"/><path d="M524,130 H564" stroke="#7A7873" marker-end="url(#rb4-g)"/><path d="M64,104 V64" stroke="#1F5FBF" marker-end="url(#rb4-b)"/><path d="M124,64 V104" stroke="#1F5FBF" marker-end="url(#rb4-b)"/><path d="M54,236 V156" stroke="#1F5FBF" marker-end="url(#rb4-b)"/><path d="M614,288 V336 H94 V288" stroke="#1F5FBF" marker-end="url(#rb4-b)"/><path d="M324,288 C324,318 244,318 244,288" stroke="#1F5FBF" marker-end="url(#rb4-b)"/></g>
<g fill="none" stroke="#1C1B1B" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M614,156 V236" marker-end="url(#rb4-d)"/><path d="M564,262 H524" marker-end="url(#rb4-d)"/><path d="M404,262 H354" marker-end="url(#rb4-d)"/><path d="M244,236 V196 H134 V156" marker-end="url(#rb4-d)"/><path d="M314,236 V156" marker-end="url(#rb4-d)"/></g>
<rect x="24" y="16" width="140" height="48" rx="10" fill="#E8EFFA" stroke="#1F5FBF" stroke-width="1.5"/><rect x="24" y="104" width="140" height="52" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="214" y="104" width="140" height="52" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="404" y="104" width="120" height="52" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="564" y="104" width="100" height="52" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="24" y="236" width="140" height="52" rx="10" fill="#E8EFFA" stroke="#1F5FBF" stroke-width="1.5"/><rect x="214" y="236" width="140" height="52" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="404" y="236" width="120" height="52" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/><rect x="564" y="236" width="100" height="52" rx="10" fill="#FFFFFF" stroke="#D9D6CF"/>
<g text-anchor="middle" font-size="14" font-weight="600" fill="#1C1B1B"><text x="94" y="36">Ask one question</text><text x="94" y="126">Read prompt</text><text x="284" y="126">Pick songs</text><text x="464" y="126">Playlist</text><text x="614" y="126">Listener</text><text x="94" y="258">Fix it</text><text x="284" y="258">Lessons</text><text x="464" y="258">Consolidate</text><text x="614" y="258">Reflect</text></g>
<g text-anchor="middle" font-size="12" fill="#5F5E5A"><text x="94" y="53">waits up to 20 s</text><text x="94" y="143">writes the brief</text><text x="284" y="143">search and order</text><text x="464" y="143">on Spotify</text><text x="614" y="143">likes, review</text><text x="94" y="275">builds version 2</text><text x="284" y="275">≤ 40 per listener</text><text x="464" y="275">add · supersede</text><text x="614" y="275">≤ 3, quoted</text></g>
<g font-size="13" fill="#5F5E5A"><text x="189" y="122" text-anchor="middle">brief</text><text x="379" y="122" text-anchor="middle">tracks</text><text x="622" y="200">review</text><text x="379" y="254" text-anchor="middle">writes</text><text x="146" y="188">recall 1</text><text x="322" y="200">recall 2</text></g>
<g font-size="13" font-weight="500" fill="#1F5FBF"><text x="58" y="88" text-anchor="end">clash</text><text x="132" y="88">answer</text><text x="62" y="212">correction</text><text x="480" y="354" text-anchor="middle">this playlist's lessons</text></g>
<g text-anchor="middle" font-size="12" font-weight="700" fill="#FFFFFF"><circle cx="184" cy="40" r="10" fill="#1F5FBF"/><text x="184" y="44">1</text><circle cx="34" cy="358" r="10" fill="#1F5FBF"/><text x="34" y="362">2</text><circle cx="420" cy="200" r="10" fill="#1C1B1B"/><text x="420" y="204">3</text><circle cx="372" cy="308" r="10" fill="#1F5FBF"/><text x="372" y="312">4</text></g>
<g font-size="13" font-weight="500" fill="#1F5FBF"><text x="200" y="44">ask first · seconds</text><text x="50" y="362">fix this playlist · minutes to days</text><text x="388" y="312">unused 180 days → expired</text></g>
<text x="436" y="198" font-size="13" font-weight="600" fill="#1C1B1B">between playlists</text><text x="436" y="214" font-size="12" fill="#5F5E5A">the paper's loop</text></svg></div>
<figcaption>The four loops. The heavy dark loop is the paper's; the three blue loops are what Moodify added around it.</figcaption>
</figure>

1. **Seconds: ask before building.** When a prompt pulls against a lesson (“gym session, running on 4 hours of sleep” against “workout energy 8 or more”), Moodify asks one question and waits up to 20 seconds. The call that writes the brief spots the clash itself, so asking costs no extra LLM call. “From now on” retires the lesson. No answer keeps it, so a build never stalls. This is the first step of Meta's PAHF loop, which the paper doesn't have.
2. **Minutes to days: Fix it.** The lessons about one playlist, plus the review itself, rebuild that playlist as version 2.
3. **Between playlists: the paper's loop.** Reviews become lessons, and later builds recall them.
4. **Months: upkeep.** Every recall marks a lesson as used. Pitfalls and strategies nobody recalls for 180 days expire. Preferences never expire; they can only be superseded.

## <span class="sec-num">०४</span>What it measured

Memory has a cost the paper doesn't deal with: it changes the prompt, and a prompt that changes on every build can't be reused from OpenAI's prompt cache. So memory comes in two parts. A per-listener block (preferences, liked songs, playlists rated 8 or more) is rendered once and kept until new feedback arrives, and sits right after the fixed rules. Only the lessons recalled for this request change from build to build. My first layout cached 53% of input tokens; this one caches 98%.

Each of these phases shipped with a live check against the real model, next to the mocked tests.

<div class="cost">
<table style="border-collapse: collapse; width: 100%; min-width: 0"><thead><tr><th style="text-align: left; padding: 12px 16px; font-family: var(--mono); font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; font-weight: 500; color: #5F5E5A; border-bottom: 1px solid #E7E5E0">Check</th><th style="text-align: right; padding: 12px 16px; font-family: var(--mono); font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; font-weight: 500; color: #5F5E5A; border-bottom: 1px solid #E7E5E0">Result</th></tr></thead><tbody><tr><td style="padding: 14px 16px; border-bottom: 1px solid #E7E5E0; vertical-align: top; font-size: 16px; line-height: 24px; color: #2A2927">Builds with lessons vs without<br><span style="font-size: 13px; color: #5F5E5A">judged against 5 test reviews</span></td><td style="padding: 14px 16px; border-bottom: 1px solid #E7E5E0; vertical-align: top; text-align: right; font-family: var(--mono); font-size: 15px; line-height: 24px; white-space: nowrap; color: #1F5FBF; font-weight: 500">5 of 5</td></tr><tr><td style="padding: 14px 16px; border-bottom: 1px solid #E7E5E0; vertical-align: top; font-size: 16px; line-height: 24px; color: #2A2927">Next gym brief after a “too slow” review<br><span style="font-size: 13px; color: #5F5E5A">energy, out of 10</span></td><td style="padding: 14px 16px; border-bottom: 1px solid #E7E5E0; vertical-align: top; text-align: right; font-family: var(--mono); font-size: 15px; line-height: 24px; white-space: nowrap; color: #1F5FBF; font-weight: 500">8 → 9</td></tr><tr><td style="padding: 14px 16px; border-bottom: 1px solid #E7E5E0; vertical-align: top; font-size: 16px; line-height: 24px; color: #2A2927">Input tokens cached<br><span style="font-size: 13px; color: #5F5E5A">second build, a minute later</span></td><td style="padding: 14px 16px; border-bottom: 1px solid #E7E5E0; vertical-align: top; text-align: right; font-family: var(--mono); font-size: 15px; line-height: 24px; white-space: nowrap; color: #1F5FBF; font-weight: 500">98%</td></tr><tr><td style="padding: 14px 16px; border-bottom: 1px solid #E7E5E0; vertical-align: top; font-size: 16px; line-height: 24px; color: #2A2927">Ask before building<br><span style="font-size: 13px; color: #5F5E5A">6 prompts, 3 runs</span></td><td style="padding: 14px 16px; border-bottom: 1px solid #E7E5E0; vertical-align: top; text-align: right; font-family: var(--mono); font-size: 15px; line-height: 24px; white-space: nowrap; color: #1F5FBF; font-weight: 500">17 of 18 right</td></tr><tr><td style="padding: 14px 16px; vertical-align: top; font-size: 16px; line-height: 24px; color: #2A2927">Recall by meaning<br><span style="font-size: 13px; color: #5F5E5A">14 live requests</span></td><td style="padding: 14px 16px; vertical-align: top; text-align: right; font-family: var(--mono); font-size: 15px; line-height: 24px; white-space: nowrap; color: #1F5FBF; font-weight: 500">12 found, 1 wrong</td></tr></tbody></table>
</div>

<div class="post-cta"><span>Building agent memory too?</span><a class="btn btn-dark" href="/contact">Say hello</a></div>
