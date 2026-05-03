# Supabase Architecture Plan — Social Watch (multi-tenant)

**Version:** 0.1 (architecture lock — pre-implementation)
**Date:** 2026-04-29
**Scope:** Database, auth, RLS, realtime, storage, vector search, workers, edge functions, lifecycle, edge cases.
**Audience:** Engineering, security review, infra, eventual SOC2 audit.

> This document is the **plan only**. No SQL is executed yet. Every table, policy, and worker contract here is reviewable before a single migration runs. Every claim has a falsifiable test or a stated alternative. If a section reads aspirational rather than operational, please flag it.

---

## Table of Contents

1. [Goals & Non-Goals](#1-goals--non-goals)
2. [Multi-Tenancy Model](#2-multi-tenancy-model)
3. [Auth, Identities, Roles](#3-auth-identities-roles)
4. [Schema — Twelve Logical Areas](#4-schema)
5. [Row-Level Security (RLS) Strategy](#5-rls-strategy)
6. [Realtime: Live Dashboard Without Polling](#6-realtime)
7. [Workers vs Edge Functions — Where Each Job Runs](#7-workers-vs-edge-functions)
8. [File Storage (Evidence, Screenshots, Exports)](#8-file-storage)
9. [Vector Search & Semantic Clustering (pgvector)](#9-vector-search)
10. [Edge Cases Catalog (28 cases mapped to mitigations)](#10-edge-cases)
11. [Future Expansion Surfaces](#11-future-expansion)
12. [Migration Plan: SQLite → Supabase](#12-migration-plan)
13. [Cost Model (free tier → enterprise)](#13-cost-model)
14. [Risks & Mitigations](#14-risks)
15. [Implementation Phasing](#15-phasing)

---

## 1. Goals & Non-Goals

### Goals (must satisfy at v1)

| Goal | Falsifiable test |
|---|---|
| Tenants isolated absolutely — Zomato never sees Swiggy data, even on a query bug | RLS test suite that runs `SET LOCAL ROLE` and proves cross-tenant SELECT returns 0 rows |
| Auth-protected dashboard — no anonymous access to a tenant's data | Hitting `/api/posts` without a JWT returns 401 |
| Realtime updates — operator sees new posts within 5 seconds of the worker writing them | E2E test: worker INSERTs, dashboard `useEffect` receives within 5s |
| Bring-your-own-LLM-key — tenant rotates Gemini key without engineering intervention | Settings page edits a row; next classifier run uses the new key |
| Compliance-ready: GDPR/DPDP delete-on-request | Per-author hard-delete cascade tested |
| Cost discipline: free tier supports 5 tenants × 200 posts/day | Postgres + Storage + Realtime usage stays under Supabase free quota |

### Non-goals (deferred to v2+)

- Mobile native apps (web-first)
- White-labeling (custom domain / branding per tenant)
- Custom ML training per tenant (we run shared LLM, taxonomy is per-tenant)
- Multi-region active-active (single-region for v1)
- SSO/SAML (email + Google OAuth for v1)

---

## 2. Multi-Tenancy Model

Three industry patterns:

| Pattern | Pros | Cons | Verdict |
|---|---|---|---|
| **A. Schema-per-tenant** | Strong logical isolation | Migrations N× cost; doesn't scale past ~50 tenants | ❌ |
| **B. Project-per-tenant** | Strongest isolation; per-tenant compute | $25/tenant/month minimum; ops nightmare | ❌ for v1, ✓ as enterprise tier |
| **C. Row-level tenancy + RLS** | Industry standard; scales to thousands of tenants; one schema to migrate | Bug in RLS = data leakage | ✓ — chosen |

### Decision: row-level tenancy

Every tenant-scoped table has `tenant_id uuid not null references tenants(id)`. RLS policies enforce isolation. The `service_role` key (used by workers only, never sent to browsers) bypasses RLS for system operations.

### Tenant compute model

Phased:

- **v1 (Free / Starter)**: shared workers run all tenants in round-robin. One worker process scrapes for all tenants, looking up per-tenant configs from `tenant_settings`.
- **v2 (Pro)**: dedicated worker pods per tenant, scheduled via a queue (e.g., `pg_cron` triggers, or external like Cloud Scheduler).
- **v3 (Enterprise)**: optional pattern B (separate Supabase project) for clients who require physical isolation (e.g., regulated industries).

The schema is the same across all three; only the worker placement changes.

---

## 3. Auth, Identities, Roles

### 3.1 Auth providers

- **Email + password** (Supabase Auth default)
- **Magic link** (low-friction onboarding)
- **Google OAuth** (most B2B users)
- **(Future v2)** Microsoft OAuth, GitHub OAuth, SAML/SSO

Supabase handles the JWT lifecycle. We never store passwords.

### 3.2 User → tenant relationship

A single auth user can belong to many tenants (user might consult for both Zomato and Swiggy). Each membership has a role.

```
auth.users (Supabase managed)
   └── tenant_members
        ├── user_id  (FK → auth.users)
        ├── tenant_id (FK → tenants)
        └── role     (enum)
```

### 3.3 Roles

| Role | Capabilities | Use case |
|---|---|---|
| `owner` | Everything + billing + delete tenant | Founder / admin |
| `admin` | Manage users, edit taxonomies, change settings | Team lead |
| `editor` | Take actions (post reply, escalate, dismiss), edit own annotations | Frontline ops |
| `viewer` | Read-only dashboard, see classifications and reasoning | Stakeholders, exec readers |
| `bot` | Service account for inbound webhooks (e.g., tenant pushes posts in) | Integration accounts |

Role-based predicates are layered onto RLS (e.g., only `editor`+ can `INSERT INTO post_actions`).

### 3.4 Auth edge cases

- **User in 2 tenants, switches**: dashboard reads `current_tenant_id` from a cookie/JWT claim updated on context-switch click. Backend RLS uses `auth.jwt()->>'current_tenant_id'`.
- **User invited but not signed up**: row in `tenant_invites` with email + role; consumed when user creates account with that email.
- **Owner leaves company**: ownership transfer flow; never orphan a tenant.
- **Service account compromise**: `bot` role tokens are scoped, time-bound, rotatable; logged in `audit_log` separately.

---

## 4. Schema

Twelve logical areas. Each table includes: purpose, columns, indexes, rationale.

### 4.1 Tenancy

#### `tenants`
The top-level customer.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk default uuid_generate_v4()` |  |
| `slug` | `text unique not null` | url-safe id (`zomato`, `swiggy`) |
| `name` | `text not null` | display |
| `brand_keywords` | `text[]` | the brand-name variants the scraper looks for |
| `plan` | `text not null default 'free'` | `free / starter / pro / enterprise` |
| `status` | `text not null default 'active'` | `active / paused / suspended` |
| `default_timezone` | `text default 'UTC'` |  |
| `default_locale` | `text default 'en'` |  |
| `created_at` | `timestamptz default now()` |  |
| `created_by` | `uuid references auth.users` |  |
| `metadata` | `jsonb default '{}'` | escape hatch for per-tenant config that doesn't deserve its own column yet |

#### `tenant_members`
User ↔ tenant link with role.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` |  |
| `tenant_id` | `uuid fk` |  |
| `user_id` | `uuid fk references auth.users` |  |
| `role` | `text` | enum check |
| `invited_by` | `uuid fk auth.users` |  |
| `joined_at` | `timestamptz` |  |
| `last_active_at` | `timestamptz` | for inactive-user reminders |
| Unique `(tenant_id, user_id)` |  |  |

#### `tenant_invites`
Pending invites by email.

| Column | Type |
|---|---|
| `id`, `tenant_id`, `email`, `role`, `invited_by`, `token`, `expires_at`, `accepted_at` |  |

#### `tenant_settings`
Single-row-per-tenant config.

| Column | Type | Notes |
|---|---|---|
| `tenant_id` | `uuid pk` |  |
| `refresh_interval_seconds` | `int default 300` | scrape cadence |
| `default_llm_provider` | `text default 'gemini'` |  |
| `default_llm_model` | `text default 'gemini-2.5-flash'` |  |
| `gemini_api_key_encrypted` | `text` | column-level encrypted via pgcrypto |
| `anthropic_api_key_encrypted` | `text` |  |
| `auto_action_enabled` | `boolean default false` | global kill switch for automated replies |
| `auto_action_safe_threshold` | `numeric default 0.85` | confidence threshold |
| `data_retention_days` | `int default 365` | when to soft-delete |
| `taxonomy_version` | `int default 1` | bumps when taxonomy is edited |
| `created_at`, `updated_at` |  |  |

### 4.2 Source data (posts)

#### `posts` ⭐ (largest table)

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk default uuid_generate_v4()` |  |
| `tenant_id` | `uuid fk not null` |  |
| `source` | `text not null` | `reddit / twitter / instagram / youtube / news / rss` |
| `native_id` | `text not null` | upstream id |
| `author_handle` | `text` |  |
| `author_id` | `text` | upstream id (immune to handle changes) |
| `content` | `text not null` |  |
| `content_lang` | `text` | detected language |
| `url` | `text` |  |
| `created_at` | `timestamptz not null` | when the post was made |
| `fetched_at` | `timestamptz default now()` |  |
| `metadata` | `jsonb default '{}'` | source-specific |
| `tenant_metadata` | `jsonb default '{}'` | per-tenant tags, e.g., campaign_id |
| Unique `(tenant_id, source, native_id)` | dedup |  |
| Index on `(tenant_id, created_at desc)` | listing |  |
| Index on `(tenant_id, source, fetched_at)` | watermark queries |  |
| Index on `(tenant_id, author_handle)` | handle history |  |
| **GIN** `(content gin_trgm_ops)` | full-text search via pg_trgm |  |

> Partitioning at scale: when one tenant exceeds ~10M posts, declarative range-partition `posts` by month on `created_at`. v1 doesn't need this; v2 does.

#### `watermarks`
Per-tenant per-query incremental scraping cursors.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` |  |
| `tenant_id` | `uuid fk` |  |
| `source_query` | `text not null` | e.g., `reddit:r/india`, `twitter:zomato` |
| `last_native_id` | `text` |  |
| `last_created_at` | `timestamptz` |  |
| `last_run_at` | `timestamptz` |  |
| Unique `(tenant_id, source_query)` |  |  |

#### `fetch_runs`
Per-cycle observability.

| Column | Type |
|---|---|
| `id`, `tenant_id`, `source`, `query`, `started_at`, `finished_at`, `posts_seen`, `posts_new`, `error`, `worker_id`, `cycle_id` |

`worker_id` lets us reconcile when multiple workers run; `cycle_id` ties together all queries from one cycle.

### 4.3 Classification

#### `post_classifications`
1:1 with `posts` for the current classification, but kept in a separate table so we can re-classify without losing history.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` |  |
| `post_id` | `uuid fk not null` | one row per (post_id, version) |
| `tenant_id` | `uuid fk` | denormalized for RLS speed |
| `version` | `int default 1` | bumped on re-classification |
| `is_current` | `boolean default true` | only one TRUE per post |
| `method` | `text` | `rules-only / llm-gemini / llm-claude / hybrid` |
| `model_id` | `text` | exact model version |
| `side` | `text` | `consumer / merchant / both / neither` |
| `primary_topic_id` | `text` | references `categories.id` |
| `secondary_topic_ids` | `text[]` |  |
| `tripwires_fired` | `text[]` |  |
| `sentiment` | `text` |  |
| `tone_flags` | `text[]` |  |
| `urgency` | `text` |  |
| `urgency_score` | `numeric` |  |
| `audience` | `text[]` |  |
| `author_role` | `text` |  |
| `author_influence` | `text` |  |
| `geography` | `text` |  |
| `format` | `text` |  |
| `lifecycle` | `text` |  |
| `confidence` | `numeric` |  |
| `needs_human_review` | `boolean` |  |
| `auto_action_safe` | `boolean` |  |
| `reasoning` | `text` |  |
| `sub_claims` | `jsonb default '[]'` | array of sub-claim objects |
| `cluster_id` | `uuid fk clusters(id)` | nullable |
| `cluster_role` | `text` | `lead / member / outlier` |
| `classified_at` | `timestamptz default now()` |  |
| `llm_run_id` | `uuid fk llm_runs(id)` | for cost tracing |
| Unique `(post_id, version)` |  |  |
| Partial unique `(post_id) where is_current = true` |  |  |
| Index `(tenant_id, urgency, classified_at desc)` |  |  |

Why a separate table: re-classifications on taxonomy change preserve history without dropping the audit trail. Diffing v1 → v2 reveals classifier drift.

#### `handles`
Per-tenant author profile cache.

| Column | Type |
|---|---|
| `id`, `tenant_id`, `source`, `handle`, `display_name`, `bio`, `account_created_at`, `followers_count`, `verified`, `profile_class`, `trust_score`, `watchlist`, `notes`, `first_seen_at`, `last_seen_at`, `total_posts`, `prior_complaints`, `prior_praise`, `resolved_count`, `unresolved_count`, `sentiment_30d_avg` |

Unique `(tenant_id, source, handle)`.

#### `clusters`
Detected event clusters.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` |  |
| `tenant_id` | `uuid fk` |  |
| `primary_topic_id` | `text` |  |
| `geography` | `text` |  |
| `started_at` | `timestamptz` |  |
| `last_member_at` | `timestamptz` |  |
| `member_count` | `int` |  |
| `lead_post_id` | `uuid fk posts` |  |
| `summary` | `text` | LLM-generated synopsis |
| `closed_at` | `timestamptz` | when cluster goes dormant |

#### `cluster_members`
Junction.

| Column |
|---|
| `cluster_id`, `post_id`, `role`, `joined_at` |

### 4.4 Routing & actions

#### `post_actions`
Each action taken on a post (record-of-truth for the action history).

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` |  |
| `tenant_id` | `uuid fk` |  |
| `post_id` | `uuid fk` |  |
| `action_type` | `text` | `reply_public / reply_dm / escalate_slack / escalate_email / escalate_sheet / escalate_linear / amplify_like / amplify_retweet / track_only / dismiss` |
| `channel_id` | `uuid fk notification_channels` | nullable |
| `payload` | `jsonb` | the actual content sent |
| `requested_by` | `uuid fk auth.users` | who clicked the button |
| `requested_at` | `timestamptz default now()` |  |
| `executed_at` | `timestamptz` | when it actually fired |
| `status` | `text` | `pending / sent / failed / cancelled` |
| `external_id` | `text` | id returned by the external system (e.g., tweet id, slack ts) |
| `external_url` | `text` |  |
| `error` | `text` |  |
| `parent_action_id` | `uuid fk post_actions(id)` | for follow-ups in a thread |

Index `(tenant_id, post_id, requested_at desc)`.

#### `notification_channels`
Per-tenant Slack/email/sheet/Linear configurations.

| Column | Type | Notes |
|---|---|---|
| `id`, `tenant_id`, `name`, `type` (`slack_webhook`, `email`, `gsheet`, `linear`, `jira`), `config_encrypted`, `is_active`, `created_by`, `created_at` |  |

`config_encrypted` holds the webhook URL / API key encrypted column-level.

#### `escalation_rules`
Per-tenant overrides on default routing.

| Column | Type |
|---|---|
| `id`, `tenant_id`, `name`, `condition` (jsonb expression: e.g., `{topic_starts_with: 'safety', author_influence: 'tier-1'}`), `action_type`, `channel_id`, `priority`, `enabled` |

Engine evaluates rules in `priority` order; first match wins.

#### `review_queue`
Posts flagged for human review (low confidence, sensitive topics, tripwires).

| Column | Type |
|---|---|
| `id`, `tenant_id`, `post_id`, `reason`, `priority`, `assigned_to`, `status` (`pending / in_review / decided / dismissed`), `decision`, `decided_by`, `decided_at`, `notes` |

#### `annotations`
Operator notes on posts.

| Column | Type |
|---|---|
| `id`, `tenant_id`, `post_id`, `author_id`, `body`, `created_at`, `pinned` |

#### `legal_holds`
Embargoes on engagement (legal puts a hold on a post pending litigation).

| Column | Type |
|---|---|
| `id`, `tenant_id`, `post_id`, `reason`, `placed_by`, `placed_at`, `released_by`, `released_at` |

While a hold is active, all auto-actions are blocked even if other gates pass.

### 4.5 Configuration (data-driven taxonomy)

#### `taxonomies`
Versioned per-tenant taxonomies.

| Column | Type |
|---|---|
| `id`, `tenant_id`, `version`, `is_active`, `notes`, `created_by`, `created_at` |

Edits create a new version; one is `is_active` at a time.

#### `categories`
Hierarchical leaves and parents.

| Column | Type |
|---|---|
| `id` (`text` for human-readable IDs like `consumer.delivery.late`), `taxonomy_id`, `parent_id`, `side`, `name`, `description`, `default_audience`, `default_urgency`, `urgency_modifier`, `severity_rubric` (jsonb), `action_hint`, `requires_human_review`, `sensitivity_flags`, `active` |

#### `category_examples`
Few-shot examples per leaf for LLM classification.

| Column | Type |
|---|---|
| `id`, `category_id`, `taxonomy_id`, `text`, `created_by`, `created_at`, `usage_count` |

When a reviewer corrects a misclassification, the corrected post text becomes a new example here. Closing the feedback loop.

#### `tripwires`
Per-tenant tripwire rules.

| Column | Type |
|---|---|
| `id`, `tenant_id`, `name`, `rationale`, `detection` (jsonb: keywords, keyword_pairs, regex, handles, handle_bio), `override` (jsonb: urgency, audience, sensitivity_flags, action_lock), `active`, `priority`, `precision`, `recall` (these last two are tracked metrics, updated by an audit job) |

#### `cross_cuts`
Cross-cut axes. Same shape as the YAML, stored as JSONB on a single row per tenant — tenant-customizable but rarely edited.

### 4.6 LLM observability

#### `llm_runs`
Every LLM call is logged for cost, debug, and quality tracing.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` |  |
| `tenant_id` | `uuid fk` |  |
| `provider` | `text` | `gemini / anthropic / openai` |
| `model` | `text` |  |
| `purpose` | `text` | `classification / reply_draft / embedding / summary / cluster_summary` |
| `input_tokens` | `int` |  |
| `output_tokens` | `int` |  |
| `cached_tokens` | `int` | for context cache (Gemini) / prompt cache (Anthropic) |
| `cost_usd` | `numeric(10,6)` |  |
| `latency_ms` | `int` |  |
| `succeeded` | `boolean` |  |
| `error_class` | `text` |  |
| `posts_processed` | `int` | for batched calls |
| `started_at` | `timestamptz` |  |
| `finished_at` | `timestamptz` |  |
| `request_payload_hash` | `text` | sha256 of inputs (for dedup / replay) |

Index `(tenant_id, started_at desc)` for billing and observability dashboards.

### 4.7 Vector / embeddings

#### `post_embeddings`
pgvector for semantic search and clustering.

| Column | Type |
|---|---|
| `post_id` (pk, fk), `tenant_id`, `model` (`text-embedding-004` etc.), `embedding` (`vector(768)`), `created_at` |

`HNSW` or `ivfflat` index on `(tenant_id, embedding vector_cosine_ops)` so searches are tenant-scoped + fast.

### 4.8 Storage / files

Supabase Storage handles uploaded files. Metadata in:

#### `attachments`

| Column | Type |
|---|---|
| `id`, `tenant_id`, `post_id` (nullable), `bucket`, `path`, `mime_type`, `size_bytes`, `uploaded_by`, `uploaded_at`, `purpose` (`evidence / screenshot / export`) |

Storage bucket policy mirrors RLS: `tenant/{tenant_id}/...` paths, server-signed URLs for downloads.

### 4.9 Subscriptions / billing

#### `tenant_plans`

| Column | Type |
|---|---|
| `tenant_id`, `plan`, `started_at`, `ended_at`, `monthly_post_quota`, `monthly_llm_token_quota`, `seat_count`, `billing_email` |

#### `usage_records`
Aggregated daily.

| Column | Type |
|---|---|
| `tenant_id`, `date`, `posts_scraped`, `posts_classified`, `llm_input_tokens`, `llm_output_tokens`, `llm_cost_usd`, `actions_taken`, `seats_active` |

### 4.10 Audit / compliance

#### `audit_log`
**Append-only** record of every privileged action.

| Column | Type | Notes |
|---|---|---|
| `id`, `tenant_id`, `actor_id` (`uuid fk auth.users` or `null` if system), `actor_type` (`user / bot / system`), `action`, `target_type`, `target_id`, `before` (jsonb), `after` (jsonb), `ip_address`, `user_agent`, `at` (timestamptz default now) |

DDL: `INSERT`-only via RLS (no UPDATE/DELETE allowed by anyone, even owners).

#### `data_subject_requests`
GDPR/DPDP requests.

| Column | Type |
|---|---|
| `id`, `tenant_id`, `subject_handle`, `subject_email`, `request_type` (`access / deletion / correction / portability`), `status`, `received_at`, `completed_at`, `notes` |

When a deletion request is fulfilled, the cascade hard-deletes posts, classifications, embeddings, attachments tied to that handle.

### 4.11 Webhook integrations

#### `webhooks_outbound`
Per-tenant outbound webhooks (we notify them when something happens).

| Column | Type |
|---|---|
| `id`, `tenant_id`, `event_types` (text[]), `url`, `secret_encrypted`, `active`, `created_at`, `last_success_at`, `last_failure_at`, `failure_count` |

#### `webhooks_inbound`
Inbound webhook tokens (tenant pushes posts INTO us).

| Column | Type |
|---|---|
| `id`, `tenant_id`, `name`, `token_hash`, `bot_user_id`, `created_at`, `revoked_at` |

### 4.12 System health

#### `system_locks`
Worker leases (so two workers don't process the same cycle).

| Column |
|---|
| `name`, `holder`, `acquired_at`, `expires_at` |

#### `feature_flags`

| Column |
|---|
| `tenant_id` (nullable for global), `flag`, `enabled`, `value` (jsonb) |

---

## 5. RLS Strategy

### 5.1 Universal pattern

```sql
-- Helper function: returns the tenants the current user belongs to
CREATE OR REPLACE FUNCTION user_tenant_ids()
RETURNS uuid[] LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT array_agg(tenant_id) FROM tenant_members WHERE user_id = auth.uid()
$$;

-- Standard SELECT/INSERT/UPDATE/DELETE policy
CREATE POLICY "tenant_isolation_select" ON posts
  FOR SELECT USING (tenant_id = ANY(user_tenant_ids()));
```

### 5.2 Role-gated policies

```sql
-- Only editor+ can create actions
CREATE POLICY "actions_editor_or_higher" ON post_actions
  FOR INSERT WITH CHECK (
    tenant_id = ANY(user_tenant_ids())
    AND EXISTS (
      SELECT 1 FROM tenant_members
      WHERE user_id = auth.uid() AND tenant_id = post_actions.tenant_id
        AND role IN ('owner','admin','editor')
    )
  );
```

### 5.3 Audit log: append-only enforced via RLS

```sql
CREATE POLICY "audit_insert_only" ON audit_log
  FOR INSERT WITH CHECK (tenant_id = ANY(user_tenant_ids()));
-- No SELECT/UPDATE/DELETE policies → users cannot read others' rows
-- Owners can read via a separate read-only audit policy:
CREATE POLICY "audit_read_owner" ON audit_log
  FOR SELECT USING (
    tenant_id = ANY(user_tenant_ids())
    AND EXISTS (SELECT 1 FROM tenant_members
                WHERE user_id = auth.uid() AND tenant_id = audit_log.tenant_id
                  AND role IN ('owner','admin'))
  );
```

### 5.4 Workers bypass RLS

Workers connect with the `service_role` key (bypasses RLS by Supabase design). The key never leaves the worker host. Browsers always use `anon` key + user JWT.

### 5.5 RLS test suite

A separate test database with two seed tenants and crafted users. Tests:
- User A reads → only sees Tenant A rows
- User A INSERTs post with `tenant_id = TenantB` → rejected
- User A reads `audit_log` → only own tenant
- Service role reads → sees all (verify worker can do its job)
- Anonymous user → all SELECTs return 0

This suite runs in CI. Any RLS regression blocks merge.

---

## 6. Realtime

Supabase Realtime (built on Postgres logical replication) lets the dashboard subscribe to live changes without polling.

### 6.1 Channels per tenant

The dashboard subscribes to:
```
posts:tenant_id=eq.<tenant_id>
post_classifications:tenant_id=eq.<tenant_id>
post_actions:tenant_id=eq.<tenant_id>
review_queue:tenant_id=eq.<tenant_id>
fetch_runs:tenant_id=eq.<tenant_id>
```

When a worker INSERTs a post → all currently-open dashboards for that tenant get a `INSERT` event. Frontend prepends to the post list.

### 6.2 Auth on Realtime

JWT is sent to Realtime on connection; RLS applies to the subscription. A Swiggy user cannot subscribe to Zomato's channel.

### 6.3 Broadcast for ephemeral signals

Supabase Realtime also supports `broadcast` (not DB-backed) for ephemeral signals:
- "User X is viewing post Y" (presence)
- "Sync started" / "Sync finished" (system pings, faster than fetch_runs polling)

### 6.4 Edge case: high-volume tenant

A tenant ingesting 1000 posts/min will spam the channel. Mitigation: per-tenant rate limit on Realtime broadcasts; UI batches updates with `requestAnimationFrame` + 200ms throttle.

---

## 7. Workers vs Edge Functions

Two execution surfaces, very different constraints.

| Concern | Edge Functions (Deno) | Worker pods (Python) |
|---|---|---|
| Time limit | 25s default; 150s max on paid | Unlimited |
| Cold starts | Yes (~hundreds of ms) | No (long-running) |
| Browser/Playwright | No | Yes |
| pip / Python ecosystem | No | Yes |
| Cost | Per-invocation, generous free tier | Per-hour (separate hosting) |
| Use cases | Webhook receivers, action dispatchers, REST shims | Scrapers, LLM batch classifiers, embedding jobs |

### 7.1 What runs as Edge Function

- `POST /webhook/inbound/{token}` — tenant pushes a post into our system
- `POST /webhook/slack-action` — Slack interactive-message callback
- `POST /actions/dispatch` — pulls `post_actions` rows where status='pending', fires Slack/email/sheet/Linear
- `GET /api/posts` — for browser fetches that need RLS + cookies (otherwise dashboard hits Supabase REST directly)

### 7.2 What runs as worker

- **Scraper**: Python + Playwright + httpx. Runs on Modal / Railway / Fly / Render. Cron loop per tenant. Writes to Postgres via `service_role`.
- **Classifier**: Python + Gemini SDK. Reads new posts (where no classification yet), batches 15–20, calls Gemini, writes back.
- **Embedder**: same loop, writes vectors to `post_embeddings`.
- **Cluster job**: SQL-heavy + LLM cluster summaries. Runs every 5 min.
- **Action dispatcher**: alternative to Edge Function for actions if we need long-running jobs (e.g., post 100 replies sequentially with rate limits).
- **Audit retention job**: weekly. Hard-deletes posts past `tenant_settings.data_retention_days`.

### 7.3 Worker → DB safety

- Workers use `service_role` (bypasses RLS) but ALWAYS scope queries by `tenant_id`. Linter rule in CI: any DB call without `tenant_id` filter is a CI failure.
- Workers acquire a lease via `system_locks` before processing a tenant — prevents double-processing.

---

## 8. File Storage

Supabase Storage = S3-compatible. Three buckets:

| Bucket | Purpose | RLS |
|---|---|---|
| `tenant-evidence` | Screenshots, photos attached by reviewers | per-tenant prefix: `{tenant_id}/...` |
| `tenant-exports` | Generated CSV/XLSX exports for reports | tenant prefix; signed URLs only |
| `tenant-uploads` | User-uploaded reply drafts (e.g., a video reply) | tenant prefix |

Edge cases:
- Tenant deletion: cascade delete all bucket objects in their prefix
- File >50MB: resumable upload via tus protocol (Supabase supports)
- Sensitive content (food poisoning evidence): time-bound signed URLs only, not public

---

## 9. Vector Search

`pgvector` extension in Postgres. Two use cases:

### 9.1 Semantic search

> "Show me all posts similar to this one about delivery agent identity issues."

```sql
SELECT p.*
FROM posts p
JOIN post_embeddings e ON e.post_id = p.id
WHERE p.tenant_id = $1
ORDER BY e.embedding <=> $2  -- cosine distance
LIMIT 20;
```

### 9.2 Cluster detection assist

After topic-based clustering, we use embeddings to dedup near-identical posts (copy-paste campaigns) and to surface OUTLIERS within a cluster (someone defending Zomato in the middle of a complaint storm — Layer 5 counter-narrative).

### 9.3 Embedding strategy

- **Provider**: Gemini `text-embedding-004` (768 dim, free tier covers normal volume) or self-hosted `bge-small-en-v1.5` (cheaper at scale, multilingual).
- **What we embed**: post `content` (truncated to first 500 tokens). Re-embed on content edit (rare).
- **When**: async after classification finishes; non-blocking.
- **Index**: HNSW on `(tenant_id, embedding)` — tenant-scoped from the start.

---

## 10. Edge Cases

Twenty-eight edge cases, each mapped to its mitigation.

| # | Edge case | Mitigation |
|---|---|---|
| 1 | Same post discovered from two scrapers (cross-post) | Dedup by content+author hash in addition to (source, native_id) |
| 2 | Tenant deletes a category | Soft delete (`active=false`); existing posts keep classification; UI shows "deprecated" |
| 3 | Tenant rebrands (Zomato → Eternal) | `tenants.brand_keywords` is `text[]`; just append the new variant |
| 4 | Bulk historical import (CSV upload) | Dedicated import endpoint; rate-limited; runs in worker, not synchronously |
| 5 | GDPR/DPDP delete request | `data_subject_requests` row → trigger fires → cascade hard-delete by handle |
| 6 | RLS bug leaks tenant data | Test suite blocks merge; quarterly external pen test |
| 7 | Two workers race on same cycle | `system_locks` row with TTL; second worker waits or skips |
| 8 | LLM cost overrun | Per-tenant `monthly_llm_token_quota`; hard stop at 100%; warn at 80% |
| 9 | Soft delete vs hard delete | All tables have `deleted_at timestamptz`; hard-delete only via `data_subject_requests` or retention worker |
| 10 | Time zones | Always UTC in DB; render in `tenant_settings.default_timezone` |
| 11 | High-volume tenant | Range-partition `posts` by month past 10M rows |
| 12 | Cluster spans tenants (viral incident) | Each tenant only sees their slice of cluster; no cross-tenant aggregation in v1 |
| 13 | LLM proposes invalid category | Classifier validates against `categories.id`; falls back to parent on no-match |
| 14 | Schema migration without downtime | Online migrations only (add columns, never DROP/RENAME without alias); use `pg_repack` for table rewrites |
| 15 | Backup / DR | Supabase PITR (Point-in-Time Recovery, 7-day default); daily logical dumps off-site |
| 16 | User in 2 tenants switches | `current_tenant_id` JWT claim updated on switch; backend reads it |
| 17 | Service account compromise | `bot` tokens scoped + time-bound + revocable; logged separately in audit |
| 18 | Gemini key compromise | Per-tenant key in `tenant_settings.gemini_api_key_encrypted`; rotate via UI; old key invalidated |
| 19 | Regional language post | Multilingual embeddings; per-language YAML examples; LLM is multilingual already |
| 20 | Cold storage for old data | Posts >365 days move to `posts_archive` partition; cheaper storage tier |
| 21 | Embargoed posts (legal hold) | `legal_holds` row blocks `auto_action_safe = true` regardless of other gates |
| 22 | Pinned manual escalations | `annotations.pinned = true`; surfaces in a "Pinned" tab independent of classification |
| 23 | Editor's annotations on a post | `annotations` table; visible to all tenant members |
| 24 | Re-classification on taxonomy edit | `post_classifications.version` bumps; `is_current` flips; old version preserved |
| 25 | Inbound webhook from tenant's CRM | `webhooks_inbound` token-auth'd; bot user creates posts |
| 26 | Outbound webhook to tenant's systems | `webhooks_outbound`; retries with exponential backoff |
| 27 | Slack interactive callback | Edge Function verifies signature, looks up the post, writes `post_actions` row |
| 28 | Dashboard offline / Realtime disconnects | UI falls back to 30s polling; auto-reconnects |

---

## 11. Future Expansion Surfaces

Designed-in extension points that don't require schema migrations:

| Surface | Where it plugs in |
|---|---|
| New social platform (Instagram, YouTube) | Add `source` enum value; new scraper module reuses `posts` schema |
| New LLM provider | `tenant_settings.default_llm_provider`; classifier module dispatches |
| Auto-drafted replies | New `reply_drafts` table 1:N from posts; existing `post_actions` consumes |
| Sentiment per sub-claim | Already in `sub_claims` jsonb |
| White-labeling | `tenants.brand_settings jsonb` (logo, colors, custom domain) |
| Mobile app | Same Supabase APIs; just a different client |
| SAML/SSO | Supabase Enterprise add-on; no schema change |
| Custom per-tenant ML models | `tenant_settings.classifier_model_id`; classifier dispatches to in-house endpoint |
| Multi-region deployment | Read replicas per region; Supabase EE; logical replication |
| Inbound webhooks from CRM | `webhooks_inbound` already specced |
| Predictive virality model | New `post_predictions` table; doesn't disturb classifications |
| Cross-platform handle linking | New `handle_links` table joining `(source_a, handle_a) ↔ (source_b, handle_b)` |
| Image/video classification | `attachments.purpose='content'` + new `attachment_classifications` table |
| Tenant analytics dashboard | Materialized views per tenant, refreshed nightly |

The schema is built so each of these is *additive* — no breaking changes required.

---

## 12. Migration Plan: SQLite → Supabase

### Step 1 — Supabase project provision
- Create project (free tier) with Postgres 16
- Enable extensions: `pgcrypto`, `uuid-ossp`, `pg_trgm`, `vector`
- Set up Auth (email + Google)
- Configure Storage buckets

### Step 2 — Schema migration
- Use `supabase/migrations/*.sql` directory (versioned)
- Run via Supabase CLI: `supabase db push`
- 0001_initial.sql: tenancy + posts + watermarks
- 0002_classifications.sql: classifications, handles, clusters, embeddings
- 0003_actions.sql: post_actions, channels, escalation_rules
- 0004_config.sql: taxonomies, categories, tripwires
- 0005_audit.sql: audit_log + RLS policies
- 0006_realtime.sql: enable replication on posts, classifications, actions, review_queue

### Step 3 — Data migration
- Create a `migration_zomato.py` script:
  - Connect to local SQLite + Supabase Postgres
  - Create tenant `zomato`
  - Bulk INSERT posts → `posts` (with `tenant_id = zomato.id`)
  - Re-classify or copy classifications → `post_classifications`
  - Move watermarks → `watermarks`
  - Move fetch_runs → `fetch_runs`
- Idempotent: re-runnable safely

### Step 4 — Code switchover
- Replace `aiosqlite` connections with `asyncpg` (or `supabase-py`)
- Add `tenant_id` parameter through the call chain
- Update workers to use `service_role` key
- Update dashboard to use Supabase JS client + RLS-protected queries

### Step 5 — Cutover
- Run both stores in parallel for 24h (dual-write)
- Compare counts and key fields
- Flip dashboard read source to Supabase
- Decommission SQLite (keep as backup for 30 days)

Estimated effort: 3–5 engineering days for a focused engineer.

---

## 13. Cost Model

### Supabase tiers

| Tier | Cost | Capacity | Fits us? |
|---|---|---|---|
| Free | $0 | 500MB DB, 5GB egress, 50K MAUs, 2GB Storage | ✅ for MVP / 1–3 tenants × 200 posts/day |
| Pro | $25/month | 8GB DB, 250GB egress, 100K MAUs, 100GB Storage, daily backups, PITR | ✅ for 10–50 tenants |
| Team | $599/month | Compute add-on, more bandwidth, SSO | ✅ for 50+ tenants or enterprise SLAs |
| Enterprise | quote | Dedicated, multi-region, premium support | enterprise tier of our own product |

### Per-tenant variable costs (at our level)

- **LLM tokens**: ~200 posts/day × 500 input + 400 output @ Gemini Flash ≈ $0.001/post × 200 = **$0.20/day = $6/month**
- **Storage**: minimal in v1
- **Worker compute**: shared free tier on Modal/Railway covers 5+ tenants
- **Egress**: dashboard usage well under free tier per tenant

**Total at MVP (5 tenants):** ~$0 fixed + ~$30/month variable on Supabase free + ~$30/month LLM ≈ **$60/month total**.

---

## 14. Risks & Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| RLS misconfiguration leaks data | Critical | Mandatory test suite, quarterly external pen test, code review checklist |
| Worker crash kills scraping for all tenants | High | Per-tenant worker lease; one tenant's failure doesn't block others |
| LLM cost overrun on a runaway tenant | High | Per-tenant token quotas, hard stop at 100% |
| Supabase outage | Medium | Realtime falls back to polling; scraper queues writes locally and replays |
| pgvector index bloat at scale | Medium | Periodic `REINDEX`; consider switching to dedicated vector DB (Qdrant) at scale |
| Schema drift between dev and prod | Medium | All changes via versioned migrations only; CI runs full migration on PR |
| Tenant requests data export but we're behind on processing | Medium | `data_subject_requests` includes SLA tracking; warn at 25 days, escalate at 28 |
| Compliance audit fails because audit_log is gappy | Medium | RLS forbids DELETE on audit_log; trigger on every privileged table writes audit row |
| API key compromise blast radius | High | Per-tenant keys, encrypted at rest, scoped service tokens with short TTLs |
| Realtime channel pollution at scale | Medium | Per-tenant rate limits; UI throttles render |

---

## 15. Implementation Phasing

### Phase A — Foundation (1–2 days)
- Provision Supabase project
- Migrations 0001–0005 (schema only, no data)
- RLS policy v0 + test suite
- Migration script SQLite → Postgres for current Zomato tenant only
- Workers updated to write to Postgres via service_role
- Dashboard uses Supabase JS client + Auth (email+Google)

### Phase B — Real-time & multi-tenant (1–2 days)
- Migration 0006 (Realtime publications)
- Realtime subscriptions in dashboard
- Tenant onboarding flow (sign up → create tenant → invite teammates)
- Tenant settings page (LLM keys, refresh interval)

### Phase C — Configuration UI (2–3 days)
- Per-tenant taxonomy editor (replaces YAML editing)
- Per-tenant tripwire editor
- Per-tenant escalation rule builder
- Notification channel setup (Slack webhook, email, sheet)

### Phase D — Actions & integrations (2–3 days)
- Edge Function for action dispatcher (Slack, email, sheet)
- Slack interactive-message callbacks
- Webhook in/out
- Audit log viewer

### Phase E — Polish, scale prep (1–2 days)
- Embeddings + semantic search
- Cluster summaries via LLM
- Usage dashboard per tenant
- Billing integration (Stripe + tenant_plans)

Total: ~8–12 engineering days for a complete, multi-tenant production system.

---

*End of architecture plan. Code that implements this design will live in `supabase/migrations/`, `social_watch/storage/postgres.py`, and `social_watch/web/auth.py`. Every code path must be traceable to a decision in Sections 4–9; every table to a rationale in Section 4.*
