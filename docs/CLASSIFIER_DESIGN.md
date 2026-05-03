# Zomato Social Watch — Classifier Design Document

**Version:** 0.1 (initial design lock)
**Date:** 2026-04-29
**Scope:** Phase 2 of Social Watch — classification only. Phases 3 (actions) and 4 (dashboard) consume this layer's output.
**Audience:** Engineering, social-team leadership, classification reviewers, interviewer panel.

> **Status (May 2026):** Design is shipped largely as-described in `social_watch/preclassifier.py` and `social_watch/classifier/`. The 7-layer rules-first approach + Gemini Flash overlay is live. Phase 3 (actions: Slack/Discord/Email/Sheets/Linear/Twitter-reply/Reddit-comment, plus the auto-reply policy and operator drain modal) and Phase 4 (dashboard with Inbox / Activity / Operations / Discovery / Briefing) have also shipped. See the root [`README.md`](../../README.md) for current capabilities and [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md) for a walk-through of the running system. This document is preserved unchanged as the design rationale — treat it as the *why*, not the current spec.

> This document records the *why* behind every classification choice. It is intentionally falsifiable: every claim either has a measurable test, a stated alternative we rejected, or a refinement path that lets a future operator change our minds with data. If a section reads like marketing rather than engineering, that is a defect — please flag it.

---

## Table of Contents

