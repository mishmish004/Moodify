// Copy for the single pages. Empty strings and empty lists are hidden on the page, so fill a
// TODO when you have the words and it appears; nothing half-written ever shows.
import { site } from './site';

export const home = {
  // Photos that rotate inside the headline ("I try to build at [photo] scale"), in order.
  // Put the files in public/images/ and list them here. With none, the headline reads without it.
  photos: [] as { src: string; alt: string }[],
  photoIntervalMs: 1500,
  // dilly-dallying: side quests, experiments and things made for fun
  sideQuests: [
    {
      title: 'Moodify',
      year: '2026',
      line: 'A small app that turns how you feel into a Spotify playlist. Just for me and four friends.',
      href: site.moodify,
      image: undefined as { src: string; alt: string } | undefined,
    },
  ],
};

export const about = {
  headline: 'Hi, I’m Amish.',
  subline: 'I build backend systems that have to be right.',
  // 4:5 portrait in public/images/, e.g. { src: '/images/portrait.jpg', alt: '…' }
  portrait: undefined as { src: string; alt: string } | undefined,
  // "the short version": TODO three short paragraphs. Where you are now (role, the systems you
  // work on, what you care about getting right); how you got here (the first thing you built,
  // the first thing you broke, what made you stay); what you want next.
  shortEn: [] as string[],
  // The same three paragraphs in Hindi; the "hindi mai" button shows only when these are set
  shortHi: [] as string[],
  facts: [
    { k: 'Based in', v: site.city },
    { k: 'Studying', v: 'B.Tech IT, MIT Manipal' },
    { k: 'Mostly', v: 'Backends, data pipelines, reliability' },
    // TODO: { k: 'Off the clock', v: 'Two things you do for fun' },
  ],
  principles: [
    { t: 'Every bug that reaches production gets a write-up.', d: 'What broke, how it was found, and what changed after. They live in the incident log.' },
    // TODO: two more, each a title and one or two lines on what it means in practice
  ],
};

export const contact = {
  // e.g. 'Open to: full-time roles · interesting side projects'. Empty hides the chip.
  openTo: '',
  // e.g. 'I usually reply within 3 days.'
  replyNote: '',
  topics: ['A role', 'A project', 'A bug on this site', 'Just saying hi'] as const,
};

export const now = {
  updated: 'Oct 23, 2026',
  from: site.city,
  rows: [
    { k: 'building', v: 'Moodify', note: 'A small app that turns how you feel into a Spotify playlist. Just for me and four friends.', href: site.moodify, link: 'moodify.mish4mish.com' },
    { k: 'at work', v: 'Trippy AI' },
    { k: 'learning', v: 'Go', note: 'The language a lot of distributed systems are written in, like Kubernetes, etcd and Docker. Goroutines and channels make concurrent backend services far less painful to get right.' },
    { k: 'reading', v: 'The Count of Monte Cristo by Alexandre Dumas', note: 'Yes, it’s a cliché. It earns it.' },
    // TODO: { k: 'on repeat', v: 'Song by Artist', href: 'https://open.spotify.com/track/…', link: 'Play on Spotify' },
    { k: 'not doing', v: 'Model-inference-adjacent gigs' },
  ] as { k: string; v: string; note?: string; href?: string; link?: string }[],
};

type UseItem = { name: string; why?: string; href?: string; linkLabel?: string };

