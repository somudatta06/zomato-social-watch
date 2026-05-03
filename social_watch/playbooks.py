"""Incident playbooks — what the team must do when specific tripwires fire.

A "channel fired" doesn't mean "incident handled". A Slack message into
a busy channel can be missed; an email can sit in an inbox; a sheet row
is invisible to anyone who isn't auditing. For incidents that can hurt
real people (food safety, alleged death, sexual misconduct) the system
needs a *process*, not just a notification: an owner team, an
acknowledgment SLA, the required steps in priority order, and a guard
against premature public response.

That's what this module owns. One playbook per hard tripwire. The
dispatcher consults it before firing actions; the dashboard surfaces
it on every matching post so the operator sees the procedure inline.

Design intent — what a great PM in this seat would optimize for:
  • Clarity. New ops hire reads any playbook in 10 seconds.
  • Safety. ``block_auto_reply`` stops the bidirectional connectors
    from posting on a tweet that's about a death — no algorithm
    should be authoring corporate replies on those.
  • Auditability. Every ``required_step`` is a checkbox the team can
    point at when reviewing the response post-mortem.
  • Single source of truth. Routing in ``actions/dispatcher.py`` and
    UI rendering in ``dashboard.html`` both read from this map.

Add a playbook by appending to ``PLAYBOOKS`` in priority order
(most-severe first; ``for_post`` picks the first match).
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Playbook definitions
# ---------------------------------------------------------------------------
#
# Order matters: ``for_post`` returns the first matching playbook from the
# dict iteration order (Python 3.7+ preserves insertion order). Most
# dangerous categories come first so a post matching multiple tripwires
# routes to the strictest playbook.

PLAYBOOKS: dict[str, dict[str, Any]] = {

    # ── Loss of life ─────────────────────────────────────────────
    "death_claim": {
        "key":               "death_claim",
        "name":              "Death claim",
        "icon":              "skull",
        "color":             "rose",     # red — hard stop
        "ack_deadline_min":  5,
        "owner_team":        "Trust & Safety",
        "ticket_team":       "T&S",
        "ticket_priority":   1,           # Linear: Urgent
        "banner": (
            "A user is alleging a death linked to Zomato. STOP. "
            "Do not auto-reply. Page T&S lead within 5 minutes. "
            "Loop in legal before any public response."
        ),
        "required_steps": [
            "Acknowledge in #ts-warroom within 5 min — page T&S lead",
            "Loop in legal@zomato.com before any public statement",
            "Open Linear ticket assigned to T&S lead (Urgent)",
            "Pull order metadata: ID, partner, rider, restaurant, time",
            "Mandatory post-incident review within 24 hours",
        ],
        "block_auto_reply":  True,
    },

    # ── Food-borne illness / contamination ───────────────────────
    "food_safety_incident": {
        "key":               "food_safety_incident",
        "name":              "Food safety",
        "icon":              "alert-octagon",
        "color":             "rose",
        "ack_deadline_min":  15,
        "owner_team":        "Quality & Compliance",
        "ticket_team":       "Quality",
        "ticket_priority":   1,
        "banner": (
            "Foreign object, contamination, or illness reported. "
            "Quality team owns response. 15-min acknowledgment required."
        ),
        "required_steps": [
            "Acknowledge in #food-safety within 15 min",
            "Pull restaurant from rotation (manual flag)",
            "Open Linear ticket assigned to Quality lead",
            "Request photo evidence + order ID via DM",
            "FSSAI report if contamination is confirmed",
        ],
        "block_auto_reply":  False,  # safe to ask for DM publicly
    },

    # ── Sexual misconduct allegation ─────────────────────────────
    "sexual_misconduct": {
        "key":               "sexual_misconduct",
        "name":              "Sexual misconduct",
        "icon":              "alert-triangle",
        "color":             "rose",
        "ack_deadline_min":  10,
        "owner_team":        "Trust & Safety + HR",
        "ticket_team":       "T&S",
        "ticket_priority":   1,
        "banner": (
            "Allegation of sexual misconduct by a delivery partner / "
            "merchant / staff member. T&S + HR own. Mandatory legal review."
        ),
        "required_steps": [
            "Page T&S + HR in #ts-warroom within 10 min",
            "Suspend delivery partner pending investigation",
            "Open Linear ticket assigned to T&S lead",
            "Offer victim support resources via private DM",
            "Mandatory legal review before any public statement",
        ],
        "block_auto_reply":  True,
    },

    # ── PII / data leak ──────────────────────────────────────────
    "privacy_data_leak": {
        "key":               "privacy_data_leak",
        "name":              "Privacy / data leak",
        "icon":              "shield-alert",
        "color":             "rose",
        "ack_deadline_min":  10,
        "owner_team":        "InfoSec",
        "ticket_team":       "InfoSec",
        "ticket_priority":   1,
        "banner": (
            "Possible PII exposure or data leak. InfoSec owns response. "
            "Do not confirm or deny publicly until investigation."
        ),
        "required_steps": [
            "Page InfoSec lead in #security within 10 min",
            "Open Linear ticket assigned to InfoSec",
            "Loop in legal@ for breach notification assessment (DPDP/GDPR)",
            "Audit access logs for the affected timeframe",
            "Hold all public response until scope is confirmed",
        ],
        "block_auto_reply":  True,
    },

    # ── Court / FIR / consumer-court threat ──────────────────────
    "court_fir_legal": {
        "key":               "court_fir_legal",
        "name":              "Legal threat",
        "icon":              "scale",
        "color":             "amber",
        "ack_deadline_min":  30,
        "owner_team":        "Legal",
        "ticket_team":       "Legal",
        "ticket_priority":   2,           # High
        "banner": (
            "User has invoked court / FIR / consumer-court. Legal team owns "
            "response. NO public reply without legal sign-off."
        ),
        "required_steps": [
            "Email legal@zomato.com + founder-office@ within 30 min",
            "Open Linear ticket assigned to legal counsel",
            "Preserve all message history (audit log) — do not delete",
            "Do NOT engage publicly until legal approves the response",
        ],
        "block_auto_reply":  True,
    },

    # ── Regulatory / CCI / anti-competitive claim ────────────────
    "anti_competitive_regulatory": {
        "key":               "anti_competitive_regulatory",
        "name":              "Regulatory",
        "icon":              "landmark",
        "color":             "amber",
        "ack_deadline_min":  60,
        "owner_team":        "Legal + Policy",
        "ticket_team":       "Legal",
        "ticket_priority":   2,
        "banner": (
            "Anti-competitive / CCI / regulatory threat raised. "
            "Legal + Policy own. Preserve evidence; do not engage publicly."
        ),
        "required_steps": [
            "Email legal@ + policy@ within 60 min",
            "Open Linear ticket assigned to general counsel",
            "Preserve evidence — screenshots, timestamps, full thread",
            "Hold all public response until policy review",
        ],
        "block_auto_reply":  True,
    },

    # ── Discrimination / sensitive incident ──────────────────────
    "religious_caste_gender_sensitivity": {
        "key":               "religious_caste_gender_sensitivity",
        "name":              "Sensitive — discrimination",
        "icon":              "users",
        "color":             "amber",
        "ack_deadline_min":  30,
        "owner_team":        "PR + T&S",
        "ticket_team":       "PR",
        "ticket_priority":   2,
        "banner": (
            "Religious / caste / gender discrimination claim. "
            "PR + T&S must align on response. Senior approval required."
        ),
        "required_steps": [
            "Align PR + T&S in #pr-warroom within 30 min",
            "Open Linear ticket assigned to PR lead",
            "Draft public response — requires senior approval",
            "If credible, suspend the partner and open separate T&S case",
        ],
        "block_auto_reply":  True,
    },

    # ── Insider leak ─────────────────────────────────────────────
    "insider_leak": {
        "key":               "insider_leak",
        "name":              "Insider leak",
        "icon":              "user-x",
        "color":             "amber",
        "ack_deadline_min":  30,
        "owner_team":        "InfoSec + HR",
        "ticket_team":       "InfoSec",
        "ticket_priority":   2,
        "banner": (
            "Possible employee leak of internal information. "
            "InfoSec + HR investigate."
        ),
        "required_steps": [
            "Page InfoSec + HR in #security within 30 min",
            "Audit recent access logs for the leaked content",
            "Open Linear ticket assigned to InfoSec",
            "Hold public response until source is identified",
        ],
        "block_auto_reply":  True,
    },
}


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def for_tripwires(tripwires_fired: list[str] | None) -> dict[str, Any] | None:
    """Pick the most severe playbook that matches a list of fired tripwires.

    Order of severity is the dict insertion order in ``PLAYBOOKS``
    (death first, insider leak last). Returns ``None`` if no playbook
    applies (the post is just a routine complaint).
    """
    if not tripwires_fired:
        return None
    fired_set = set(tripwires_fired)
    for key, pb in PLAYBOOKS.items():
        if key in fired_set:
            return pb
    return None


def for_post(classification: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convenience wrapper around ``for_tripwires`` that takes the
    classification dict directly."""
    if not classification:
        return None
    return for_tripwires(classification.get("tripwires_fired") or [])


