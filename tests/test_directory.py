"""Tests for jarvis.channels.directory — the @handle -> id resolver.

Kept as its own file rather than folded into tests/test_channels.py so it
never collides with whatever else is being added there in parallel; it only
needs channels/directory.py, channels/instagram_gateway.py and the
platform constants, so it stands alone cleanly.

    python3 tests/test_directory.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.channels import DISCORD, INSTAGRAM              # noqa: E402
from jarvis.channels import config as channel_config        # noqa: E402
from jarvis.channels import directory                       # noqa: E402
from jarvis.channels import instagram_gateway as ig          # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' — ' + detail) if detail else ''}")


class SandboxDirectory:
    """Point directory.py's storage at a tempdir for the duration of a test."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = directory.CHANNELS_DIR
        self._orig_file = directory.DIRECTORY_FILE
        directory.CHANNELS_DIR = Path(self._tmp.name)
        directory.DIRECTORY_FILE = Path(self._tmp.name) / "directory.json"
        return self

    def __exit__(self, *exc):
        directory.CHANNELS_DIR = self._orig_dir
        directory.DIRECTORY_FILE = self._orig_file
        self._tmp.cleanup()
        return False


def test_unknown_handle_fails_clearly():
    with SandboxDirectory():
        resolved, err = directory.resolve(DISCORD, "@nobody")
        check("unknown handle resolves to None", resolved is None)
        check("error explains how to fix it", "send a DM first" in err, err)


def test_record_then_resolve():
    with SandboxDirectory():
        directory.record(DISCORD, "Alice", "111222333")
        resolved, err = directory.resolve(DISCORD, "@Alice")
        check("known handle resolves to its id", resolved == "111222333")
        check("no error on success", err is None)
        check("case-insensitive", directory.resolve(DISCORD, "@ALICE")[0] == "111222333")
        check("bare handle without @ also resolves",
              directory.resolve(DISCORD, "alice")[0] == "111222333")


def test_non_handles_pass_through_unresolved():
    with SandboxDirectory():
        check("bare numeric id is untouched",
              directory.resolve(DISCORD, "111222333") == ("111222333", None))
        check("wildcard is untouched",
              directory.resolve(DISCORD, "*") == ("*", None))
        check("empty string is untouched",
              directory.resolve(DISCORD, "")[0] == "")


def test_platforms_are_isolated():
    with SandboxDirectory():
        directory.record(DISCORD, "alice", "111")
        directory.record(INSTAGRAM, "alice", "999")
        check("same handle resolves differently per platform",
              directory.resolve(DISCORD, "@alice")[0] == "111"
              and directory.resolve(INSTAGRAM, "@alice")[0] == "999")


def test_rerecording_overwrites_not_merges():
    with SandboxDirectory():
        directory.record(DISCORD, "alice", "111")
        directory.record(DISCORD, "alice", "222")   # handle renamed hands / reused
        check("latest recording wins",
              directory.resolve(DISCORD, "@alice")[0] == "222")


def test_empty_sides_are_not_recorded():
    with SandboxDirectory():
        directory.record(DISCORD, "someone", "123")
        before = directory.all_entries(DISCORD)
        directory.record(DISCORD, "", "999")
        directory.record(DISCORD, "another", "")
        check("recording with an empty handle or id is a no-op",
              directory.all_entries(DISCORD) == before)


def test_all_entries_listing():
    with SandboxDirectory():
        directory.record(DISCORD, "alice", "1")
        directory.record(INSTAGRAM, "bob", "2")
        check("filtered by platform", len(directory.all_entries(DISCORD)) == 1)
        check("unfiltered sees everything", len(directory.all_entries()) == 2)


def test_instagram_actively_fetches_missing_username():
    """The bug this whole feature exists to fix: Instagram's webhook
    payload for a DM almost never includes `username` — just a bare numeric
    `sender.id`. Without an active fetch, the directory never learns
    anything no matter how many messages arrive."""
    with SandboxDirectory():
        payload = {"entry": [{"messaging": [
            {"sender": {"id": "17856012345678901"},  # no username — realistic
             "message": {"mid": "m1", "text": "hi"}}]}]}
        cfg = channel_config._platform_defaults(INSTAGRAM)
        cfg.update({"enabled": True, "reply_allowlist": ["*"],
                   "access_token": "unused-because-fetch-is-mocked"})

        calls = []

        def fake_fetch(user_id, _cfg):
            calls.append(user_id)
            return "truly_assim"

        original = ig._fetch_username
        ig._fetch_username = fake_fetch
        try:
            ig._process_payload(payload, cfg)
        finally:
            ig._fetch_username = original

        check("username was actively fetched since the webhook had none",
              calls == ["17856012345678901"])
        resolved, _ = directory.resolve(INSTAGRAM, "@truly_assim")
        check("the fetched handle lands in the directory",
              resolved == "17856012345678901")


def test_instagram_does_not_refetch_when_webhook_already_has_a_handle():
    """If a future Meta payload DOES include a username, don't waste an
    API call re-fetching what's already there."""
    with SandboxDirectory():
        payload = {"entry": [{"messaging": [
            {"sender": {"id": "555", "username": "already_known"},
             "message": {"mid": "m1", "text": "hi"}}]}]}
        cfg = channel_config._platform_defaults(INSTAGRAM)
        cfg.update({"enabled": True, "reply_allowlist": ["*"]})

        calls = []
        original = ig._fetch_username
        ig._fetch_username = lambda uid, c: calls.append(uid) or "should-not-be-used"
        try:
            ig._process_payload(payload, cfg)
        finally:
            ig._fetch_username = original

        check("no fetch when the webhook already supplied a handle", calls == [])
        check("directory still learns the handle it was given",
              directory.resolve(INSTAGRAM, "@already_known")[0] == "555")


def main():
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAILED.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}\n"
                          + "    " + traceback.format_exc().splitlines()[-3].strip())

    total = PASSED + len(FAILED)
    print(f"{PASSED}/{total} passed")
    for failure in FAILED:
        print("  FAIL:", failure)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