1. [First Principles — what this classifier is FOR](#1-first-principles)
2. [Ten Architectural Decisions, Defended](#2-ten-architectural-decisions)
3. [The Taxonomies — Consumer and Merchant](#3-the-taxonomies)
4. [Cross-cut Axes](#4-cross-cut-axes)
5. [Tripwire Catalog](#5-tripwire-catalog)
6. [The Seven-Layer Pipeline](#6-the-seven-layer-pipeline)
7. [Interview Defense — 12 Likely Questions](#7-interview-defense)
8. [The Refinement Protocol](#8-the-refinement-protocol)
9. [Metrics & Success Definition](#9-metrics-success-definition)
10. [Out of Scope; Future Work](#10-out-of-scope-future-work)
11. [USP Statement](#11-usp-statement)

---

## 1. First Principles

Before any technical decision, the question every choice is held against:

| Goal | Why it matters | Failure mode if missed |
|---|---|---|
| **Reduce cognitive load** | The Zomato social team can't read 1,000+ daily posts. The classifier turns that volume into ~30 a human must look at. | Team burns out; real signals lost in noise. |
| **Drive correct routing** | Right team, right urgency, right channel. Engineering shouldn't read PR drama; PR shouldn't read app crashes. | Issues pile up in wrong queues; cross-team trust erodes. |
| **Enable measurable outcomes** | "We responded to 87% of complaints in <30 min" is only meaningful if classification is consistent and category-stable. | Reporting becomes vibes; SLAs cannot be enforced. |
| **Build institutional learning** | Patterns surfaced over months become Zomato's product roadmap input. ("Top complaint leaf this quarter: agent identity mismatch → fix verification.") | Same issues recur each quarter; no compounding insight. |
| **Be defensible to legal, leadership, customers** | Every action this system takes must be explainable when challenged — internally, by a regulator, or in court. | Lawsuits, brand damage, regulator inquiries with no audit trail. |

**The test we apply to every decision below:** does it serve at least one of these five goals? If not, it is overhead and should be removed.

---

## 2. Ten Architectural Decisions

Each decision is documented in a fixed format:

```
DECISION:           what we did
WHY:                the principle
ALTERNATIVE REJECTED: the simpler/standard approach we considered
TRADE-OFF ACCEPTED:   what we gave up
SUCCESS METRIC:       how we know this is working
REFINEMENT PATH:      how Zomato improves this over time
```

### Decision 1 — Multi-axis classification, not single-label

```
DECISION
  Each post is independently tagged on 10 orthogonal axes: side,
  primary_topic, secondary_topics, sentiment, tone_flags, urgency,
  audience, author_role, geography, format. The combination drives
  every downstream decision — there is no single "category" field
  that summarises the post.

WHY
  Real social posts are multi-dimensional. A complaint can be
  (negative-sentiment) AND (high-urgency) AND (consumer-side) AND
  (cluster-member) AND (verified-author) — and each dimension drives
  a different downstream system. Forcing one summary label loses
  the information that makes routing possible.

ALTERNATIVE REJECTED
  A single primary category per post (the standard sentiment-tool
  approach). Rejected because routing decisions require querying
  multiple dimensions independently — e.g., "auto-reply only if
  sentiment is not abusive AND author is not a journalist AND
  topic is not religious-sensitivity." A single label cannot answer
  that question without re-examining the post text.

TRADE-OFF ACCEPTED
  More expensive per-post (single Claude call, but more output
  tokens). More complex to QA — each axis has its own accuracy
  target. More ways for a single dimension to be wrong without
  invalidating others.

SUCCESS METRIC
  Per-axis accuracy on a labeled holdout set. Track each axis
  independently. Target: 90%+ on side and topic, 85%+ on
  sentiment/tone, 80%+ on urgency.

REFINEMENT PATH
  Add new axes by editing the cross_cuts.yaml config (e.g., add a
  "promotion-conflict" axis when Diwali campaign launches). No
  retraining needed; the classifier prompt re-reads YAML on each
  run.
```

### Decision 2 — Hierarchical taxonomy (L1 → L2 → L3) over flat list

```
DECISION
  The topic taxonomy is a 3-level tree per side: 10 top-level
  categories (L1), each with 3–8 mid-level groups (L2), each with
  1–6 leaves (L3). Total ~50 consumer leaves, ~45 merchant leaves.

WHY
  Different audiences need different granularity. Customer-care
  needs to know "delivery issue" (L2). Engineering needs to know
  specifically "agent app crashed during pickup" (L3). PR needs
  the L1 view for trend reports. A flat list at any single
  granularity cripples the others.

ALTERNATIVE REJECTED
  A flat list of ~100 categories. Rejected because (a) prompting
  Claude with 100 unstructured options is unreliable, (b) reporting
  cannot aggregate to a meaningful L1 view without grouping logic
  living somewhere, (c) adding categories requires re-evaluating
  all 100 for overlap — exponentially worse than tree edits.

TRADE-OFF ACCEPTED
  Harder to maintain consistent depth. Some branches may be deeper
  than others (delivery issues are more decomposed than account
  issues). Periodic depth-balancing required.

SUCCESS METRIC
  Coverage rate: % of real posts that match a leaf (L3), not a
  parent. Target >90%. If a post can only match L1 or L2, the
  taxonomy is missing leaves.

REFINEMENT PATH
  Add subcategories without breaking parents. Old data still
  classifies at L2 if no L3 fits. The YAML is git-versioned —
  every change is reviewable, revertable.
```

### Decision 3 — Side detection (consumer / merchant) FIRST

```
DECISION
  Before any topic classification, the post is routed to one of:
  consumer, merchant, both, neither. Side determines which
  taxonomy is applied. Side detection is a separate, cheap step
  using rules + a focused LLM call — not bundled into the main
  classification.

WHY
  Consumer and merchant are different audiences with:
    - Different SLAs (customer-care responds in minutes;
      merchant-success in hours-days).
    - Different teams.
    - Different escalation paths.
    - Different response styles (consumer wants empathy;
      merchant wants accountability and action).
  A single classifier with a unified taxonomy would entangle
  these. Side-first guarantees the right taxonomy is applied
  to the right post.

ALTERNATIVE REJECTED
  A unified taxonomy with side as just-another-tag. Rejected
  because consumer's "C1.6.1 late delivery" and merchant's
  "M3.5 delivery agent issues" are conceptually distinct
  categories with the same surface name. Unifying them breaks
  the routing logic and confuses reporters reading a "delivery
  issues" trend chart.

TRADE-OFF ACCEPTED
  Side detection must be highly reliable. A wrong side = wrong
  taxonomy = wrong routing. A post that is genuinely both-sided
  (consumer fraud claim disputed by merchant) requires special
  handling.

SUCCESS METRIC
  Side-detection accuracy >97% on labeled holdout. Audit log
  of misroutes reviewed weekly.

REFINEMENT PATH
  Side detection rules in side_detection.yaml — keyword lists,
  bio patterns, thread-context cues. Both-sided posts get a
  dedicated `disputed_incident` flag in Layer 5 so they can
  be cross-referenced.
```

### Decision 4 — Tripwires (deterministic) run BEFORE the LLM

```
DECISION
  A keyword/regex/handle-list scan runs on every post before any
  LLM call. If a tripwire fires (food poisoning, hospitalization,
  sexual misconduct, court/FIR, journalist account, founder
  mention, religious/caste language, boycott hashtag, insider
  leak), urgency is forced to "critical" and a hard-coded
  audience override is applied. The LLM still classifies the
  topic, but its output cannot DOWNGRADE a tripwired post.

WHY
  Safety, legal, and PR-disaster events must NEVER depend on LLM
  judgement being correct. A 99% accurate classifier still misses
  1 in 100 — and that 1 in 100 might be the food-poisoning case
  that goes ignored for 4 hours because the LLM tagged it
  "generic dissatisfaction." The cost of one missed safety event
  eclipses the cost of running rules forever. Rules are
  auditable, fast, free, and explainable in court.

ALTERNATIVE REJECTED
  "Trust a well-prompted LLM with a 'safety-first' instruction."
  Rejected because: (a) prompts drift across model versions,
  (b) jailbreaks exist, (c) probabilistic guarantees are not
  guarantees, (d) we cannot show a regulator a deterministic
  chain of logic for why we acted or did not act.

TRADE-OFF ACCEPTED
  Keyword lists must be maintained. False-positive rate (innocent
  posts triggering tripwires) creates extra human-review
  workload. Edge cases where the right keyword is absent but the
  case is severe will slip through (e.g., a non-English
  description of food poisoning).

SUCCESS METRIC
  Tripwire RECALL ≥95% on labeled holdout of known critical
  events. Tripwire PRECISION ≥70% (we accept some false positives
  because human review of those is cheap; missing real events is
  not).

REFINEMENT PATH
  Every miss (a critical event that didn't fire any tripwire)
  gets the missed phrase added to tripwires.yaml within 24 hours.
  Every false-positive cluster gets a context exclusion rule
  added. Quarterly review of precision/recall.
```

### Decision 5 — YAML as source of truth, not code, not DB

```
DECISION
  The taxonomy, tripwires, cross-cut definitions, and policy
  rules all live in YAML files in the repo. The classifier reads
  these on every run; no values are hard-coded in Python.

WHY
  Categories evolve at business speed, not engineering speed.
  Today: "Zomato Gold benefit." Six months later: "Zomato Pay
  subscription." A new product line, a new campaign, a new
  regulatory requirement — each may require a category. The
  social team's category lead must be able to ship a new
  category without filing an engineering ticket.

ALTERNATIVE REJECTED
  Categories in a database, edited via UI. Rejected for v1
  because: no version history without extra tooling, no diff
  review, no rollback, no offline editing, no concurrent-edit
  conflict resolution. The DB approach is correct *eventually*
  (when there are 10+ editors) but premature now. YAML in git
  gets us 95% of the value with 10% of the engineering effort
  and a clean migration path to a UI-on-DB later.

TRADE-OFF ACCEPTED
  Slightly slower to deploy (YAML edits require service
  restart). No ACID guarantees. No UI for non-technical
  category lead — they must edit YAML in a web IDE or pair
  with engineering.

SUCCESS METRIC
  Edit frequency. If the YAMLs are edited weekly, the team is
  using them. If they sit untouched for a month, either the
  taxonomy is perfect (unlikely) or no one is engaging with the
  refinement protocol (likely a process failure to investigate).

REFINEMENT PATH
  Phase 5 (post-MVP): a thin web UI that edits the YAMLs via PR.
  Same source of truth, friendlier interface for non-engineers.
```

### Decision 6 — Sub-claim decomposition (multi-claim posts)

```
DECISION
  A single post can carry 1–N sub-claims, each independently
  classified. The full post still has a primary_topic, but the
  classification output also enumerates each sub-claim with its
  own topic, severity, and audience.

WHY
  Real complaints contain multiple issues:
  "Order was 2 hours late, the food was cold, AND when I called
   support they hung up on me twice."
  That is three claims, each routing to a different team:
  ops (lateness), restaurant-quality (cold), customer-care
  leadership (CCE behavior). Forcing one category routes only
  one and leaves the other two to be discovered "by hand" later.
  Worse: when the social team replies to the post, an empathetic
  reply that addresses only one of three issues feels hollow
  and erodes trust.

ALTERNATIVE REJECTED
  Single primary topic per post. Rejected because (a) it loses
  the "this needs THREE teams" insight, (b) it makes reply
  drafting blind to the other claims, (c) it makes pattern
  detection across teams impossible — engineering will never
  see the "agent app crashed AND food was cold" co-occurrence
  if only one is captured.

TRADE-OFF ACCEPTED
  Output JSON is more complex. Reply-drafting prompts must
  acknowledge multiple claims. Some posts will be over-
  decomposed (single rant interpreted as three claims).

SUCCESS METRIC
  Sub-claim recall: when posts are manually labeled as
  containing N claims, does the system find all N? Target:
  85% of N≥2 posts have at least N-1 claims found.

REFINEMENT PATH
  Sub-claim extraction prompt is in the same Claude call (cost-
  free). Examples in YAML — adding a few "watch out for posts
  with multiple claims like this" examples improves recall
  measurably.
```

### Decision 7 — Confidence scoring + human-review queue

```
DECISION
  Every classification carries a confidence score (0.0–1.0).
  Posts with confidence below 0.7 — OR any post with a
  sensitivity flag (religious, caste, sexual, threats) — land
  in a human review queue. They are NOT auto-actioned regardless
  of how confident the LLM appears.

WHY
  The system MUST acknowledge uncertainty. Pretending we are
  100% accurate would lead to bad auto-actions on the cases
  where we are wrong — and those cases are exactly the
  ambiguous, sensitive, novel ones where bad auto-actions are
  most damaging. Calibrated confidence + threshold gating is
  the only way to safely automate at scale.

ALTERNATIVE REJECTED
  Auto-action on whatever the LLM produces. Rejected because
  one bad auto-reply on a religious-sensitivity case can
  generate a PR incident larger than a year of correct
  classifications. Asymmetric downside demands conservative
  defaults.

TRADE-OFF ACCEPTED
  Some posts will require human review unnecessarily (false
  uncertainty). Threshold tuning matters and is non-trivial.
  The review queue must be staffed, or the entire system
  becomes a backlog.

SUCCESS METRIC
  Calibration curve — plot confidence vs actual accuracy on
  reviewer-labeled holdout. They should be linear. If they
  diverge (system is overconfident or underconfident), the
  threshold needs adjustment.

REFINEMENT PATH
  Reviewers' corrections become new few-shot examples added to
  the relevant YAML leaf. Next classification batch is more
  accurate without code changes. After 4 weeks of operation,
  the long-tail posts that were initially low-confidence are
  now high-confidence, and the review queue thins.
```

### Decision 8 — Lifecycle awareness (handle history)

```
DECISION
  A separate `handles` table tracks every author we've seen:
  first_seen, total_posts, prior_complaints, prior_praise,
  resolved_count, unresolved_count, sentiment_30d_avg, profile
  class, watchlist flag, notes. Classification consults this
  table to determine if a post is a first-mention, follow-up,
  escalation, or re-flare.

WHY
  Same complaint twice from the same handle = different signal
  than one complaint each from two handles. Same complaint
  after we already replied = different from before. Without
  lifecycle awareness, the system treats a fourth ignored
  complaint exactly like a first complaint and continues to
  fail the customer.

ALTERNATIVE REJECTED
  Stateless classification — classify each post in isolation.
  Rejected because it is blind to repeat customers, follow-ups,
  resolution patterns, and the most important signal: "did we
  actually solve their problem?"

TRADE-OFF ACCEPTED
  Storage growth (one row per unique handle, plus history).
  Privacy implications — we are building a profile of public
  social handles, which is allowed but should be policy-
  reviewed. Cost: an extra DB read per classification.

SUCCESS METRIC
  Repeat-handle detection precision: when the same handle posts
  about the same topic twice in 30 days, does the classifier
  flag the second one as "follow-up" or "escalation"? Target
  >95%.

REFINEMENT PATH
  Phase 3+: link cross-platform handles (same person on Reddit
  AND Twitter) using bio cross-references. Currently, profiles
  are per-platform.
```

### Decision 9 — Cluster detection (post-batch)

```
DECISION
  After each classification batch, run a clustering pass:
  group recent posts (last 60 min) by (side, primary_topic,
  geography, time-bucket). Clusters of ≥5 posts get a
  cluster_id; the first post in each is the cluster_lead;
  the rest are cluster_members. Outlier posts that almost-
  match a cluster but contradict (e.g., a happy customer in
  the middle of a complaint storm) are tagged cluster_outlier.

WHY
  Single posts lie; patterns of posts tell the truth. 50
  individual complaints about a Bangalore outage = 50 noise
  items if treated independently. Aggregated into one cluster =
  1 actionable signal: "Bangalore is having a payment outage,
  notify ops." The system that handles 1 alert vs 50 has a
  50× productivity gap on outage events.

ALTERNATIVE REJECTED
  No clustering — let the social team manually notice patterns.
  Rejected because by the time a human notices "wait, I've seen
  this complaint 30 times," 30 minutes have passed and the
  outage has compounded.

TRADE-OFF ACCEPTED
  Cluster precision is its own metric — clusters that aren't
  real (coincidental similar posts not from one event) generate
  false alerts. Threshold for "cluster" is sensitivity-tunable.

SUCCESS METRIC
  Cluster precision (clusters that are real events) ≥80%.
  Cluster recall (real events that get clustered) ≥90%.

REFINEMENT PATH
  Tune cluster threshold per topic — outage-prone topics
  (payment, login) cluster at lower N; rare topics (food
  poisoning) cluster at N=2. Per-topic cluster thresholds
  live in the YAML alongside category metadata.
```

### Decision 10 — Action policy as a derived layer (rules over Layers 1–9)

```
DECISION
  The decision "should this post be auto-actioned?" is a pure
  function of the classification — no LLM call, no separate
  model. The rules live in Python (or could live in YAML;
  Python chosen for v1 because conditions are nontrivial to
  express in YAML).

  Example rule:
    auto_action_safe = (
        confidence >= 0.7
        and not tripwires_fired
        and "religious-language" not in tone_flags
        and "caste" not in tone_flags
        and severity_tier <= "L3"
        and claim_shape not in {"sarcastic_complaint",
                                  "threat_legal", "threat_social"}
        and resolution_state != "engaged_awaiting"
        and not author.watchlist
        and not disputed_incident
        and not cluster_lead
    )

WHY
  The decision "should we auto-reply?" is too important to leave
  to LLM judgment. It must be deterministic, predictable,
  auditable, debuggable. When something goes wrong (an auto-
  reply fires that shouldn't have), an engineer must be able to
  read the rule and explain in 30 seconds which clause failed.
  Pure rules give us this; LLM-decided actions do not.

ALTERNATIVE REJECTED
  "Ask the LLM whether it's safe to auto-action." Rejected
  because (a) LLMs cannot be reliably instructed to refuse on
  edge cases — they default to helpfulness, (b) any auto-reply
  on a religious/caste post is a brand emergency, and we
  cannot accept a probabilistic safety guarantee on that, (c)
  the rule version is auditable in a way LLM judgment is not.

TRADE-OFF ACCEPTED
  Rules can be too conservative (gating too many posts to
  manual review → reviewer fatigue). Or too liberal (PR risk).
  Tuning is ongoing.

SUCCESS METRIC
  Auto-action error rate: % of auto-actions that should have
  been manual. Target <0.1%. (Out of 1,000 auto-actions, fewer
  than 1 should have been routed to human review.)

REFINEMENT PATH
  Rules are versioned in code. Each rule change ships as a PR
  with explanation, reviewable by the social team lead.
  Quarterly review of the auto-action error log; tighten or
  loosen specific clauses based on what caused errors.
```

---

## 3. The Taxonomies

### 3.1 Why two taxonomies, not one

Zomato is a two-sided marketplace. The Consumer app and Merchant app serve fundamentally different audiences with different SLAs (consumer-care responds in minutes; merchant-success in hours), different teams, different escalation paths, and even different definitions of "complaint" — a consumer's "agent didn't show up" is a merchant's "platform reliability issue affecting my revenue."

Forcing a single taxonomy would either cripple consumer-side granularity (consumer needs ~50 leaves), cripple merchant-side granularity (merchant needs ~45 leaves), or muddy both with a forced common parent that obscures team ownership.

Two taxonomies, side detected first, each tuned to its audience.

### 3.2 Consumer taxonomy — top-level rationale

| L1 | Why it earned a top-level slot |
|---|---|
| **C1 Order Lifecycle** | The most common complaint surface. Every step from search → menu → cart → payment → tracking → delivery → post-delivery has its own failure modes. Highest volume, customer-care owns it. |
| **C2 Payment & Billing** | Money issues are a distinct severity class. A wrong charge has finance-team implications absent from a delivery complaint. Customer-care + finance overlap. |
| **C3 Account & Access** | Account suspension/hacking is high-anxiety for users; a separate top-level surface lets trust-safety prioritize them. |
| **C4 Loyalty & Promos** | Zomato Gold, coupons, cashback — distinct from regular orders, owned by marketing operations. Frequent issue volume, but rarely safety-critical. |
| **C5 App / Technical** | Engineering-routed. Distinct from "service" issues. Bugs, crashes, performance regressions need product team eyes. |
| **C6 Safety** | Tripwire-capable. Food poisoning, hospitalization, agent harassment, underage alcohol orders. Vastly disproportionate per-event weight even at low volume. Top-level so it never gets buried. |
| **C7 Support Experience** | When customer-care itself fails the customer. Different signal than the underlying issue — escalates to CC leadership for QA. |
| **C8 Product Feedback** | "Feature request" is a *distinct* signal from "complaint." Goes to product team, not customer-care. Mixing them buries product input. |
| **C9 Brand Perception** | Praise, brand love, generic dissatisfaction without specific incident, competitor comparison. Marketing-routed. Critical for brand health metrics that don't fit anywhere else. |
| **C10 Edge / Outlier** | Disasters, regulatory mentions, discrimination claims, off-topic noise. Catch-all for anything that doesn't fit elsewhere — but never a dumping ground because each leaf has explicit routing. |

### 3.3 Merchant taxonomy — top-level rationale

| L1 | Why it earned a top-level slot |
|---|---|
| **M1 Onboarding & Verification** | Merchant onboarding has its own team and its own bottlenecks (KYC, FSSAI, GST). Distinct from operations. |
| **M2 Listing & Visibility** | Whether a restaurant appears, where, and to whom — a distinct surface from order operations. Search/discovery team owns it. |
| **M3 Orders & Operations** | The merchant-side equivalent of consumer's C1. Daily operational pain. Highest volume on merchant side. |
| **M4 Payment & Settlement** | Payouts, settlements, deductions. Finance + merchant-success. High emotional intensity (cash flow). |
| **M5 Commission & Fees** | Strategically distinct from settlement issues. Commission disputes can become collective actions. Leadership-attention category. |
| **M6 Reviews & Rating** | False reviews, extortion, retaliation. Trust-safety domain. Different from order operations. |
| **M7 Analytics & Dashboard** | Merchant-product team. Bug reports specifically on the merchant analytics surface. |
| **M8 Ads & Growth** | Ads team. Distinct because ROI disputes have different SLAs and different escalation. |
| **M9 Policy & Trust** | Suspensions, appeals, bias allegations. Legal-adjacent. |
| **M10 Relationships / Support** | Account manager (KAM) experience. Critical for relationship health, even if individual complaints are small. |
| **M11 Industry / Collective** | Tripwire-capable. NRAI complaints, CCI cases, coordinated boycotts, mass restaurant action. Strategic-level concern, leadership routing. |
| **M12 App / Technical (Merchant App)** | Bugs in the *merchant* app — distinct from M3 operations. Merchant-engineering owns it. |
| **M13 Safety (Merchant-side)** | Threats from customers, threats from agents, legal action threats against Zomato. Tripwire-capable. |

(The full leaf list lives in `taxonomy/consumer.yaml` and `taxonomy/merchant.yaml`. This document captures the *shape* and rationale, not every leaf.)

---

## 4. Cross-cut Axes

These ten axes are tagged on every post, regardless of topic. They are *orthogonal* to topic — same topic, different axis values change routing entirely.

| Axis | Values | Why this axis exists |
|---|---|---|
| **Side** | consumer / merchant / both / neither | Determines which taxonomy applies; routes to which team. |
| **Sentiment** | negative / positive / neutral / mixed / abusive / sarcastic | Gates auto-reply; abusive/sarcastic never auto-reply. |
| **Tone flags** | accusation, threat, profanity, religious, caste, gender, political, satirical, factual | Sensitivity routing. Religious/caste/gender flags require manual review regardless of topic. |
| **Urgency** | critical / high / medium / low (with numeric 0–1 score) | SLA bucket. Drives queue priority. |
| **Audience** | one or more of {customer-care, merchant-ops, safety, legal, pr, marketing, eng, finance, ir, founder-office, trust-safety, ads-team, kam, none} | Routing — multiple teams can see the same post with their own role context. |
| **Author role** | consumer / restaurant-owner / delivery-partner / journalist / politician / influencer / verified / regular / suspected-bot / employee-suspected | Author identity changes the response tone, escalation path, and confidence-required bar. |
| **Author influence** | tier-1 (>1M followers / verified press) / tier-2 (10K–1M) / tier-3 (<10K) / unknown | Reach amplifies impact; tier-1 complaints escalate faster regardless of topic. |
| **Format** | complaint / question / review / news / meme / opinion / threat / promotion-spam | Some formats never get auto-replies (memes, spam). Question vs complaint changes reply tone. |
| **Geography** | point / neighborhood / city / state / country / multi / international / unknown | Local-ops dashboards consume this; granularity is itself a signal of legitimacy. |
| **Lifecycle** | first-mention / repeat-handle / follow-up-after-response / resolution-confirmation / re-flared | Repeat complainants escalate faster; already-engaged posts avoid duplicate replies. |

---

## 5. Tripwire Catalog

Tripwires are deterministic rules. Each entry below has: detection criteria (regex/keyword/handle-list), the override behavior, and the rationale for inclusion.

| Tripwire | Detection | Override | Why |
|---|---|---|---|
| **Food safety incident** | Keywords: poisoning, hospitalized, ICU, vomit + food, allergic + reaction, foreign object (hair, glass, worm, insect) | urgency=critical, audience adds {safety, legal, pr, ceo-office}, never auto-reply | Health emergencies have asymmetric downside; missing one is a corporate crisis. |
| **Death claim** | "died", "passed away", "killed" + zomato/delivery context | Same as food safety + immediate Slack to founder-office | Death claims may be exaggerations or real; either way, must reach founder office within minutes. |
| **Sexual misconduct** | harassment, molested, inappropriate, predator + agent/delivery | urgency=critical, audience={legal, safety, pr}, never auto-reply, DM-only path, evidence-preservation flag | Public engagement on sexual misconduct allegations risks revictimization and legal exposure. |
| **Court / FIR / police** | FIR filed, police complaint, court case, sued, defamation | audience={legal, pr}, "do not engage publicly until cleared" lock | Anything we say publicly may be used as evidence; legal must clear before engagement. |
| **Journalist mention** | Handle in curated press list (Reuters, Bloomberg, ET, Mint, FT, Inc42, etc.) OR @-handle declares "journalist" in bio | audience adds {pr, founder-office}, urgency≥high, "PR drafts response, not auto" | A journalist's tweet *is* a story being researched. Response affects the story. |
| **Politician mention** | Handle in curated political list (verified MPs, ministers, party handles) | audience adds {pr, founder-office, legal}, urgency=critical | Political engagement is high-stakes; founders must approve any response. |
| **Founder personal attack / mention** | @deepigoyal, @gaurav_tw, "deepinder" + name | audience adds {founder-office, pr} | Founder-targeted content needs founder-office awareness within minutes. |
| **Boycott / coordinated** | hashtags (#LogoutZomato, #BoycottZomato, #DeleteZomato) OR ≥10 similar tweets in 1hr | cluster flag raised, audience={pr, leadership}, urgency=critical | Coordinated campaigns escalate exponentially; early detection enables narrative response. |
| **Religious/caste/gender** | Regex set covering religious labels, caste references, gender slurs in context with zomato | sensitivity flag set, never auto-reply, manual PR review only | Edge cases of public discourse where bad replies generate disproportionate damage. |
| **Insider leak / whistleblower** | "I worked at zomato", "ex-employee", "leaked internal", screenshots of internal tools | audience={legal, hr, pr, founder-office}, evidence-preservation flag | Insider claims may be true or fabricated; both cases require legal/HR coordination. |
| **Anti-competitive / regulatory** | CCI mention + zomato + commission/predatory/anti-competitive language | audience={legal, leadership} | CCI is the regulator that has previously investigated platforms; must be tracked. |

The full list lives in `taxonomy/tripwires.yaml`. Adding new tripwires is a pull-request-level change — every addition is reviewed because tripwires bypass everything else.

---

## 6. The Seven-Layer Pipeline

Each post flows through seven layers. Each layer is independently testable. Each layer's output is deterministic given input + config.

```
LAYER 0  Ingestion             — raw post + scraper metadata (Phase 1; done)
LAYER 1  Foundation            — side detection · topic classification · tripwires · cross-cut tagging
LAYER 2  Depth                 — severity tier (per leaf) · sub-claim split · evidence type · resolution state · claim shape
LAYER 3  Identity              — author profile · trust score · history with brand · LTV proxy · geographic precision
LAYER 4  Context               — thread context · channel context · real-world correlation · watchlist hits
LAYER 5  Aggregation           — cluster role · velocity · reach trajectory · counter-narrative detection · disputed incidents
LAYER 6  Action policy         — auto_action_safe (deterministic) · response tone · action blockers
LAYER 7  Feedback              — review queue placement · accuracy tracking · prompt-example refresh
```

| Layer | Determinism | LLM use | Cost driver |
|---|---|---|---|
| 1 | Mostly LLM (one call) | Yes — topic + cross-cuts + side | One Claude call per post |
| 2 | Mostly LLM (same call as L1) | Same call extended | Same call, more output tokens |
| 3 | Rules + LLM (per-handle, cached) | LLM for new handles only | One Claude call per *new* handle, ~0 marginal cost |
| 4 | Rules | None | DB lookups |
| 5 | Rules | None | Batch SQL |
| 6 | Pure rules | None | None |
| 7 | Rules | None | Human time |

Net cost per post in steady state: ~1 Claude call (Sonnet 4.6, ~500 input + ~400 output tokens) ≈ $0.0015–$0.003 per post.

---

## 7. Interview Defense

Twelve questions an interviewer is most likely to ask, with prepared answers. Each answer connects back to a specific architectural decision in Section 2.

### Q1: How did you decide on these specific categories?

Three sources informed every category:
1. **Live data analysis** — scraped 170 real Zomato/Blinkit posts and tagged each by hand to find natural clusters before writing any taxonomy.
2. **Org structure** — every leaf maps to a Zomato team that exists today (customer-care, merchant-ops, safety, legal, PR, founder-office, IR, ads-team, KAM). If a category had no owner, dropped.
3. **Risk gradient** — categories organized around what *could become* an incident, not what's most common. C6 (Safety) has fewer posts than C1 (Order Lifecycle) but ten times the per-post weight.

### Q2: How do you know the long tail is covered?

Initially I don't. That's why Layer 7 (feedback loop) exists. Posts that classify with confidence below 0.7 land in a review queue. Reviewers' corrections become new YAML examples. After ~4 weeks of operation, the long-tail posts that initially fell through are now well-classified, without code changes. The system is *designed* to bootstrap from blind spots.

### Q3: What's the biggest risk in this design?

Tripwire silent failures — a real safety event missed by both regex AND LLM. Mitigations: (a) tripwire recall target ≥95% on a labeled holdout, (b) full audit log of every classification with reasoning, (c) "manually flag this post" override path for ops to correct in real-time, (d) cluster detection as a backstop — even if individual tripwires miss, 5 similar posts in 30 minutes will trigger cluster review.

### Q4: How does this scale to 100K posts/day?

Per-post Claude API call is the cost driver. At 100K/day with Sonnet 4.6 (~$3/M input, ~$15/M output, ~500 tokens/post): roughly **$200/day, $6K/month**. Optimizations: (i) batch classify 20 posts per call (5× cost reduction), (ii) tripwire-handled posts skip Claude entirely (~10% reduction), (iii) cache identical-text posts (~15% reduction). Net at scale with optimizations: ~$3K/month. Acceptable for the value delivered.

### Q5: How is this different from off-the-shelf social listening (Brandwatch, Sprout, Talkwalker)?

Three differences. **Org-aware**: every category routes to a Zomato team that actually exists; off-the-shelf tools route to generic buckets. **Multi-dimensional**: 10 orthogonal axes vs typical 2–3 (sentiment + topic). **Editable by the social team itself**: YAML config means category lead ships a new category in one git commit; SaaS tools require waiting for vendor roadmap. The USP isn't sentiment analysis — it's a routing engine purpose-built for Zomato's two-product, multi-team reality.

### Q6: Why are some categories never auto-actioned?

The cost of a wrong auto-action on certain categories (food poisoning, religious sensitivity, legal threats, sexual misconduct allegations, personal attacks on agents) is asymmetric — orders of magnitude worse than a slow human response. Better to have a human read 100 sensitive posts than have the system reply badly to one of them.

### Q7: How do you handle adversarial input — bots, coordinated campaigns, gaming?

Multiple layers: (a) trust score per handle, low for new accounts; (b) watchlist for known problem accounts; (c) cluster detection catches coordinated campaigns by their statistical signature; (d) pattern detection in the handle-history table (e.g., 50 different handles all posting near-identical text within an hour = synthetic). No single defense is sufficient; combined, they raise the cost of attack significantly.

### Q8: Will this work in regional languages?

Claude is multilingual but the YAML examples are English-heavy at v1. Phase 1: English + romanized Hindi (which dominate Zomato chatter). Phase 2: add Tamil/Bengali/Marathi/Kannada exemplars to YAML. The architecture itself is language-agnostic; only the prompt examples need expansion. Tripwire keyword lists also need regional language variants — this is a known gap.

### Q9: What if a category's right team isn't obvious?

Multi-audience tagging is built in. A post can route to legal+pr+customer-care simultaneously. Each team has its own queue showing the same post with its role context: legal sees "evidence preservation needed," PR sees "narrative risk," customer-care sees "draft empathetic reply for approval."

### Q10: How do you avoid LLM hallucinating categories?

(a) Constrained output — Claude is asked to return JSON with category fields constrained to enums from the YAML. (b) Few-shot examples per category in the YAML — the prompt includes 2–3 real examples for each category Claude might use. (c) Tripwires for critical signals — if the LLM hallucinates a category in a safety case, the tripwire still fires and overrides. (d) Confidence + review queue — hallucinated categories tend to have low LLM confidence; they get caught by the review queue.

### Q11: How would you improve this with more time?

Priority order:
1. **Image/video analysis** — currently text-only; food photos and bug screenshots carry critical signal.
2. **Cross-platform attribution** — link the same incident discussed on Twitter and Reddit.
3. **Sentiment per sub-claim** — currently sentiment is per-post; multi-issue posts may have mixed sentiments per claim.
4. **Predictive virality model** — flag posts likely to explode in the next N hours.
5. **Auto-generated weekly insights** for leadership — the system has the data; it could write the report.
6. **Multilingual exemplar expansion** — Tamil, Bengali, Marathi, Kannada YAML examples.
7. **A UI** for category lead to edit YAML without git.

### Q12: How do you measure success?

Three categories of metrics:

**Classification quality** (per axis): side accuracy ≥97%, topic accuracy ≥85% (L3 leaf), sentiment accuracy ≥85%, tripwire recall ≥95%, tripwire precision ≥70%.

**Operational**: routing accuracy (% of escalations that hit the right team first time, ≥85%); time-to-acknowledgment (median minutes from post creation to Zomato action, target <5 min for critical); auto-action error rate (<0.1%).

**Business**: response rate (% of complaints answered, target ≥95%); resolution rate (% that progress to "resolved_thanks" lifecycle, target ≥60%); sentiment trajectory of repeat complainants (target net-positive after engagement).

---

## 8. The Refinement Protocol

The classifier is not static. It is designed to improve on a known cadence. Without this, leadership cannot trust it long-term.

### Daily — operations team (~15 min)

- Triage the confidence-flagged review queue.
- For each correction, add an example to the relevant YAML leaf.
- Note any tripwire false-positive — add a context exclusion rule the same day.

### Weekly — social team lead (~1 hour)

- Audit auto-action errors (any wrong auto-replies that went out?).
- Review cluster precision (any clusters formed that shouldn't have?).
- Tune confidence thresholds if drift is detected.
- Track week-over-week: response rate, time-to-action, escalation accuracy per team.

### Monthly — cross-functional review (~2 hours)

- New category proposals based on review-queue patterns (what's been hard to classify?).
- Category retirement: low-volume or low-value leaves that haven't fired in 30 days.
- Cross-team review: "Does customer-care still want X routed here? Does PR get noise from Y?"
- Leadership reads dashboard: top categories this month, week-over-week deltas, top issues by team.

### Quarterly — engineering + product (half-day)

- Calibration check: are confidence scores still accurate? (plot confidence vs actual accuracy).
- New axis evaluation: does the org need a new dimension? (e.g., "ESG signal", "regional-language tag").
- Re-baseline if Claude model upgrades change classification behavior.
- Retire stale tripwires; add new ones based on emerging risks.

This cadence is what differentiates a one-off classifier from a system Zomato keeps running on Monday — and Tuesday, and 18 months from now.

---

## 9. Metrics & Success Definition

Three layers of metrics:

### Classification quality

| Metric | Target | How measured |
|---|---|---|
| Side detection accuracy | ≥97% | Labeled holdout set, weekly |
| Topic accuracy (L3 leaf) | ≥85% | Labeled holdout set, weekly |
| Sentiment accuracy | ≥85% | Labeled holdout set, weekly |
| Tripwire recall | ≥95% | Manual audit of known critical events |
| Tripwire precision | ≥70% | Audit log review, weekly |
| Confidence calibration | Linear correlation ≥0.85 | Reviewer-labeled holdout, monthly |

### Operational

| Metric | Target | How measured |
|---|---|---|
| Routing accuracy | ≥85% | Receiving team confirms / corrects, weekly |
| Time-to-acknowledgment (critical) | <5 min median | Timestamps in DB |
| Time-to-acknowledgment (high) | <30 min median | Timestamps in DB |
| Auto-action error rate | <0.1% | Audit log review |
| Cluster precision | ≥80% | Manual audit of cluster events |
| Cluster recall | ≥90% | Comparison vs known events |

### Business outcomes

| Metric | Target | How measured |
|---|---|---|
| Response rate (complaints) | ≥95% | DB query: % of complaints with action_taken |
| Resolution rate | ≥60% | DB query: % that reach `resolved_thanks` lifecycle |
| Repeat complainant sentiment trajectory | Net-positive after engagement | Per-handle sentiment moving average |
| Reviewer-corrected classifications/week | Trending down | Review queue size over time |
| YAML edit frequency | ≥1/week | Git history |

---

## 10. Out of Scope; Future Work

What this v1 deliberately does not do, and why.

| Out of scope | Why deferred |
|---|---|
| **Image / video analysis** | Text-only is the 80/20. Image analysis adds significant cost and complexity; defer until text classifier is proven and cost-justified. |
| **Cross-platform handle linking** | Useful but non-trivial; per-platform profiles cover 90% of value. |
| **Predictive virality models** | Requires historical data we don't yet have. Build after 3 months of operation. |
| **Multilingual exemplar expansion** | English + romanized Hindi covers ~85% of Indian Zomato chatter. Tamil/Bengali/etc. expand once the core is stable. |
| **A UI for editing the YAML** | YAML-via-git is sufficient for v1. UI is a Phase 5 addition. |
| **Automated reply drafting** | This is Phase 3, intentionally separated. Classification first, action second. |
| **Connection to internal Zomato systems** (CRM, Linear, Slack workspaces) | Phase 3 territory. v1 produces classification JSON; Phase 3 wires it to internal systems. |
| **Privacy / GDPR / data retention policy review** | Required before production deployment, not before v1 prototype. Flagged as a launch-blocker for production. |

---

## 11. USP Statement

> This isn't sentiment analysis dressed up. It's a multi-dimensional decision-support system purpose-built for Zomato's two-product, multi-team reality. Every post is evaluated on ten orthogonal dimensions; safety-critical signals are caught by deterministic rules that survive any LLM behavior; the same data is presented differently to customer-care, merchant-ops, PR, legal, and the founder office. The taxonomy is editable by the social team itself — no engineering bottleneck. The system carries its own confidence calibration and a structured human-review feedback loop, so it gets sharper every week. The classification isn't the output — it's the routing infrastructure that turns 1,000 daily posts into a prioritized worklist, with reasoning, audit trail, and a documented improvement protocol.

---

*End of design document. Code that implements this design lives in `social_watch/classifier/` and the YAML configs in `taxonomy/`. Every code path is traceable to a decision in Section 2; every category has a rationale in Section 3.*
