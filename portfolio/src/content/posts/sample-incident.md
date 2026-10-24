---
# A sample showing every field. Drafts appear in `npm run dev` only.
# Copy this file, rename it (the file name becomes the URL: /yapping/<file-name>),
# fill it in and delete `draft: true`.
title: "[Post title: what broke, in plain words]"
summary: "[The one-paragraph summary: what users saw, how long it lasted, and the root cause in a single line.]"
line: "[One line: what broke, the impact, and the fix.]"
date: 2026-10-01
category: databases
featured: true
status: Resolved
impact: "[Who or what was affected]"
duration: "[How long]"
detectedBy: "[Alert, user report, luck]"
rootCause: "[One line]"
timeline:
  - { time: "[HH:MM]", event: "[First alert or report]" }
  - { time: "[HH:MM]", event: "[Started investigating; first hypothesis]" }
  - { time: "[HH:MM]", event: "[Mitigation applied]" }
  - { time: "[HH:MM]", event: "[Resolved; back to normal]" }
tags: ["[Tag]", "[Tag]"]
draft: true
---

## What happened

[Set the scene: what the system does, what normal looks like, and the first sign that something was off.]

[What you saw next: errors, dashboards, user reports. Keep it concrete.]

## Digging in

[The investigation: what you checked, the dead ends, and the clue that cracked it.]

```
// [Paste the log line, query, config or diff that mattered]
// [Keep it short: just enough to show the problem]
```

[Explain what the snippet shows and why it pointed to the cause.]

## Root cause

[The actual cause, explained so someone who wasn't there gets it.]

## The fix

[The short-term mitigation, then the permanent fix, and how you confirmed it worked.]

> “[The one lesson worth remembering from this.]”

## What I'd do differently

- [A monitoring or alerting change]
- [A code or architecture change]
- [A process change]
