# Classification Deep Dive — The Prioritization Engine

**Version:** 0.1 (planning lock — pre-implementation)
**Date:** 2026-04-30
**Scope:** The "what to tackle first" engine. Builds on `CLASSIFIER_DESIGN.md` (which covers the 7-layer foundation) by going deep on **classification depth, dynamic priority scoring, and the edge-case catalog.**
**Purpose:** Defend the design under interview pressure. Show leadership how a billion-dollar enterprise actually thinks about this. Don't ship code; ship reasoning.

> If `CLASSIFIER_DESIGN.md` answers *"how do we tag every post?"*, this document answers the harder question: **"given 700 tagged posts at 9 AM Monday, which one does the operator open first — and why?"**

---

## Table of Contents

1. [The product question we're really solving](#1-the-product-question)
2. [Industry context: how Brandwatch, Sprinklr, Pulsar do this](#2-industry-context)
3. [The Priority Score — multi-factor weighted model](#3-priority-score)
4. [The eight signals that feed the score](#4-eight-signals)
5. [Edge case catalog (60 cases)](#5-edge-case-catalog)
6. [Dynamic adaptation — how the system learns](#6-dynamic-adaptation)
7. [Crisis & viral detection — the early warning layer](#7-crisis-detection)
8. [Author tier system + LTV proxy](#8-author-tier)
9. [SLA targets per priority band](#9-sla-targets)
10. [Fairness, bias, defensibility](#10-fairness)
11. [Validation & quality gates](#11-validation)
12. [What we deliberately decided NOT to build](#12-out-of-scope)
13. [Build phasing](#13-phasing)

---

## 1. The product question we're really solving

The brief asks "classify and escalate." The naïve interpretation is: tag every post, list the urgent ones. **That's what every off-the-shelf tool does — and it's why operators still drown.**

The real question — the one a Zomato social-team lead actually asks at 9 AM on Monday morning — is:

> *"I've got 8 minutes before my standup. Of these 712 unread posts, which 4 do I open first, and what do I do with each?"*

This is a **prioritization problem dressed up as classification**. Tagging is necessary but not sufficient. The dashboard needs to answer **"which one first?"** with the precision and defensibility of a triage nurse — not the chaos of an inbox sorted by date.

That reframing changes everything downstream:
- Categories aren't the output. They're an **input** to the priority score.
- Severity isn't a label. It's a **scalar weighted into the score.**
- Tripwires aren't filters. They're **deterministic priority overrides** that bypass the score when the legal/safety bar is reached.

Everything below serves this single product question.

---

## 2. Industry context

What the leading tools actually do (April 2026):

| Tool | What they're best at | What we should emulate | What we shouldn't |
|---|---|---|---|
| **Brandwatch** | Data scale + archive depth — the SaaS used in boardroom decks | Multi-axis tagging at granular leaf depth; conversation-level (not post-level) clustering | Don't try to compete on data scale — pointless for a single-brand tool |
| **Sprinklr** | "All-in-one" CX suite (listening + ads + service + commerce) | Per-channel SLA configurability; routing rules engine | Don't build the CX suite. Stay focused. |
| **Pulsar** | Audience intelligence + narrative prediction | Predict which posts will go viral (velocity-based modeling) | Don't try to compete on narrative-level analytics in v1 |
| **Typewise** | Customer support ticket prioritization (multi-factor weighted score) | **Their P0/P1/P2/P3 banding with explicit weights — directly applicable here** | None — adopt this whole-cloth |
| **Kustomer / Gorgias** | AI ticket routing with sentiment + customer-tier signals | Per-tier SLA, tier auto-detection from author profile | Don't tightly couple to e-commerce data — we don't have order history per author |

**The cross-industry consensus** that emerges from this scan:

1. **Multi-factor weighted scoring is universal.** Every serious tool computes a single priority score from 4–8 weighted signals, not a single label.
2. **Banding is explicit (P0–P3) with SLA targets per band.** Without an SLA target, "priority" has no operational meaning.
3. **Velocity (rate of mentions) is the gold-standard crisis signal.** Brandwatch, Pulsar, and Sprinklr all alert on `>3× baseline volume in 60 min`.
4. **Author tier is treated as a multiplier, not a category.** A verified press handle gets the same complaint as anonymous handle but with 5× weight.
5. **Auto-action confidence threshold is conservative.** Industry default ~0.85 — meaning the system auto-replies on <15% of posts even at scale.

We adopt all five. The rest of this document is the explicit implementation.

---

## 3. The Priority Score

A single number per post, recomputed every cycle, used to rank rows on the urgency page.

### 3.1 The formula

```
priority_score = clamp01(
    0.30 × severity        +
    0.20 × reach           +
    0.15 × velocity        +
    0.10 × sla_proximity   +
    0.10 × repeat_penalty  +
    0.08 × cross_channel   +
    0.04 × author_trust    +
    0.03 × counter_narrative_offset
)

→ priority_band:
    P0 (drop everything)     score ≥ 0.85
    P1 (today)               score ≥ 0.65
    P2 (this week)           score ≥ 0.40
    P3 (track only)          score <  0.40

→ TRIPWIRE OVERRIDE:
    If any tripwire fired → priority_band forced to P0,
    score forced to 1.0, recomputation suppressed.
```

### 3.2 Why these weights, why these signals

**Severity (30%) — the largest single factor.**
The topic-leaf urgency multiplied by the post's specific severity tier (L1–L5 within the leaf). A "delivery 5 min late" L1 post and a "delivery agent assaulted me" L5 post share a parent topic but differ by 4× in severity weight.

> *Why 30% not 50%?* Because severity alone over-weights individual incidents and misses systemic trends — 50 L2 complaints clustered geographically is bigger news than 1 L4 incident. Reach + velocity capture that.

**Reach (20%) — author influence × current engagement.**
A complaint with 3 likes and 0 retweets from an anonymous handle ≠ the same complaint at 800 likes from a verified journalist. Computed as: `log10(1 + likes + 5×retweets) / 6` × `author_influence_multiplier`.

> *Why not larger?* Because high reach can be a delayed signal — by the time a post hits 10K likes, the news cycle has moved. Velocity catches it earlier; reach captures the now-state.

**Velocity (15%) — rate-of-engagement growth.**
Engagement-per-hour over the last hour vs the post's lifetime average. A post growing at `>3× baseline` is a viral-trajectory signal worth promoting *before* it explodes. Industry consensus threshold (Brandwatch, Pulsar).

**SLA proximity (10%) — minutes-until-breach.**
For posts with an active SLA timer, how close are we to the response deadline? Linear ramp from 0 (just posted) to 1 (deadline missed). A P1 post 25 min into its 30 min window contributes more than the same post at minute 1.

**Repeat penalty (10%) — same handle complained N times.**
A handle that's complained 3 times in 7 days about the same topic is a 3× weight on the next post. Captures *patience-thinning* customers — the ones most likely to escalate publicly to the press.

**Cross-channel (8%) — same incident on multiple platforms.**
If the same handle is also complaining on Reddit + Twitter, OR if cluster detection links a Twitter incident to a Reddit thread, the score boosts. Cross-platform amplification is a viral risk independent of any single platform's reach.

**Author trust (4%) — anti-bot, anti-spam baseline.**
Inversely weighted: low trust → small *de*-prioritization (~0.04 max). Doesn't punish anonymous accounts (most legitimate complainants are anonymous), but does discount obvious bot patterns.

**Counter-narrative offset (3%) — happy users defending us.**
If a complaint is countered by 5+ positive replies in the same thread, slight de-priority (the conversation is self-correcting). Small weight by design — this is signal-shaping, not signal-suppression.

### 3.3 Why a weighted scalar, not ML

The classifier already uses an LLM. Why is the priority score a hand-tuned weighted formula and not another ML model?

| Property | Weighted scalar | ML model |
|---|---|---|
| Auditability | Every weight has a rationale | Black box |
| Tunability | Social team lead can adjust weights | Requires retraining |
| Stability | Same input → same output forever | Drifts with retraining |
| Defensibility (legal review) | "We boosted this because the author is verified press" | "The model said so" |
| Dataset requirement | Zero | Thousands of labeled examples |
| Time to ship | 1 day | 6 weeks |

Hand-tuned wins decisively at v1. We layer ML on top *only after* we have months of operator-corrected priority decisions to learn from.

### 3.4 What this gives the operator on the urgency page

```
RANK   POST                                  SCORE  WHY (top contributors)
1      "@zomatocare ICU after eating..."     1.00   tripwire: food_safety
2      "30 mass complaints, Mumbai outage"   0.91   velocity 4.2× + cluster + sla
3      "@deepigoyal investigating you..."    0.87   reach (verified press) + severity L4
4      "Refund 8 days late, 4th time..."     0.81   repeat_penalty + sla_proximity
5      "Rude agent identity issue"           0.79   tripwire: religious_sensitivity
```

Each row's score is **decomposable** — the dashboard can show *which signals contributed how much* on hover. Operator never sees a black-box number.

---

## 4. The eight signals — implementation notes

For each signal: the source data, the normalization, the time complexity, the edge cases.

### 4.1 Severity
- **Source**: `classification.urgency_score` (LLM-assigned 0–1) × `severity_tier_multiplier` (per-leaf, in YAML).
- **Tier multipliers**: L1=0.2, L2=0.4, L3=0.6, L4=0.8, L5=1.0.
- **Edge case**: Multi-claim post takes the *max* severity across claims, not the mean (one bad claim doesn't get diluted).

### 4.2 Reach
- **Source**: `metadata.like_count + 5×metadata.retweet_count + 0.5×metadata.reply_count` (Twitter); `metadata.score + metadata.num_comments` (Reddit).
- **Normalization**: `log10(1+x) / log10(1+P95_global)`. P95 baseline updated nightly from the global distribution.
- **Multiplier**: Author tier (see §8) — tier-1 = 5×, tier-2 = 2×, tier-3 = 1×.
- **Edge case**: Sock-puppet networks. Detected by the cross-channel signal (4.6) — if all "engagement" is from accounts <30 days old that follow each other, the multiplier collapses.

### 4.3 Velocity
- **Source**: engagement_now − engagement_at_t-60min. Polled per-post on cycle for posts already in DB.
- **Normalization**: `min(1, growth_rate / (3 × baseline))` where baseline = the moving 24h average per source.
- **Compute cost**: One re-fetch per post per cycle for posts <24h old. Bounded.
- **Edge case**: New posts have no baseline. Use category-level baseline as fallback.

### 4.4 SLA proximity
- **Source**: `created_at`, current priority band, SLA target (§9).
- **Formula**: `min(1, age_minutes / sla_minutes)`. Posts past SLA stay at 1.0 (don't go back down).
- **Edge case**: Posts that *aren't* expected to need a reply (`zomato_response_status='not_applicable'`) get sla_proximity = 0. No deadline → no pressure.

### 4.5 Repeat penalty
- **Source**: `handles` table — number of prior complaints from this handle in last 7 days.
- **Formula**: `min(1, (prior_complaints − 1) / 3)`. So 1 prior = 0, 2 prior = 0.33, 4+ prior = 1.0.
- **Edge case**: Bot accounts often have many "prior posts" but they're not repeat *complaints*. Filter by sentiment/topic similarity before counting.

### 4.6 Cross-channel
- **Source**: post_embeddings + handle linking. Same handle on both platforms = direct match. Different handles but high text similarity (>0.85 cosine on embeddings) within last hour = inferred match.
- **Formula**: 1.0 if direct same-handle match, 0.7 if inferred match, 0 otherwise.
- **Edge case**: Coincidental similarity (two unrelated users complaining about a common issue with similar phrasing). Mitigated by the time window (60 min) and embedding threshold.

### 4.7 Author trust
- **Source**: `handles.trust_score` (0–1). Computed from account age, follower-ratio sanity, post frequency, post diversity.
- **Formula**: `(1 − trust_score) × −0.04` (negative contribution for low-trust accounts).
- **Edge case**: Low trust ≠ illegitimate. A throwaway account complaining about food poisoning is still a real signal — the cap on de-prioritization is small (4%) by design.

### 4.8 Counter-narrative offset
- **Source**: replies in the post's thread, classified for sentiment.
- **Formula**: if positive_replies / total_replies > 0.6 AND total_replies ≥ 5 → −0.03. Else 0.
- **Edge case**: Astroturf positive replies (Zomato employees defending the brand without disclosure). Mitigated by trust score on the replier.

---

## 5. Edge case catalog

60 edge cases mapped to handling. Sorted by category. Each has: description, signal it confuses, mitigation.

### 5.1 Content shape

| # | Edge case | Mitigation |
|---|---|---|
| 1 | Sarcastic praise that's actually a complaint | LLM `claim_shape=sarcastic_complaint`, never auto-reply |
| 2 | Ironic / meme content mentioning brand | Format=meme → de-prioritize but keep for trend tracking |
| 3 | Implicit complaint ("I've stopped using Zomato") | LLM `claim_shape=implicit_complaint` — still counts toward attrition signal |
| 4 | Rhetorical question ("does Zomato even care?") | Format=rhetorical, route to PR (narrative risk) not customer-care |
| 5 | Multi-issue post with conflicting urgencies | Sub-claim decomposition; max severity wins overall, but each claim routes separately |
| 6 | Quote-tweet adding NEW commentary on existing complaint | Treated as new post; cross-channel signal links to parent |
| 7 | Reply to Zomato's own tweet (criticism in thread) | Captured by thread-context awareness; routed to PR |
| 8 | Hijacked thread (off-topic complaint piggybacking on viral post) | Clustering detects topic shift; post stays attached to its real topic |
| 9 | Cross-posted between Reddit and Twitter (same content, two posts) | Cross-channel signal merges them in priority computation |
| 10 | Posts entirely in image/video, text minimal | Currently text-only; image/video classification = v2 future-work |

### 5.2 Author identity

| # | Edge case | Mitigation |
|---|---|---|
| 11 | Verified press handle asks an "innocent" question | Author tier overrides everything else — auto-routed to PR with founder-office cc |
| 12 | Politician @-mention | Tripwire forces P0 + founder-office routing |
| 13 | Influencer (>1M) negative review | Author tier-1 multiplier on reach; PR drafts response |
| 14 | Self-declared Zomato employee posting | Tripwire `insider_leak` — routed to HR + legal |
| 15 | Restaurant owner complaining (cross-side) | Side detection flags `merchant`; routes to KAM team, not customer-care |
| 16 | Customer who's also a merchant (dual role) | Posts treated as merchant-side if mentions of restaurant ops; consumer-side otherwise |
| 17 | Anonymous throwaway account with severe complaint | Trust score ≥ 0 floor — never auto-deprioritize legitimate complaints due to anonymity |
| 18 | Suspected bot complaint (high post volume, generic text) | Trust score down-weights ~0.04 max; flagged for review queue |
| 19 | Coordinated bot network (>20 similar posts in 1hr) | Cluster detection + bot-pattern signature → flagged as `coordinated_attack` |
| 20 | Foreign-language post (Tamil/Bengali/Marathi/Kannada) | Multi-lingual LLM classification; routes same as English |

### 5.3 Temporal & lifecycle

| # | Edge case | Mitigation |
|---|---|---|
| 21 | Old tweet (>30d) suddenly going viral | Lifecycle = `archive_resurface`; velocity signal triggers full P0 review |
| 22 | Same handle replied to once, escalating again | Lifecycle = `escalation`; bypass first-line, route to senior support |
| 23 | Resolved-thanks tweet from previously complaining user | Lifecycle = `resolved_thanks`; mine for testimonial |
| 24 | Post deleted by user mid-handling | Soft-delete preserve in DB with `deleted_externally=true`; classification stays |
| 25 | Tweet author suspended mid-handling | Mark `author_unavailable=true`; freeze SLA timer (we can't reply anyway) |
| 26 | Post from before our scraper was online | First-discovery treated normally; lifecycle = `first_mention` regardless of post age |
| 27 | Posts during a known internal outage | Cluster detection flags as ops issue; suppress per-post escalations, single ops alert |
| 28 | Posts during a competitor's outage (Swiggy down) | Spike in our positive sentiment; counter-narrative; mine for marketing |
| 29 | Post timed with a marketing campaign launch | Real-world correlation flag; campaign-team awareness; doesn't change priority |
| 30 | IPL match / festival surge in delivery delays | Real-world correlation flag; ops awareness; SLA may relax temporarily |

### 5.4 Safety & legal sensitivity

| # | Edge case | Mitigation |
|---|---|---|
| 31 | Food poisoning / hospitalization claim | Tripwire P0; safety + legal + founder-office; never auto-reply |
| 32 | Death claim | Tripwire P0; same routing + immediate Slack to founder office |
| 33 | Sexual misconduct allegation against agent | Tripwire P0; legal + safety + DM-only; evidence preservation flag |
| 34 | Religious / caste / gender slur in complaint | Sensitivity tripwire; never auto-reply, manual PR review only |
| 35 | Discrimination claim ("agent refused because of religion") | C10.4 + religious tripwire; legal + PR + safety |
| 36 | Threats against Zomato or staff | Tripwire P0; legal + safety; document for police if escalates |
| 37 | Court / FIR / police complaint mention | Tripwire P0; legal lock — no public engagement until cleared |
| 38 | CCI / regulatory body mention | Tripwire P0; legal + leadership |
| 39 | NRAI / mass-restaurant action | Tripwire P0; PR + leadership |
| 40 | Underage user + alcohol delivery | Tripwire P0; compliance + legal |

### 5.5 Trust & adversarial

| # | Edge case | Mitigation |
|---|---|---|
| 41 | Customer fraud claim ("never delivered" but agent confirmed) | Disputed-incident flag; merchant-ops + trust-safety; both sides get evidence |
| 42 | Restaurant review-bombing (20 fake 1-stars in 1 night) | Cluster detection + trust score; trust-safety + merchant-ops |
| 43 | Extortion threat to merchant | Tripwire P0; legal |
| 44 | Suspected astroturf positive responses | Trust score on responders; counter-narrative weight zeroes out |
| 45 | Coordinated boycott hashtag | Tripwire `boycott_coordinated`; PR war room |
| 46 | Insider leak with screenshots of internal tools | Tripwire `insider_leak`; HR + legal + founder-office; evidence preservation |
| 47 | Defamation claim (we should sue) | Routed to legal only; no public engagement |
| 48 | Counterfeit / impersonation account complaining | Trust-safety to platform takedown channel |
| 49 | Zomato-targeted phishing detected (fake @zomato_care) | Trust-safety alert; user education routed |
| 50 | Manipulated screenshots (Photoshop) presented as evidence | Image forensics is v2; for now, escalate to legal for due diligence |

### 5.6 Operational

| # | Edge case | Mitigation |
|---|---|---|
| 51 | Privacy / PII leak claim | Tripwire P0; legal + trust-safety; DPDP compliance |
| 52 | Geographic mismatch (post mentions Bangalore, author profile shows Delhi) | Author location is hint; post text-mentioned location is authoritative for routing |
| 53 | Time-zone display in non-IST tenant | Always store UTC; render in tenant's preferred timezone |
| 54 | Long thread where 2nd reply has the actual complaint | Thread-aware embedding; classifier reads up to 5 surrounding messages |
| 55 | Same incident mentioned in DM screenshot inside a public tweet | Public tweet is the trigger; DM content is secondary signal |
| 56 | User edits original post after we've classified | Re-classification on next cycle; old version preserved with `version=N+1` |
| 57 | User responds "thanks for resolving" 3 days after our reply | Lifecycle = `resolved_thanks`; close ticket; mine for testimonial |
| 58 | User responds 10 days later "still not resolved" | Lifecycle = `re_flared`; senior recovery path; SLA timer reset |
| 59 | Founder publicly addresses the issue mid-flow | All related posts marked `founder_engaged=true`; pause our auto-handling |
| 60 | A post mentions @zomato but actually means a different "Zomato" (rare, e.g., a restaurant named Zomato) | Disambiguation via brand context — does the post mention delivery / refund / app? Otherwise side=neither |

---

## 6. Dynamic adaptation

Static rules rot. The system must improve continuously. Five mechanisms:

### 6.1 Reviewer correction loop
Every time a human in the review queue disagrees with the classifier (changes the topic, sentiment, urgency, audience):
- The post + corrected label is added to `category_examples` for that leaf
- Next classification batch sees that example as few-shot
- Effect: the classifier gets sharper at *exactly the cases this team finds confusing*, with zero retraining

**Falsifiable test**: classifier accuracy on the held-out set should improve by ≥2% per 100 reviewer corrections, measured monthly.

### 6.2 Drift detection
Weekly job compares:
- Topic distribution this week vs prior 4 weeks
- Confidence distribution
- Auto-action rate
- Reviewer-correction rate

If any axis drifts >2 standard deviations, alert the social-team lead. Could mean: model upgraded, new product launched, new hashtag campaign, or actual seasonality. Either way, human eyes on it.

### 6.3 Weight calibration
The 8 priority-score weights (§3.1) aren't sacred. Quarterly:
- Sample 200 posts that got triaged
- Survey the social team: "in retrospect, was the priority right?"
- For posts where the team disagrees with the score, run a regression: which signals are over- or under-weighted?
- Adjust by 0.01–0.05; never a step-change

This is **opinionated tuning informed by data**, not blind ML. Defensible, slow, stable.

### 6.4 SLA tightening over time
Monthly review of:
- Median response time achieved per priority band
- % of posts that breached SLA
- % that auto-actioned correctly

If band-wide P0 response time is consistently 8 min when target is 5, either: (a) tighten staffing, or (b) loosen target with formal sign-off. Don't let SLAs silently slip.

### 6.5 Taxonomy evolution
Every category in YAML has a `last_used_at` and `total_volume`. Categories with zero volume in 90 days are flagged for retirement. New categories proposed by reviewers (they tag a post as "other" + leave a note) are batched for monthly cross-functional review.

---

## 7. Crisis & viral detection

Single posts lie. Patterns of posts predict the next 90 minutes.

### 7.1 Velocity-baseline alarm

Industry consensus (Brandwatch, Pulsar): **`>3× baseline mention volume in any rolling 60-min window` triggers a crisis review.**

Implementation:
- Per-tenant 24h rolling baseline
- If current 60-min volume > 3× baseline → alert the on-call PR person
- If > 5× → wake the founder office
- Suppression: don't fire repeatedly within a single cluster event

### 7.2 Cluster types we recognize

| Cluster signature | Real-world cause | Routing |
|---|---|---|
| 30+ posts, same topic, same city, 30 min | City-level ops outage (payment, delivery, app) | Ops on-call |
| 20+ posts, same hashtag, mixed cities | Coordinated boycott / activism campaign | PR war room |
| 15+ posts, same restaurant_id mentioned | Restaurant-level safety/quality event | Trust-safety + restaurant-ops |
| 10+ posts citing same news article URL | News-driven reaction wave | PR awareness |
| 5+ posts about same delivery agent | Single-agent misconduct pattern | Trust-safety, watchlist add |
| 3+ posts about same handle complaint | Possible review-bombing | Trust-safety |

### 7.3 Counter-narrative as a signal

When a cluster forms around a complaint topic but a sub-cluster of *positive* messages also forms in the same window, the situation is "self-correcting" — the brand's defenders are showing up organically. Two effects:
- Slightly de-priority new individual posts (the conversation is balancing)
- Mine the positive cluster for marketing-friendly testimonials

### 7.4 Pre-virality predictor (v2)

Pulsar-style prediction: posts with engagement growth rate exceeding their author's historical median by >5x are likely to go viral within 4 hours. Predictive boost — flag at minute 30, not minute 240.

This is an v2 (post-MVP) addition. Requires per-author engagement history.

---

## 8. Author tier system

Identity is reach. A 1M-follower journalist's complaint is operationally a different beast from an anonymous account's.

### 8.1 Tier definitions

| Tier | Definition | Reach multiplier |
|---|---|---|
| **T0 — Authority** | Verified gov/regulator handle, founder/CEO of Fortune 500, head-of-state | 10× + always-P0 routing |
| **T1 — Press** | Verified journalist (curated whitelist), major news handle, anchor | 5× + always founder-office cc |
| **T2 — Influencer** | >1M followers OR verified-blue + niche relevance (food/tech) | 3× |
| **T3 — Power user** | 100K–1M followers OR repeat-engagement track record | 2× |
| **T4 — Active citizen** | 10K–100K, active poster, real profile | 1.5× |
| **T5 — Regular** | <10K followers, real-name profile | 1× (baseline) |
| **T6 — Anonymous regular** | Pseudonymous, low activity | 1× (no penalty for legitimacy) |
| **T7 — Suspected bot** | High volume + low diversity + recent account | 0.3× (de-priority but not zero) |

### 8.2 LTV proxy for consumers

Without order history (we don't have CRM access), we proxy LTV from public signal:
- Mentions of "I order all the time" / "I've been with Zomato since [year]"
- Heavy historical engagement with @zomato (likes, replies)
- Multi-mention pattern over months suggests heavy user

Heavy-user complaints get a 1.3× boost. **Customer-attrition signals are leading indicators of revenue loss.**

### 8.3 Watchlists

Maintained per-tenant:
1. **Press watchlist** — handles to always route via PR
2. **Politician watchlist** — handles to always cc founder office
3. **Repeat-complainer watchlist** — handles whose 4th+ complaint of the same type gets senior CCE
4. **Bad-actor watchlist** — handles flagged for fraud / extortion / harassment of staff
5. **VIP customer watchlist** — high-LTV customers worth retention investment
6. **Restaurant watchlist** — restaurants under elevated quality monitoring
7. **Agent watchlist** — delivery agents under behavioral monitoring

Watchlists are **explicit rules**, not learned. Auditable. Editable by ops.

---

## 9. SLA targets per priority band

Without SLAs, "priority" is meaningless. Industry norms (Typewise, Sprinklr):

| Band | Acknowledge by | Resolve by | Response style |
|---|---|---|---|
| **P0** | 15 min | 1 hour | Senior team; manual; no template |
| **P1** | 30 min | 4 hours | First-line + senior review for sensitive |
| **P2** | 2 hours | 24 hours | Auto-drafted reply, manual approve |
| **P3** | Same day | 48 hours | Auto-drafted, low-touch approval |

(Acknowledge = first DM/reply. Resolve = issue closed-out from user's perspective.)

These are tenant-configurable. A health-conscious DTC food brand might tighten P0 to 5 min. An enterprise B2B might relax P3 to 1 week. Defaults match SaaS industry medians.

---

## 10. Fairness, bias, defensibility

A billion-dollar brand cannot afford to silently de-prioritize legitimate complaints from groups statistically more likely to use anonymous accounts, regional languages, or lower-engagement formats. Three explicit safeguards:

### 10.1 Anonymity is not a penalty
Trust score caps de-prioritization at −0.04 (4% of total score). Anonymous users with severe complaints still surface in P0/P1 if other signals support.

### 10.2 Language-neutral routing
Tamil/Bengali/Hindi/etc. complaints are not de-prioritized. Multi-lingual LLM classification + per-language YAML examples. **Routing should be language-blind; reply drafting should be language-aware.**

### 10.3 Audit log of de-prioritizations
Every score-decreasing signal application is logged with a reason. If a post gets deprioritized, an auditor can ask *which signals contributed how much* and overturn if needed. No silent suppression.

### 10.4 Sensitive-category guardrails
Tripwires for religious/caste/gender content force human review *regardless of priority score*. We never auto-reply on protected-attribute conversations even when the score is low — the cost of getting one wrong is asymmetric.

### 10.5 Monthly bias audit
Sample 100 posts that were de-prioritized to P3. Check distribution:
- % from anonymous handles vs verified
- % in non-English languages
- % from low-LTV indicators

If any class is disproportionately represented in P3 vs the input distribution, investigate. This is a process, not a prediction — guardrails that survive audit.

---

## 11. Validation & quality gates

Three layers of quality measurement:

### 11.1 Classification accuracy (offline)
- Hand-labeled holdout set of 500 posts (refresh quarterly)
- Per-axis accuracy: side ≥97%, topic ≥85%, sentiment ≥85%, urgency ≥80%
- Tripwire recall ≥95%, precision ≥70%
- Calibration: confidence vs actual accuracy should be linearly correlated (slope ≈ 1, R² ≥ 0.85)

### 11.2 Operational quality (online)
- Time-to-acknowledgment per band — target medians
- Response rate per band — target ≥95% for P0/P1
- Auto-action error rate — target <0.1% (any auto-reply that should have been manual)
- Reviewer-correction rate — target trending down month-over-month

### 11.3 Business outcomes (slow but real)
- Per-tenant **NPS shift** for handled-complainants (do they net-promote after we resolve?)
- **Repeat complaint rate** per topic — if "delivery late" complaints don't drop after we ship a fix, the fix isn't real
- **Founder-attention frequency** — how often founder-office gets pinged. Trending down = system is filtering well
- **Cost per resolved complaint** — total operations cost / resolved-thanks count. Should drop as auto-action share rises

---

## 12. What we deliberately decided NOT to build

Out-of-scope for v1, with rationale. (Important — defensibility comes as much from explaining what we *didn't* do as what we did.)

| Thing | Why not | When |
|---|---|---|
| Image / video classification | Text-only is the 80/20. Adds significant model cost and complexity. | After 3 months of text operations |
| Predictive virality model | Requires historical data we don't have | After 6 months of operation |
| Cross-tenant intelligence | Privacy review needed first | Enterprise tier |
| Custom per-tenant ML | YAML-driven taxonomy + LLM is enough until ~5+ tenants | When second customer asks |
| Multi-region deployment | Single-region serves Indian market for v1 | When TAM expands |
| Reply auto-draft tuning per agent | One brand voice config is enough for v1 | When a tenant has multiple personas |
| Image forensics for manipulated screenshots | Niche use case | When legal asks for it |
| Predictive churn on individual customers | Speculative; not actionable | Likely never; this is a CRM job, not a listening job |

---

## 13. Build phasing

In order, with explicit gates:

### Phase α — Priority score scaffolding (1.5 days)
- `priority.py` module with the 8-signal weighted formula
- Stub signals where data is missing (velocity = 0 if we don't have engagement history yet)
- Apply at classification time; persist `priority_score` and `priority_band`
- **Gate**: every post in DB has both fields populated

### Phase β — Urgency dashboard wiring (0.5 day)
- Add `priority_score` column to dashboard table
- Sort by `priority_score DESC` on the urgency view
- Show priority band badge per row (P0/P1/P2/P3)
- Hover row → score breakdown (which signals contributed)
- **Gate**: operator can scan top 10 in 30 seconds and reasonably defend the rank

### Phase γ — Watchlists + author tier (1 day)
- `handles.tier` populated from rules + bio scan
- Watchlist tables with admin UI
- Tier multiplier active in reach signal
- **Gate**: P0 routing fires correctly when a press-watchlist handle posts

### Phase δ — Velocity + cluster detection (1.5 days)
- Per-post engagement re-fetch on cycle (Twitter only for v1)
- Baseline computation, 3× alarm
- Cluster detection job by (side, topic, geo, time-bucket)
- Cluster summary via LLM
- **Gate**: a synthetic 30-post burst triggers a single ops alert, not 30

### Phase ε — Reviewer correction feedback (1 day)
- Review queue UI (corrections happen)
- Corrections feed `category_examples`
- Next-cycle classifier reads new examples
- **Gate**: a deliberately-mislabeled post, corrected by a reviewer, gets correctly classified on its next-cycle re-run

### Phase ζ — Drift detection + weight calibration (0.5 day)
- Weekly drift report (auto-emailed)
- Quarterly weight-tuning playbook (manual; document only)
- **Gate**: drift report runs and emails

### Phase η — Bias audit tooling (0.5 day)
- Monthly job: sample 100 deprioritized posts; bucket by anonymity / language / LTV
- Report posted to a `compliance_audits` table
- **Gate**: report exists; reviewable

**Total**: ~6 engineering days. Each phase is independently shippable; no big bang.

---

*End. Code that implements this design will live in `social_watch/priority/` (Phase α) and `social_watch/clusters/` (Phase δ). Every line traces to a numbered section above.*

---

## Sources

This document is grounded in these real-world references (April 2026):

- [10 Best Social Listening Tools for 2026: Expert Comparison — Pulsar](https://www.pulsarplatform.com/blog/2025/best-social-listening-tools-2026)
- [Brandwatch vs Sprinklr — Gartner Peer Insights](https://www.gartner.com/reviews/market/social-monitoring-and-analytics/compare/brandwatch-vs-sprinklr)
- [Prioritizing Customer Support Tickets: The Complete Method — Typewise](https://www.typewise.app/blog/prioritizing-support-tickets-method) (source of the P0–P3 weighted-scoring framework adopted in §3)
- [Social Media Customer Service — Sprout Social](https://sproutsocial.com/insights/social-media-customer-service/)
- [Ticket triage in customer support — PartnerHero](https://www.partnerhero.com/blog/customer-support-triage)
- [12 Best AI Ticket Routing and Triage Tools for 2026 — Kustomer](https://www.kustomer.com/resources/blog/ai-ticket-triage-tools/)
- [AI in Crisis Management — Prime Tech PR](https://prime-techpr.com/startups/ai-in-crisis-management-how-real-time-detection-and-social-listening-are-transforming-digital-age-response/) (source of the 3× velocity baseline norm in §7)
- [How to Detect a Brand Crisis Before It Goes Viral — Buska](https://www.buska.io/blog/detect-brand-crisis-early)
- [Social Media Crisis Management — Sprinklr](https://www.sprinklr.com/blog/social-media-crisis-management/)
- [Listening In: Social Signal Detection for Crisis Prediction — HICSS 2024](https://aisel.aisnet.org/cgi/viewcontent.cgi?article=1374&context=hicss-57)
