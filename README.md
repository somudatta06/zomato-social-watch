# Zomato Social Watch

Real-time social listening + triage + automated reply for Zomato's social-media team. Pulls live mentions from Reddit and X every 5 minutes, classifies each post (urgency, sentiment, audience routing, tripwires), routes hard-tripwire incidents through documented playbooks, and either auto-replies on safe customer-care posts or lets an operator drain the overdue backlog in one click.

Built as the AI-Native Intern take-home assignment.

---

## What it does, in one screen

```
                      ┌────────── HOME ──────────┐
   Reddit ─┐          │ Action needed: 186       │
           ├─► [classifier] ─►  [dispatcher] ─► Slack / Discord / Email
   X/X.com ┘          │   • urgency               │   Sheets / Linear ticket
                      │   • tripwires             │   Twitter reply (R+W)
                      │   • playbook match        │   Reddit comment (manual)
                      └───────────┬───────────────┘
                                  │
                          ┌───────┴────────┐
                          │  Inbox (live)  │
                          │  Activity log  │
                          │  Drain modal   │   ← bulk-fire safe replies
                          │  Playbook lock │   ← refuse on death/legal
                          └────────────────┘
```

Every escalated post becomes a *case*: ack-deadline tracked, escalation re-fires when nobody owns it, post-incident-review issue auto-created in Linear 24h after the incident.

---

## The brief, mapped to evidence

The take-home brief had eight hard requirements. Each one is provable by clicking through the running app:

| Brief checkbox                              | Evidence in the app                                                                |
| :------------------------------------------ | :--------------------------------------------------------------------------------- |
| Live data from Reddit + X (real, not mock)  | `Last sync 2m ago` on every page; raw scrapers in `social_watch/scrapers/`        |
| Refresh every ≤ 5 minutes                   | `REFRESH_INTERVAL=300` in `.env.example`; bg loop in `web/server.py:_bg_sync_loop` |
| Classify every post                         | `classification` JSON column populated for every post; rules in `taxonomy/*.yaml` |
| Score by escalation priority                | Inbox sorted by Priority; Critical/High/Medium/Low chips with hover-tooltip SLA   |
| ≥ 1 real action per escalated post          | 5 firing channels + auto-reply policy + manual drain — see `social_watch/actions/` |
| ≥ 3 live connectors with read/write         | 6 connectors (Reddit/Twitter read, Slack/Discord/Email/Sheets/Linear write, Twitter R+W) |
| Working dashboard with per-post action triggers | Inbox row's `⋯` menu fires any channel for any post (with idempotency + audit)     |
| Demo of top 5 escalations                   | Inbox `?urgency=critical&window=day`                                              |

---

## Quickstart

```bash
# Python 3.11+ (3.14 tested), uv managed
git clone https://github.com/somudatta06/zomato-social-watch
cd zomato-social-watch

uv venv
uv pip install -r requirements.txt
uv run playwright install chromium      # for Twitter scraper + reply

cp .env.example .env
# Open .env and fill in the credentials you have. Each connector
# gracefully degrades — the dashboard works with zero credentials,
# but channels you don't configure render as "Setup" in the UI.

uv run python main.py serve --port 8000 --reload
# Open http://localhost:8000
```

The first sync runs ~3 seconds after boot. Within a minute you should see real Reddit + X posts about Zomato landing in the Inbox.

### Minimum credentials for a useful demo

You don't need all six connectors. Pick a subset:

- **Twitter cookies** (`TWITTER_COOKIE_AUTH_TOKEN`, `TWITTER_COOKIE_CT0`) — required for Twitter scrape AND for the bidirectional Reply-on-X. Extract from your browser via DevTools → Application → Cookies → `x.com`.
- **Slack incoming webhook** (`SLACK_WEBHOOK_URL`) — 30 seconds at https://api.slack.com/messaging/webhooks. Lets you actually fire alerts.
- **Gemini API key** (`GEMINI_API_KEY`) — free tier from https://aistudio.google.com/apikey. Without it the rules-only classifier still works, just no LLM overlay.

Everything else (`DISCORD_WEBHOOK_URL`, `SMTP_*`, `SHEETS_WEBHOOK_URL`, `LINEAR_API_KEY`, Reddit OAuth) is optional. The Setup pills in the dashboard tell you exactly which env var to set for each disabled feature.

---

## Architecture

Three layers, each one a separate concern:

### 1. Ingest — `social_watch/scrapers/`

