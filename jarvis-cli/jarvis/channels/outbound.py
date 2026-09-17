"""Sending a DM to the owner — the "Jarvis tells you something" direction.

Used by three callers:
  * the `notify_owner` tool, so Jarvis can say "that finished" on its own
  * notifier.py's new `discord`/`instagram` channels, so scheduled jobs and
    reminders reach a phone instead of a toast on a PC nobody is at
  * `jarvis channels-test`, to prove the wiring works before relying on it

WHY THIS IS BEST-EFFORT AND SAYS WHY IT FAILED
----------------------------------------------
The two platforms fail in completely different, completely normal ways:

  * Discord refuses if the owner doesn't share a server with the bot, or
    has "allow DMs from server members" off.
  * Instagram refuses whenever the owner hasn't messaged the bot in the
    last 24 hours — which is most of the time, by design, and not fixable
    from this side (see instagram_gateway.py's module docstring).

So a failure here is an expected steady state, not an exception. Every
function returns (ok, detail) with a human-readable reason, and
notify_owner() tries platforms in order and reports each one's outcome
rather than collapsing to a single bool. The notifier's durable inbox is
always written regardless, so a notification that couldn't be DMed is
never a notification that vanished.
"""

import asyncio
import sys

from . import DISCORD, INSTAGRAM, PLATFORMS
from . import config as channel_config


def _owner_of(cfg):
    return str((cfg or {}).get("owner") or "").strip().lstrip("@")


def dm_owner_discord(text, cfg=None):
    cfg = cfg or channel_config.platform_config(DISCORD)
    if not cfg.get("enabled"):
        return False, "discord channel is disabled"
    owner = _owner_of(cfg)
    if not owner:
        return False, "no discord owner configured (channels.json > discord.owner)"
    if not owner.isdigit():
        # fetch_user() takes a snowflake. A username can't be resolved to one
        # without the privileged members intent plus a shared guild, so this
        # is a config error worth naming precisely rather than a lookup to
        # attempt and fail at confusingly.
        return False, (f"discord owner '{owner}' is not a numeric user id. "
                       "Right-click your name > Copy User ID (Developer Mode on), "
                       "or run `jarvis channels-whoami`.")
    token = str(cfg.get("bot_token") or "").strip()
    if not token:
        return False, "no discord bot_token configured"

    from . import discord_gateway
    discord, err = discord_gateway.import_discord()
    if discord is None:
        return False, err

    try:
        return asyncio.run(
            discord_gateway.send_dm(discord, token, owner, text, cfg=cfg))
    except RuntimeError as exc:
        # asyncio.run() refuses to nest. A caller already inside a loop
        # should await discord_gateway.send_dm directly.
        return False, f"could not start an event loop: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def dm_owner_instagram(text, cfg=None):
    cfg = cfg or channel_config.platform_config(INSTAGRAM)
    if not cfg.get("enabled"):
        return False, "instagram channel is disabled"
    owner = _owner_of(cfg)
    if not owner:
        return False, "no instagram owner configured (channels.json > instagram.owner)"

    from . import instagram_gateway
    open_now, remaining = instagram_gateway.window_open_for(owner)
    if not open_now:
        return False, (
            "instagram's 24-hour messaging window is closed for the owner. "
            "Instagram does not allow a bot to open a conversation — DM the "
            "bot once from the owner account to reopen it.")
    ok, err = instagram_gateway.send_message(owner, text, cfg=cfg)
    if ok:
        hours = remaining // 3600
        return True, f"sent (window closes in ~{hours}h)"
    return False, err


_SENDERS = {
    DISCORD: dm_owner_discord,
    INSTAGRAM: dm_owner_instagram,
}


def dm_owner(platform, text):
    """Send to one platform's owner. Returns (ok, detail)."""
    sender = _SENDERS.get(platform)
    if sender is None:
        return False, f"unknown platform '{platform}'"
    if not (text or "").strip():
        return False, "empty message"
    try:
        return sender(text)
    except Exception as exc:  # noqa: BLE001 — never let delivery raise
        return False, str(exc)


def notify_owner(text, platforms=None, first_success_only=True):
    """Try to reach the owner, returning per-platform outcomes.

    `first_success_only` stops after one platform works, which is the right
    default for "your download finished" — two copies of the same
    notification on two apps is noise. Pass False for something important
    enough to want redundantly delivered.

    Returns {"ok": bool, "results": [{platform, ok, detail}, ...]}.
    """
    wanted = list(platforms or PLATFORMS)
    cfg = channel_config.load_config()
    results = []
    any_ok = False

    for platform in wanted:
        block = cfg.get(platform) or {}
        if not block.get("enabled"):
            results.append({"platform": platform, "ok": False,
                            "detail": "channel disabled"})
            continue
        ok, detail = dm_owner(platform, text)
        results.append({"platform": platform, "ok": ok, "detail": detail})
        if ok:
            any_ok = True
            if first_success_only:
                break

    if not any_ok:
        # Loud, because a silent failure here means the user believes they
        # were notified about something and were not.
        for item in results:
            print(f"[channels] owner DM via {item['platform']} failed: "
                  f"{item['detail']}", file=sys.stderr, flush=True)
    return {"ok": any_ok, "results": results}


def status():
    """Per-platform readiness, for `jarvis channels-status` and the web UI.
    Reports what would happen *without* sending anything."""
    cfg = channel_config.load_config()
    out = []
    for platform in PLATFORMS:
        block = cfg.get(platform) or {}
        owner = _owner_of(block)
        item = {
            "platform": platform,
            "enabled": bool(block.get("enabled")),
            "owner": owner,
            "can_send": False,
            "detail": "",
        }
        if not block.get("enabled"):
            item["detail"] = "disabled"
        elif not owner:
            item["detail"] = "no owner configured"
        elif platform == DISCORD:
            from . import discord_gateway
            _, err = discord_gateway.import_discord()
            if err:
                item["detail"] = err.splitlines()[0]
            elif not (block.get("bot_token") or "").strip():
                item["detail"] = "no bot_token"
            else:
                item["can_send"] = True
                item["detail"] = "ready"
        elif platform == INSTAGRAM:
            from . import instagram_gateway
            open_now, remaining = instagram_gateway.window_open_for(owner)
            if not (block.get("access_token") or "").strip():
                item["detail"] = "no access_token"
            elif not open_now:
                item["detail"] = "24h window closed — owner must DM the bot first"
            else:
                item["can_send"] = True
                item["detail"] = f"ready (~{remaining // 3600}h of window left)"
        out.append(item)
    return out