def block_auto_reply(classification: dict[str, Any] | None) -> bool:
    """Should auto-reply (twitter_reply, reddit_comment) be blocked for
    this post? True if a matching playbook says so.

    Manual triggers from the dashboard can still fire — but the row's
    Reply button shows a confirmation modal that surfaces the playbook
    banner ('Death claim — page legal before replying').
    """
    pb = for_post(classification)
    return bool(pb and pb.get("block_auto_reply"))


# ---------------------------------------------------------------------------
# Sanity tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cases: list[tuple[list[str] | None, str | None]] = [
        (None,                              None),
        ([],                                None),
        (["payment_charged_no_delivery"],   None),     # no hard tripwire matched
        (["death_claim"],                   "Death claim"),
        (["food_safety_incident"],          "Food safety"),
        (["court_fir_legal", "death_claim"], "Death claim"),  # death wins
        (["privacy_data_leak"],             "Privacy / data leak"),
    ]
    fail = 0
    for fired, want in cases:
        pb = for_tripwires(fired)
        got = pb["name"] if pb else None
        ok = got == want
        if not ok:
            fail += 1
        print(f"{'PASS' if ok else 'FAIL'}: for_tripwires({fired}) -> {got}  (want {want})")

    # Auto-reply guard
    block_cases = [
        ({"tripwires_fired": ["death_claim"]},          True),
        ({"tripwires_fired": ["food_safety_incident"]}, False),  # explicitly allowed (DM ask)
        ({"tripwires_fired": []},                       False),
        (None,                                          False),
    ]
    for cls, want in block_cases:
        got = block_auto_reply(cls)
        ok = got == want
        if not ok:
            fail += 1
        print(f"{'PASS' if ok else 'FAIL'}: block_auto_reply({cls}) -> {got}  (want {want})")

    print(f"\n{'all good' if fail == 0 else f'{fail} failures'}")
    raise SystemExit(0 if fail == 0 else 1)