export const uses = {
  updated: 'Oct 2026',
  groups: [
    // TODO: laptop, monitor, keyboard, mouse or trackpad, each with why it stayed
    { title: 'Desk', items: [] as UseItem[] },
    {
      title: 'Editor and terminal',
      items: [
        // TODO: editor (theme, font, the one setting you'd fight for), terminal, shell and prompt
        { name: 'Dotfiles', href: site.github.url, linkLabel: 'On GitHub' },
      ] as UseItem[],
    },
    {
      title: 'Languages and services',
      items: [
        { name: 'Go', why: 'TokenMeter’s exactly-once metering runs on it.' },
        { name: 'Rust', why: 'The DDIA storage engines are written in it.' },
        { name: 'Python', why: 'RegisterWatch and Moodify.' },
        { name: 'PostgreSQL', why: 'Where RegisterWatch keeps every licence over time.' },
        { name: 'Kafka', why: 'Transactions are what make TokenMeter’s totals match.' },
      ] as UseItem[],
    },
    {
      title: 'For listening',
      items: [
        { name: 'Spotify' }, // TODO: why: what's on rotation while you work
        // TODO: { name: 'Headphones', why: '…' },
        { name: 'Moodify', why: 'A small thing I built that turns how I feel into a Spotify playlist.', href: site.moodify, linkLabel: 'moodify.mish4mish.com' },
      ] as UseItem[],
    },
    {
      title: 'This site',
      items: [
        { name: 'Astro', why: 'Every page is plain HTML built ahead of time, with a few lines of JavaScript where something moves.' },
        { name: 'Cloudflare Workers', why: 'Serves the pages from close to wherever you are, and runs the two forms.' },
        { name: 'Supabase', why: 'Where messages from the forms land.' },
        { name: 'Aptos Display, Hanken Grotesk, Geist Mono, Tiro Devanagari Hindi', why: 'The typefaces you’re reading right now. Aptos shows where it’s installed; Hanken Grotesk stands in everywhere else.' },
      ] as UseItem[],
    },
  ],
};

export const resume = {
  updated: 'Oct 2026',
  // A PDF in public/, e.g. '/amish-shah-resume.pdf'. Empty hides the Download button.
  pdf: '',
  summary: 'I build backend systems that have to be right: exactly-once metering on Kafka, storage engines in Rust, data pipelines that never lose a row. Co-founder of Viralyzer, which I built end to end.',
  jobs: [
    {
      role: 'Technical Co-founder', company: 'Viralyzer', when: 'Aug 2026 – now',
      points: [
        'Built Viralyzer entirely, from the first commit to production.',
        'Viralyzer is AI that doomscrolls, spots, tracks, scripts, edits and generates content for you, so you don’t have to.',
        'Built to make life easier for social media marketers: a must-have for making short-form and long-form content in 2026.',
      ],
      // TODO: Viralyzer's URL. Empty hides "Visit Viralyzer".
      site: '', siteLabel: 'Visit Viralyzer',
      repos: [] as { name: string; href: string }[],
      // Write-ups tagged viralyzer are listed under this job automatically
      tag: 'viralyzer',
    },
    {
      role: 'Intern', company: 'startups', when: '2024 – 2025',
      // TODO: what you built or shipped, one line per startup
      points: [] as string[],
      site: '', siteLabel: '',
      // TODO: { name: 'repo-name', href: 'https://github.com/…' }
      repos: [] as { name: string; href: string }[],
      tag: '',
    },
  ],
  projects: [
    { name: 'TokenMeter', line: 'Exactly-once usage metering for an LLM gateway, in Go on Kafka transactions. A chaos test kills the worker mid-load and the totals still match.', href: 'https://github.com/mishmish004/TokenMeter' },
    { name: 'DDIA storage', line: 'The storage engines from Designing Data-Intensive Applications, built in Rust: hash log, SSTable, LSM-tree, B-tree and column store.', href: 'https://github.com/mishmish004/ddia-storage' },
    { name: 'RegisterWatch', line: 'Daily snapshots of 21 gambling regulators’ public registers across 18 jurisdictions, in Python on Postgres. Every licence and blocked domain is tracked over time, and a run that doesn’t finish never deletes data.', href: 'https://github.com/mishmish004/RegisterWatch' },
  ],
  skills: [
    { group: 'Languages', items: ['Python', 'Java', 'Go', 'Rust'] },
    { group: 'Infra', items: ['Kafka (KRaft)', 'PostgreSQL', 'Supabase', 'Redis', 'Qdrant', 'Docker', 'Docker Compose', 'Caddy'] },
    { group: 'Practices', items: ['Incident write-ups', 'Chaos testing', 'Integration tests (testcontainers, pytest)'] },
  ],
  education: { degree: 'B.Tech, Information Technology', school: 'Manipal Institute of Technology (MIT Manipal)', when: '2023 – 2027' },
};

export const moodifyAccess = {
  // 'open' while there are spots; 'waitlist' once Spotify's developer-mode limit is reached
  mode: 'open' as 'open' | 'waitlist',
  // YouTube playlist of people using Moodify. Empty hides "Watch the demos".
  demos: '',
};

export const life = {
  // The clip that plays when you press "good morning." Track ID from open.spotify.com/track/<ID>
  goodMorning: { spotify: '', start: 0, end: 30 },
};
