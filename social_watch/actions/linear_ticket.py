"""Linear ticket connector — creates an Issue in Linear for every P0 post.

Closes the brief's "≥1 real action per escalated post" requirement on
the *workflow* axis. Slack tells you "wake up", Sheets gives you an
audit log, Email tells the team — but Linear tickets are where work
actually gets *assigned, tracked, and closed*. A great PM treats the
ticket as the durable artifact: a Slack message disappears, a ticket
lives until the issue is resolved.

Setup (one-time, ~2 minutes):
    1. Visit https://linear.app/settings/api → "Create new" personal
       API key. Name it "Zomato Social Watch".
    2. Find your team ID: any team's URL is
       ``https://linear.app/<workspace>/team/<TEAM_KEY>/...``. The
       LINEAR_TEAM_ID we want is the *internal UUID*, not the key.
       Get it via:
           curl -X POST https://api.linear.app/graphql \\
             -H "Authorization: <api-key>" \\
             -H "Content-Type: application/json" \\
             -d '{"query":"{ teams { nodes { id key name } } }"}'
       Copy the ``id`` of the team you want tickets created in.
    3. Paste into ``.env``:
           LINEAR_API_KEY=<your-personal-api-key>
           LINEAR_TEAM_ID=<the UUID from step 2>
    4. Restart the dashboard. P0 posts will create tickets automatically.

Design notes:
    * One GraphQL mutation, no library — Linear's API is small.
    * Priority maps from our P0..P3 to Linear's 1..4 (Urgent..Low).
    * Tickets fire on P0 only (auto-fire). The narrative: not every
      P1 post needs a Linear ticket, but every P0 absolutely does.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

# ============================================================
# Config
# ============================================================

LINEAR_API_KEY_ENV = "LINEAR_API_KEY"
LINEAR_TEAM_ID_ENV = "LINEAR_TEAM_ID"
# Per-team routing maps. Both env vars are JSON: {"<owner_team>": "<uuid>"}.
# When a post matches a playbook (e.g. ``Trust & Safety``) we route the
# Linear issue to that team's queue and assign the on-call owner.
# Falls back to the default team / no assignee if a key isn't mapped.
LINEAR_TEAMS_ENV = "LINEAR_TEAMS"
LINEAR_ASSIGNEES_ENV = "LINEAR_ASSIGNEES"
DASHBOARD_BASE_ENV = "DASHBOARD_BASE_URL"
DEFAULT_DASHBOARD_BASE = "http://localhost:8000"

_API_URL = "https://api.linear.app/graphql"
_SEND_TIMEOUT_S = 15.0

# Body truncation — Linear's description is markdown; we keep it
# short enough to glance through.
_DESC_POST_TRUNCATE = 1500

# Priority map: ours → Linear's. Linear uses:
#   0 = No priority, 1 = Urgent, 2 = High, 3 = Medium, 4 = Low
# We ship P0 → 1 (Urgent) when a hard tripwire fired, else 2 (High).
def _linear_priority(band: str, tripwires_fired: list[str], hard_tripwires: set[str]) -> int:
    band_u = (band or "").upper()
    if band_u == "P0":
        return 1 if (set(tripwires_fired or []) & hard_tripwires) else 2
    if band_u == "P1":
        return 2
    if band_u == "P2":
        return 3
    return 4


# ============================================================
# Helpers
# ============================================================

def api_key() -> str | None:
    v = (os.getenv(LINEAR_API_KEY_ENV) or "").strip()
    return v or None


def team_id() -> str | None:
    v = (os.getenv(LINEAR_TEAM_ID_ENV) or "").strip()
    return v or None


def _parse_routing_map(env_var: str) -> dict[str, str]:
    """Parse a JSON env var of the shape ``{"<team>": "<uuid>"}`` into a
    dict. Returns ``{}`` on missing/invalid env (the caller falls back
    to defaults). Tolerant: malformed JSON logs a warning, never raises.
    """
    raw = (os.getenv(env_var) or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if v}
    except Exception as e:
        logger.warning(f"[linear_ticket] {env_var} is not valid JSON: {e}")
    return {}


def team_id_for(playbook_team: str | None) -> str | None:
    """Map a playbook's owner-team name (e.g. ``"Trust & Safety"``) to a
    Linear team UUID. Falls back to the default ``LINEAR_TEAM_ID`` if
    no per-team override is configured.
    """
    if playbook_team:
        teams = _parse_routing_map(LINEAR_TEAMS_ENV)
        # Try the exact team name first, then a normalized lowercase key.
        if playbook_team in teams:
            return teams[playbook_team]
        norm = playbook_team.lower().replace(" & ", "_").replace(" ", "_").replace("+", "_plus_")
        if norm in teams:
            return teams[norm]
    return team_id()


def assignee_id_for(playbook_team: str | None) -> str | None:
    """Map a playbook's owner-team to a Linear user UUID (the on-call
    person who picks up the ticket). Returns None when no assignee is
    configured — Linear leaves the ticket unassigned, which is fine."""
    if not playbook_team:
        return None
    assignees = _parse_routing_map(LINEAR_ASSIGNEES_ENV)
    if playbook_team in assignees:
        return assignees[playbook_team]
    norm = playbook_team.lower().replace(" & ", "_").replace(" ", "_").replace("+", "_plus_")
    return assignees.get(norm)


def webhook_url() -> str | None:
    """Sentinel for the dispatcher's truthiness check. Returns the API
    key (truthy when configured) so this module slots into _CHANNEL_MODS
    the same way Slack/Discord/Email/Sheets do."""
    if api_key() and team_id():
        return "linear-configured"
    return None


def is_configured() -> bool:
    return webhook_url() is not None


def _truncate(text: str, n: int) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _topic_label(classification: dict[str, Any]) -> str:
    primary = classification.get("primary_topic")
    if primary and isinstance(primary, str):
        return primary.replace("_", " ").strip().title()
    cat = classification.get("category") or classification.get("side")
    if cat:
        return f"{cat.title()} issue"
    return "Critical Mention"


# ============================================================
# Issue payload builder (pure — no I/O, easy to test)
# ============================================================

# We mirror the dispatcher's hard-tripwire set so the priority decision
# stays in lockstep when the routing rules change. Defined locally to
# keep this module independent.
_HARD_SLACK_TRIPWIRES: set[str] = {
    "food_safety_incident",
    "death_claim",
    "sexual_misconduct",
    "court_fir_legal",
    "religious_caste_gender_sensitivity",
    "privacy_data_leak",
    "anti_competitive_regulatory",
    "insider_leak",
}


def build_issue_payload(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any],
    *,
    dashboard_base: str | None = None,
) -> dict[str, Any]:
    """Produce the GraphQL ``input`` dict for the issueCreate mutation.

    Returns the full input shape Linear expects::

        { teamId, title, description, priority, assigneeId? }

    Pure: no I/O. Reads the LINEAR_TEAMS / LINEAR_ASSIGNEES routing
    maps so a death-claim post lands in the T&S team's queue assigned
    to the T&S on-call, while a generic ``customer-care`` post goes to
    the default team unassigned.
    """
    band = (priority_breakdown.get("band") or post.get("priority_band") or "P0").upper()
    topic = _topic_label(classification)
    author = (post.get("author") or "anonymous").lstrip("@")
    src = (post.get("source") or "?").lower()
    url = post.get("url") or ""

    # Pull the matching playbook (if any) — drives team + assignee routing
    # and lets us mention the owner team in the title for visibility.
    try:
        from .. import playbooks
        pb = playbooks.for_post(classification) or {}
    except Exception:
        pb = {}
    owner_team = pb.get("owner_team")  # e.g. "Trust & Safety"
    pb_name    = pb.get("name")        # e.g. "Death claim"

    if pb_name:
        title = f"[{band}] {pb_name}: {topic} — @{author} on {src}"
    else:
        title = f"[{band}] {topic} — @{author} on {src}"
    title = _truncate(title, 240)

    base = (dashboard_base or os.getenv(DASHBOARD_BASE_ENV) or DEFAULT_DASHBOARD_BASE).rstrip("/")
    dashboard_link = f"{base}/inbox?q={quote(str(post.get('id') or ''), safe='')}"

    audience = classification.get("audience") or []
    audience_str = ", ".join(str(a) for a in audience) if audience else "(unrouted)"

    score = priority_breakdown.get("score")
    score_str = f"{float(score):.2f}" if score is not None else "—"

    fired = classification.get("tripwires_fired") or []
    fired_str = ", ".join(fired) if fired else "(none)"

    reason = priority_breakdown.get("reason") or classification.get("reasoning") or "—"

    content = _truncate(post.get("content") or "", _DESC_POST_TRUNCATE)

    # Playbook section — if a hard tripwire fired, surface the procedure
    # in the ticket body so the assignee sees the SLA + steps without
    # leaving Linear.
    pb_section = ""
    if pb:
        steps = pb.get("required_steps") or []
        steps_md = "\n".join(f"- [ ] {s}" for s in steps)
        pb_section = (
            f"\n---\n\n"
            f"### 📜 Incident playbook — {pb_name}\n\n"
            f"**Owner:** {owner_team}  ·  **Ack SLA:** {pb.get('ack_deadline_min')} min  "
            f"·  **Auto-reply blocked:** {'yes' if pb.get('block_auto_reply') else 'no'}\n\n"
            f"> {pb.get('banner', '')}\n\n"
            f"**Required steps**\n{steps_md}\n"
        )

    description_md = (
        f"**Source:** [{src.title()}]({url}) · @{author}\n"
        f"**Audience routing:** {audience_str}\n"
        f"**Priority score:** `{score_str}` · band **{band}**\n"
        f"**Tripwires fired:** {fired_str}\n"
        + (f"**Owner team:** {owner_team}\n" if owner_team else "")
        + f"\n---\n\n"
        f"### Post\n\n"
        f"> {content}\n\n"
        f"---\n\n"
        f"### Why this is {band}\n\n"
        f"{reason}\n"
        + pb_section
        + f"\n---\n\n"
        f"[Open in Social Watch dashboard]({dashboard_link})"
    )

    payload: dict[str, Any] = {
        "teamId": team_id_for(owner_team) or "",
        "title": title,
        "description": description_md,
        "priority": pb.get("ticket_priority") or _linear_priority(band, fired, _HARD_SLACK_TRIPWIRES),
    }
    # Optional assignee — Linear is happy to accept the field omitted.
    aid = assignee_id_for(owner_team)
    if aid:
        payload["assigneeId"] = aid
    return payload


# ============================================================
# GraphQL sender
# ============================================================

_MUTATION = """
mutation IssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue {
      id
      identifier
      url
      title
    }
  }
}
""".strip()


async def create_issue(
    input_payload: dict[str, Any],
    *,
    key: str | None = None,
) -> dict[str, Any]:
    """POST the issueCreate mutation to Linear's GraphQL API.

    Always returns a dict (never raises). Matches the connector
    contract used by slack/email/sheets/discord/twitter/reddit.

    Returns:
        {
          "ok": bool,
          "status": int,
          "ts": iso8601,
          "error": str | None,
          "issue_url":        str | None,
          "issue_identifier": str | None,   # e.g. "ZOM-42"
        }
    """
    sent_at = datetime.now(timezone.utc).isoformat()
    auth = key or api_key()
    if not auth:
        return {"ok": False, "status": 0, "ts": sent_at,
                "error": f"{LINEAR_API_KEY_ENV} is not set",
                "issue_url": None, "issue_identifier": None}
    if not input_payload.get("teamId"):
        return {"ok": False, "status": 0, "ts": sent_at,
                "error": f"{LINEAR_TEAM_ID_ENV} is not set",
                "issue_url": None, "issue_identifier": None}

    headers = {
        "Authorization": auth,
        "Content-Type": "application/json",
    }
    body = {
        "query": _MUTATION,
        "variables": {"input": input_payload},
    }
    try:
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_S) as client:
            resp = await client.post(_API_URL, headers=headers, json=body)
        if resp.status_code != 200:
            return {"ok": False, "status": resp.status_code, "ts": sent_at,
                    "error": f"linear returned {resp.status_code}: {resp.text[:240]}",
                    "issue_url": None, "issue_identifier": None}
        try:
            data = resp.json()
        except Exception as e:
            return {"ok": False, "status": resp.status_code, "ts": sent_at,
                    "error": f"linear response not json: {e}",
                    "issue_url": None, "issue_identifier": None}
        # GraphQL errors live in top-level ``errors`` array.
        if data.get("errors"):
            return {"ok": False, "status": resp.status_code, "ts": sent_at,
                    "error": f"linear graphql errors: {json.dumps(data['errors'])[:240]}",
                    "issue_url": None, "issue_identifier": None}
        wrap = (data.get("data") or {}).get("issueCreate") or {}
        if not wrap.get("success"):
            return {"ok": False, "status": resp.status_code, "ts": sent_at,
                    "error": f"linear issueCreate.success=false; full: {json.dumps(data)[:240]}",
                    "issue_url": None, "issue_identifier": None}
        issue = wrap.get("issue") or {}
        return {
            "ok": True, "status": 200, "ts": sent_at, "error": None,
            "issue_url": issue.get("url"),
            "issue_identifier": issue.get("identifier"),
        }
    except httpx.TimeoutException as e:
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"timeout: {e}",
                "issue_url": None, "issue_identifier": None}
    except httpx.HTTPError as e:
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"httpx_error: {e}",
                "issue_url": None, "issue_identifier": None}
    except Exception as e:  # pragma: no cover
        logger.exception("[linear_ticket] unexpected send error")
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"{type(e).__name__}: {e}",
                "issue_url": None, "issue_identifier": None}


# ============================================================
# Auth-only check used by smoke --live
# ============================================================

_VIEWER_QUERY = "{ viewer { id name email } }"


async def auth_check() -> dict[str, Any]:
    """Cheap GraphQL call (``viewer``) to verify the API key works.
    Used by the smoke harness — we don't want to actually create a
    ticket for a smoke run."""
    sent_at = datetime.now(timezone.utc).isoformat()
    auth = api_key()
    if not auth:
        return {"ok": False, "status": 0, "ts": sent_at, "error": "LINEAR_API_KEY not set"}
    if not team_id():
        return {"ok": False, "status": 0, "ts": sent_at, "error": "LINEAR_TEAM_ID not set"}
    headers = {"Authorization": auth, "Content-Type": "application/json"}
    body = {"query": _VIEWER_QUERY}
    try:
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_S) as client:
            resp = await client.post(_API_URL, headers=headers, json=body)
        if resp.status_code != 200:
            return {"ok": False, "status": resp.status_code, "ts": sent_at,
                    "error": f"linear returned {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        if data.get("errors"):
            return {"ok": False, "status": resp.status_code, "ts": sent_at,
                    "error": f"graphql errors: {data['errors']}"}
        viewer = (data.get("data") or {}).get("viewer") or {}
        return {"ok": True, "status": 200, "ts": sent_at, "error": None,
                "detail": f"authenticated as {viewer.get('name') or viewer.get('email') or '?'}"}
    except Exception as e:
        return {"ok": False, "status": 0, "ts": sent_at, "error": f"{type(e).__name__}: {e}"}


# ============================================================
# Convenience — matches the dispatcher contract
# ============================================================

async def build_and_send(
    post: dict[str, Any],
    classification: dict[str, Any],
    priority_breakdown: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = build_issue_payload(post, classification, priority_breakdown)
    result = await create_issue(payload)
    return payload, result


__all__ = [
    "build_issue_payload",
    "create_issue",
    "auth_check",
    "build_and_send",
    "webhook_url",
    "is_configured",
    "api_key",
    "team_id",
    "LINEAR_API_KEY_ENV",
    "LINEAR_TEAM_ID_ENV",
]


# ============================================================
# Sanity test — pure (no network)
# ============================================================
# Run with: python -m social_watch.actions.linear_ticket
# Verifies: priority mapping + payload shape.

if __name__ == "__main__":
    cases = [
        # (band, tripwires_fired, expected_linear_priority)
        ("P0", ["food_safety_incident"], 1),
        ("P0", [],                       2),
        ("P1", [],                       2),
        ("P2", [],                       3),
        ("P3", [],                       4),
        ("",   [],                       4),  # unknown band → Low
    ]
    fail = 0
    for band, fired, expected in cases:
        got = _linear_priority(band, fired, _HARD_SLACK_TRIPWIRES)
        ok = got == expected
        if not ok:
            fail += 1
        print(f"{'PASS' if ok else 'FAIL'}: priority({band!r}, {fired!r}) -> {got}  (want {expected})")

    sample_post = {
        "id": "twitter:42", "source": "twitter", "author": "@angryuser",
        "content": "Food never came, support ghosted me. ₹890 charged. #zomatofail",
        "url": "https://x.com/angryuser/status/42",
        "priority_band": "P0",
    }
    sample_cls = {
        "primary_topic": "missing_order_refund",
        "audience": ["customer-care", "trust-and-safety"],
        "tripwires_fired": ["food_safety_incident"],
        "reasoning": "Charged + no delivery + no support response = trust risk.",
    }
    sample_pri = {"score": 0.91, "band": "P0", "reason": "Auto-escalated: payment without fulfillment"}

    # team_id may be empty in dev — that's fine; build_issue_payload still
    # produces a payload, create_issue will reject it with a clear error.
    os.environ.setdefault("LINEAR_TEAM_ID", "TEST-TEAM-UUID")
    payload = build_issue_payload(sample_post, sample_cls, sample_pri)
    must_haves = ["teamId", "title", "description", "priority"]
    for k in must_haves:
        if k not in payload:
            fail += 1
            print(f"FAIL: payload missing {k!r}")
        else:
            print(f"PASS: payload has {k!r} = {str(payload[k])[:70]}{'…' if len(str(payload[k]))>70 else ''}")
    if payload["priority"] != 1:
        fail += 1
        print(f"FAIL: payload priority for P0+tripwire should be 1 (Urgent); got {payload['priority']}")
    else:
        print("PASS: payload priority for P0+hard-tripwire is 1 (Urgent)")

    print(f"\n{'all good' if fail == 0 else f'{fail} failures'}")
    raise SystemExit(0 if fail == 0 else 1)
