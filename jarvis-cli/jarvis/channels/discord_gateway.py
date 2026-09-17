"""Discord gateway — `jarvis discord-daemon`.

A long-running foreground process holding a WebSocket to Discord, same
shape as sched_daemon.py: backgrounding is the caller's job (nohup,
systemd, NSSM, a Task Scheduler entry), and a PID file stops a second one
from starting.

NO PRIVILEGED INTENT IS REQUIRED, AND THAT IS NOT AN ACCIDENT
-------------------------------------------------------------
Discord made message content a privileged intent in September 2022. An app
without it receives empty `content` on message events — which would make a
chat assistant useless. Except Discord carved out two exemptions, and they
are exactly the two cases this integration handles:

    "These restrictions do not apply for messages that a bot or app sends,
     in DMs that it receives, or in messages in which it is mentioned."
                       — discord/discord-api-docs#5412

Discord still says the same thing in 2026 when rejecting intent
applications: bots can keep using @mentions, replies and DMs for user
interaction without the Message Content Intent.

So "only answer when @mentioned, or when DM'd by an allowlisted user" —
the requirement this whole package is built around — happens to be
precisely the set of messages whose content arrives anyway. The intent
stays OFF by default. The practical payoff: nothing to tick in the
Developer Portal, nothing to justify to Discord review, and no breakage at
100 servers where privileged intents start needing approval.

`message_content_intent: true` in the config turns it on anyway, for
someone who wants prefix commands or passive channel reading later. It is
opt-in because switching it on means the bot starts receiving the text of
every message in every channel it can see, which is a real privacy change
for everyone in that server and should be a deliberate act.

REQUIRED GATEWAY INTENTS (all non-privileged)
    guilds            — know what servers/channels exist
    guild_messages    — receive message events in servers
    dm_messages       — receive DMs
"""

import asyncio
import os
import re
import signal
import sys
from pathlib import Path

from . import DISCORD
from . import config as channel_config
from . import base, permissions, transcript, directory

PID_FILE = Path.home() / ".jarvis" / "discord_daemon.pid"

# <@123>, <@!123> (legacy nickname form), <@&123> (role). Stripped from the
# text before it reaches the model — "<@1234567890> what's my battery" is
# noise that costs tokens and invites the model to echo a raw id back.
_MENTION_RE = re.compile(r"<@[!&]?(\d+)>")

_stop = False


def _handle_stop(signum, frame):  # pragma: no cover — signal path
    global _stop
    _stop = True


def import_discord():
    """Import discord.py, or return (None, helpful message).

    Separated so `jarvis channels-status` can report "library missing"
    without the CLI itself failing to import on a machine that never
    intends to run the bot.
    """
    try:
        import discord  # noqa: F401
        return discord, ""
    except ImportError:
        return None, (
            "discord.py is not installed. Run:  pip install -U discord.py\n"
            "(Python 3.8+. The 'discord' package on PyPI is a different, "
            "unmaintained project — install discord.py specifically.)"
        )


def strip_mentions(text, bot_id=None):
    """Remove mention tokens, and the bot's own leading mention especially.

    Only the *leading* address is removed wholesale; mentions of other
    people mid-sentence become @id so the model can still tell someone was
    referred to, without carrying Discord's angle-bracket syntax.
    """
    text = (text or "").strip()
    if bot_id:
        leading = re.compile(r"^\s*<@[!&]?%s>\s*" % re.escape(str(bot_id)))
        text = leading.sub("", text)
    text = _MENTION_RE.sub(lambda m: "@" + m.group(1), text)
    return text.strip()


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return False
    return True