- `reddit.py` — public JSON endpoints (`<reddit-url>.json`), no auth. ~50 posts per cycle.
- `twitter.py` — Playwright + saved cookies (twscrape's GraphQL parser is broken against X as of Apr 2026). ~120 tweets per cycle.
- `nitter.py` — graceful fallback retained for if/when Nitter instances come back.

Posts are persisted to SQLite with PRIMARY KEY dedup and watermark-based incremental fetch. Dispatcher runs in the same async loop right after ingest.

### 2. Classify + score — `social_watch/classifier/`, `priority.py`, `playbooks.py`

- **Rules first** for cost: side detection, tripwires, geography, format, lifecycle, clusters, policy. Most posts (≥90%) classify in < 1 ms with no LLM call.
- **Gemini Flash overlay** only on the fuzzy axes (topic, sentiment, tone, urgency). Batched 15–20 posts per call. Free tier (250 RPD + 1M tokens/day) covers all 170 posts per cycle.
- **Priority scoring** combines 8 weighted signals (severity, reach, velocity, SLA proximity, repeat, cross-channel, trust, counter) into a 0–1 score → P0/P1/P2/P3 band.
- **Playbooks** map hard tripwires (death claim, food safety, legal, privacy leak, etc.) to owner team, ack SLA, required steps, and an `auto_reply_blocked` flag.

### 3. Act — `social_watch/actions/` + `social_watch/auto_reply.py`

Six write paths, all behind the same `dispatch_for_post(post_id, channels=...)` interface:

- **Slack / Discord / Email / Sheets / Linear** — webhook-style send-only channels. All idempotent via `posts.action_taken` lock.
- **Twitter reply (`twitter_reply.py`)** — bidirectional. Reuses the Playwright session; replies are templated by audience, capped at 280 chars.
- **Reddit comment (`reddit_comment.py`)** — manual-only. Reddit auto-reply is on the roadmap.

The **auto-reply policy** (`auto_reply.py`) runs in the background sweep. Ten stacked safety guards gate every fire:

1. `AUTO_REPLY_ENABLED=1` env var (default off)
2. Twitter only — Reddit auto-reply intentionally not in scope
3. `classification.auto_action_safe == true` (no tripwire, no abuse, no profanity, no sarcasm)
4. `priority_breakdown.tripwire_override != true`
5. Audience whitelist: `customer-care` or `ops` only
6. 2-minute cooling-off (LLM tripwire pass settles)
7. 1 reply per author per hour
8. 10 fires per cycle cap
9. Drain hard-cap at 200 per operator-run
10. Server-side re-validation at fire time

A misclassified post would have to slip past **all ten** to fire incorrectly. Every fire writes `action_meta.trigger = "auto_reply_v1"` for audit.

The **drain modal** lets an operator clear the overdue backlog in one click. Same eligibility gate, same audit trail, but `trigger = "drain_v1"` and an `operator` field captures who ran it.

---

## Notable behaviours worth scrolling for

- **Playbook lock + force-with-audit.** Try replying to a death-claim post via the inbox `Reply` button. Server returns HTTP 423 Locked. The modal swaps to a panel requiring an approver name, an authorization reason, and a confirmation checkbox. Fire only proceeds if all three are filled, and the bypass is permanently recorded as a purple `BYPASS · @approver` chip on the row + as `action_meta.bypass_approved_by` in the database.
- **Acknowledgment SLA + auto-escalation.** Tripwired posts show a live 5/15/30-min countdown. If nobody acknowledges by the deadline, a background sweeper re-fires Slack/Discord/Email with `[ESCALATED]` prefix and bumps `posts.escalation_count`. Capped at 2 escalations per post (no spam).
- **Post-incident review auto-creation.** 24 hours after the original action fires on a hard-tripwire incident, a sweeper opens a Linear sub-issue with the timeline (channels fired, ack timestamp, escalations, bypass approver if any), the playbook's required-steps as a checklist, and three "FILL IN" sections (Outcome, Root cause, Action items).
- **Restaurant data canonicalization.** Raw Twitter mentions like `@KFC_India`, `@KFCIndia`, `KFC` all collapse to one canonical `KFC` row in the Operations heatmap. Platform self-references (`@zomato`, `@swiggy`), parser garbage (`Zomato.They`), and tier-2/3 city names get filtered before they pollute the leaderboard. See `social_watch/restaurant_canon.py`.
- **Connector status surface.** Home shows a six-dot connector strip; Inbox menu items render `Setup` pills with the exact env var name (`SLACK_WEBHOOK_URL`, `LINEAR_API_KEY + LINEAR_TEAM_ID`, etc.) inline as a monospace subtitle. Operator never has to leave the UI to know what's missing.
- **Per-channel manual fire** + idempotency. Every connector has its own `POST /api/actions/<channel>/<post_id>` endpoint. Re-clicking a post that already fired returns `already_actioned`, never a duplicate.

---

## Repository layout

```
.
├── main.py                       # CLI: serve / sync / test-actions / migrate
├── requirements.txt
├── .env.example                  # Every env var, with setup instructions inline
├── social_watch/
│   ├── scrapers/                 # Reddit + Twitter + Nitter ingest
│   ├── classifier/               # Gemini Flash overlay + Pydantic schemas
│   ├── actions/                  # Slack, Discord, Email, Sheets, Linear,
│   │                             # twitter_reply, reddit_comment, dispatcher
│   ├── playbooks.py              # Incident procedures per hard tripwire
│   ├── auto_reply.py             # Auto-reply policy + drain eligibility gate
│   ├── lifecycle.py              # SLA escalation + post-incident review sweep
│   ├── restaurant_canon.py       # Brand/city normalization framework
│   ├── priority.py               # 8-signal priority scoring
│   ├── preclassifier.py          # Rules-first classification
│   ├── extraction.py             # Restaurants, cities, dishes, order IDs
│   ├── responses.py              # @zomatocare reply detection (Tier 1 + Tier 2)
│   ├── velocity.py               # Engagement-velocity scoring
│   ├── themes.py                 # Theme clustering (24h)
│   ├── clusters.py               # Geographic burst detection
│   ├── briefings.py              # Daily executive briefing generator
│   ├── storage.py                # SQLite schema + migrations
│   ├── config.py                 # Env loading
│   └── web/
│       ├── server.py             # FastAPI routes
│       └── templates/            # Jinja2 — inbox, home, activity, etc.
├── taxonomy/                     # YAML rules for tripwires, audiences, etc.
├── docs/                         # Design docs (see Documentation below)
└── scripts/                      # One-off utilities
```

---

## Documentation

- **`docs/DEMO_SCRIPT.md`** — 5–6 minute walk-through of the running app. Pre-flight checklist, what to say at each section, what to click, brief-checkbox proof points, recording mechanics.
- **`docs/CLASSIFIER_DESIGN.md`** — Phase 2 design lock. Why the 7-layer rules-first approach, how Gemini overlay fits, calibration plan. Pre-implementation; current code follows this design.
- **`docs/CLASSIFICATION_DEEP_DIVE.md`** — The "given 700 tagged posts, which one does the operator open first?" question. Priority-engine derivation, signal weights, edge cases.
- **`docs/SUPABASE_ARCHITECTURE.md`** — Multi-tenant Supabase migration plan. **Not shipped** — this version uses local SQLite. Kept for reference; the schema diagrams + RLS strategy would inform a future production deploy.

---

## Tech stack

- **Python 3.14** (managed via `uv`), **FastAPI**, **Jinja2** templates
- **SQLite** (via `aiosqlite`) for persistence; schema in `social_watch/storage.py`
- **Tailwind CSS** via CDN (no build step) + **Lucide icons**
- **Gemini 2.5 Flash** for classification overlay (free tier)
- **Playwright + Chromium** for Twitter scrape + reply
- **httpx** for everything else (Reddit, Slack, Discord, Sheets, Linear)

No frontend framework. No Redis. No queue. No build tooling. The whole thing runs from one `uv run python main.py serve` command.

---

## What's working / what's next

### ✅ Shipped

- Live Reddit + X ingest, dedup, watermark
- Rules-first classification + Gemini overlay
- 8-signal priority scoring with tripwire override
- 6 connectors with idempotency, audit trail, per-post manual fire
- Playbook system (8 incident classes) with auto-reply lock
- Acknowledgment + live SLA countdown + escalation sweeper
- Post-incident review auto-creation in Linear (24h delay)
- Auto-reply policy with 10 stacked safety guards
- Operator-confirmed drain modal for backlog
- Restaurant + city canonicalization (stoplist + alias dict + parser-garbage filter)
- Activity log distinguishing manual / auto / drain triggers
- Force-with-audit flow for playbook bypass

### 🛣  Roadmap

- **Reddit auto-reply.** OAuth flow plumbing exists; needs an approved Reddit app (Responsible Builder Policy, ~7 day review).
- **LLM-confidence threshold** for auto-fires on high-follower accounts. Currently the rules-only `auto_action_safe` flag is sufficient; would prefer LLM agreement before firing on accounts with >10k followers.
- **Per-assignee Linear routing learning.** `LINEAR_TEAMS` / `LINEAR_ASSIGNEES` are static maps today. Long-term, track which assignee actually closes which ticket type and route to the highest-success closer.
- **Multi-tenant Supabase migration.** Designed in `docs/SUPABASE_ARCHITECTURE.md`. Not started — single-tenant SQLite is sufficient for the take-home.

---

## License

MIT. Use, fork, and learn from any of this. Credentials and DB content are gitignored.

---

Built by [@somudatta06](https://github.com/somudatta06) for the AI-Native Intern take-home (Apr–May 2026).
