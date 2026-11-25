"""security/approval.py --- approval mode (ask / AI reviews / never ask)

Codex splits sandbox from approval; we do the same:

  security/profiles.py   decides WHAT the agent may do (permission level)
  security/approval.py   decides WHETHER it must ask first, and who answers

The mode lives in one settings key, ``approval_mode``. The old ``auto_approve``
boolean stays as a compatibility alias: when ``approval_mode`` is missing we derive
the mode from it, and whenever the mode is written we write both keys, so the two can
never disagree.

Modes:
  ask   (default) show the confirmation dialog, the user decides
  ai    let the review agent decide; if it cannot, fall back to asking the user
  off   never ask, execute straight away (that is what auto_approve meant)
"""
import logging
import os

_log = logging.getLogger("yuncat.approval")

APPROVAL_ASK = "ask"
APPROVAL_AI = "ai"
APPROVAL_OFF = "off"
MODES = (APPROVAL_ASK, APPROVAL_AI, APPROVAL_OFF)

MODE_SETTING_KEY = "approval_mode"
LEGACY_AUTO_APPROVE_KEY = "auto_approve"
MODE_ENV_VAR = "YUNCAT_APPROVAL_MODE"

# Review verdicts (see services/long_task/final_review_agent.py): proceed / ask_user /
# retry / halt. Only ``proceed`` may stand in for the user; ``ask_user``/``retry`` mean
# "the reviewer is not sure", which must never be silently turned into an approval.
_APPROVE_ACTIONS = frozenset({"proceed"})
_DENY_ACTIONS = frozenset({"halt"})


def normalize_mode(value) -> str:
    """Anything unknown becomes ask (fail-closed: unknown never auto-approves)."""
    text = str(value or "").strip().lower()
    return text if text in MODES else APPROVAL_ASK


def mode_from_settings(settings: dict | None) -> str:
    """Resolve the mode from a settings mapping, with the legacy alias as fallback."""
    if not isinstance(settings, dict):
        return APPROVAL_ASK
    raw = str(settings.get(MODE_SETTING_KEY) or "").strip()
    if raw:
        return normalize_mode(raw)
    return APPROVAL_OFF if str(settings.get(LEGACY_AUTO_APPROVE_KEY, "")).lower() == "true" else APPROVAL_ASK


async def resolve_approval_mode(db) -> str:
    """Read the mode from the settings table; unreadable means ask (fail-closed)."""
    if db is None:
        return current_mode()
    try:
        cursor = await db.execute(
            "SELECT key, value FROM settings WHERE key IN (?, ?)",
            (MODE_SETTING_KEY, LEGACY_AUTO_APPROVE_KEY),
        )
        rows = await cursor.fetchall()
    except Exception:
        _log.warning("\u8bfb\u53d6\u5ba1\u6279\u65b9\u5f0f\u5931\u8d25\uff0c\u6309\u201c\u95ee\u6211\u201d\u5904\u7406", exc_info=False)
        return APPROVAL_ASK
    return mode_from_settings({row[0]: row[1] for row in rows})


def current_mode() -> str:
    """Synchronous read of the active mode (env mirror written by lifespan / route)."""
    return normalize_mode(os.environ.get(MODE_ENV_VAR, APPROVAL_ASK))


def review_outcome(review: dict | None) -> str:
    """Map a review verdict to approve / deny / uncertain.

    ``uncertain`` covers both "no verdict at all" (agent not initialised, error) and
    "reviewer asked the user"; callers must treat it as "ask the human", never as yes.
    """
    if not isinstance(review, dict):
        return "uncertain"
    action = str(review.get("action") or "").strip().lower()
    if action in _DENY_ACTIONS:
        return "deny"
    if action in _APPROVE_ACTIONS and review.get("pass") is not False:
        return "approve"
    return "uncertain"


def describe_mode(mode: str) -> dict:
    """Mode plus what it implies, for routes and the UI."""
    resolved = normalize_mode(mode)
    return {
        "mode": resolved,
        "ask_user": resolved == APPROVAL_ASK,
        "ai_review": resolved == APPROVAL_AI,
        "auto_execute": resolved == APPROVAL_OFF,
    }


async def set_approval_mode(settings_service, mode: str) -> dict:
    """Persist the mode and keep the legacy auto_approve alias in sync."""
    requested = str(mode or "").strip().lower()
    if requested not in MODES:
        raise ValueError(f"unknown approval mode: {mode!r} (expected one of {', '.join(MODES)})")
    await settings_service.save(MODE_SETTING_KEY, requested)
    await settings_service.save(
        LEGACY_AUTO_APPROVE_KEY, "true" if requested == APPROVAL_OFF else "false",
    )
    os.environ[MODE_ENV_VAR] = requested
    return describe_mode(requested)