def _claim_pid_file():
    """Refuse to start if a live daemon already holds the file. Two
    gateways on one token means Discord delivers each message twice and
    Jarvis answers twice — see sched_daemon.py for the same reasoning."""
    try:
        if PID_FILE.exists():
            try:
                existing = int(PID_FILE.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                existing = None
            if existing and existing != os.getpid() and _pid_alive(existing):
                return False, f"a discord daemon is already running (pid {existing})"
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True, ""
    except OSError as exc:
        return False, f"could not claim pid file: {exc}"


def _release_pid_file():
    try:
        if PID_FILE.exists():
            if PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
                PID_FILE.unlink()
    except OSError:
        pass


def build_client(discord, cfg):
    """Construct the discord.py client with the right intents and handlers.

    Split out from run() so a test can build and inspect a client (intents,
    handler wiring) without ever connecting to Discord.
    """
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.dm_messages = True
    # See the module docstring — off by default, works anyway.
    intents.message_content = bool(cfg.get("message_content_intent"))

    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():  # noqa: F811 — discord.py's decorator convention
        me = client.user
        base._log(f"discord connected as {me} (id {me.id})")
        # Persist our own id so permissions._is_self() can drop our own
        # messages, and so the mention stripper knows what to remove. Only
        # written when it actually changed — this file is hand-edited and
        # rewriting it on every connect would fight the user's editor.
        current = channel_config.platform_config(DISCORD)
        if str(current.get("bot_user_id") or "") != str(me.id):
            channel_config.set_value(DISCORD, "bot_user_id", str(me.id))
            base._log(f"recorded bot_user_id={me.id} in channels.json")
        print(permissions.describe(channel_config.platform_config(DISCORD)),
              file=sys.stderr, flush=True)

    @client.event
    async def on_message(message):  # noqa: F811
        # Fast, cheap rejections first, before any config read: our own
        # messages, and other bots (two assistants in one server answering
        # each other forever is a real and very loud failure).
        if client.user and message.author.id == client.user.id:
            return
        if getattr(message.author, "bot", False):
            return

        cfg = channel_config.platform_config(DISCORD)
        is_dm = isinstance(message.channel, discord.DMChannel)
        mentioned = bool(client.user and client.user in getattr(message, "mentions", []))

        msg = permissions.IncomingMessage(
            platform=DISCORD,
            context=permissions.CTX_DM if is_dm else permissions.CTX_GROUP,
            user_id=str(message.author.id),
            user_handle=getattr(message.author, "name", "") or "",
            text=strip_mentions(message.content, client.user.id if client.user else None),
            mentioned=mentioned or is_dm,
            guild_id=str(message.guild.id) if message.guild else "",
            channel_id=str(message.channel.id),
            message_id=str(message.id),
            thread_id=str(message.channel.id),
            raw=message,
        )

        # A message with no text left after stripping the mention is a bare
        # ping ("@jarvis"). Treat it as a greeting rather than asking the
        # model to respond to an empty string.
        if not msg.text and (mentioned or is_dm):
            msg.text = "Hi"

        # Discord hands us the username for free on every event, unlike
        # Instagram (see instagram_gateway._fetch_username's docstring for
        # why that one needs an active API call). So this is a plain
        # record — teaches the directory a handle->id mapping for
        # `channels-allow discord reply @name` with no extra cost, and it
        # happens before the read-receipt/typing work below so a message
        # that later errors out still gets its handle learned.
        if msg.user_handle:
            directory.record(DISCORD, msg.user_handle, msg.user_id)

        loop = asyncio.get_running_loop()
        sent_any = {"ok": False}

        # --- read receipt -------------------------------------------------
        # Added the moment the message is accepted, before any provider is
        # contacted, because its whole job is to answer "did it even see
        # me?" during the seconds before anything else appears. Every step
        # is wrapped: a missing Add Reactions permission is extremely common
        # (the invite URL in the Guides only asks for Send Messages and Read
        # Message History) and must degrade to "no receipt", never to a
        # failed reply.
        receipts = bool(cfg.get("read_receipts", True))

        async def _react(emoji):
            if not receipts or not emoji:
                return
            try:
                await message.add_reaction(emoji)
            except Exception:  # noqa: BLE001 — usually a missing permission
                pass

        async def _unreact(emoji):
            if not receipts or not emoji:
                return
            try:
                await message.remove_reaction(emoji, client.user)
            except Exception:  # noqa: BLE001
                pass

        seen_emoji = cfg.get("reaction_seen") or "\U0001F440"
        await _react(seen_emoji)

        def send(text):
            """Called from the worker thread — hop back to the event loop,
            because discord.py's send() is a coroutine and is not safe to
            drive from another thread."""
            future = asyncio.run_coroutine_threadsafe(
                message.channel.send(text), loop)
            future.result(timeout=30)
            sent_any["ok"] = True
            return True

        failed = False
        try:
            async with message.channel.typing():
                # handle_message does blocking IO (a provider round trip, tool
                # execution). Running it on the event loop would freeze the
                # gateway's heartbeat and get the bot disconnected, so it goes
                # to a thread.
                #
                # discord.py's typing() context manager re-sends the typing
                # signal every ~9s for as long as the block is open, so a
                # multi-round tool call keeps showing "Jarvis is typing…"
                # rather than going quiet after Discord's 10-second timeout.
                await loop.run_in_executor(
                    None, lambda: base.handle_message(DISCORD, msg, send, cfg=cfg))
        except Exception as exc:  # noqa: BLE001
            failed = True
            base._log("discord handler failed: %s" % exc)
        finally:
            # Swap the receipt for an outcome. A message that produced no
            # reply at all (denied by permissions, or the model returned
            # nothing) is marked failed rather than silently left at 👀 —
            # "seen, and nothing happened" is a state worth being able to
            # see from the chat.
            await _unreact(seen_emoji)
            if failed or not sent_any["ok"]:
                if sent_any["ok"] or cfg.get("react_when_denied"):
                    await _react(cfg.get("reaction_failed") or "\u26a0\ufe0f")
            else:
                await _react(cfg.get("reaction_done") or "\u2705")

    return client


async def send_dm(discord, token, user_id, text, cfg=None):
    """Send a DM to one user. Used by outbound.py for owner notifications.

    Opens its own short-lived client rather than reusing a running gateway:
    a notification usually fires from `jarvis sched-tick` or a tool call in
    a completely separate process, which has no handle on the daemon's
    client. Discord has no REST route to DM a user without first creating a
    DM channel, and discord.py's create_dm() needs a logged-in client.
    """
    cfg = cfg or channel_config.platform_config(DISCORD)
    limit = int(cfg.get("max_reply_chars") or 1900)

    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    outcome = {"ok": False, "error": "did not connect"}

    @client.event
    async def on_ready():  # noqa: F811
        try:
            user = await client.fetch_user(int(user_id))
            for chunk in base.chunk_text(text, limit):
                await user.send(chunk)
            outcome["ok"] = True
            outcome["error"] = ""
        except Exception as exc:  # noqa: BLE001
            outcome["ok"] = False
            # The overwhelmingly common cause, worth naming explicitly
            # rather than surfacing a bare 403: Discord only lets a bot DM
            # a user who shares a server with it and has DMs from server
            # members enabled.
            outcome["error"] = (
                f"{exc} (a bot can only DM a user who shares a server with it "
                "and allows DMs from server members)")
        finally:
            await client.close()

    try:
        await client.start(token)
    except Exception as exc:  # noqa: BLE001
        outcome["error"] = str(exc)
    return outcome["ok"], outcome["error"]


def run():
    """Entry point for `jarvis discord-daemon`. Returns a process exit code."""
    discord, err = import_discord()
    if discord is None:
        print(err, file=sys.stderr)
        return 1

    cfg = channel_config.platform_config(DISCORD)
    if not cfg.get("enabled"):
        print("discord channel is disabled. Enable it with:\n"
              "  jarvis channels-set discord enabled true", file=sys.stderr)
        return 1
    token = str(cfg.get("bot_token") or "").strip()
    if not token:
        print(f"no bot_token set. Add one to {channel_config.CONFIG_FILE}\n"
              "See the Guides panel in the web UI for how to create a bot.",
              file=sys.stderr)
        return 1

    ok, why = _claim_pid_file()
    if not ok:
        print(why, file=sys.stderr)
        return 1

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_stop)
        except (ValueError, OSError):
            pass  # not always settable (non-main thread, odd platforms)

    client = build_client(discord, cfg)
    try:
        client.run(token, log_handler=None)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "Privileged" in msg or "disallowed" in msg.lower():
            print("Discord rejected the requested intents. If you set "
                  "message_content_intent: true, you must also tick "
                  "MESSAGE CONTENT INTENT in the Developer Portal — or set "
                  "it back to false, which is the supported default.",
                  file=sys.stderr)
        elif "Improper token" in msg or "401" in msg:
            print("Discord rejected the bot token. Copy it again from the "
                  "Developer Portal > Bot > Reset Token.", file=sys.stderr)
        else:
            print(f"discord gateway stopped: {exc}", file=sys.stderr)
        return 1
    finally:
        _release_pid_file()
    return 0
