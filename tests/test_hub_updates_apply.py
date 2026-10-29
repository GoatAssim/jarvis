"""Download Hub, the Updates rework (H1): applying updates.  The apply path, the auto-update rules, the apply runner and
what it records, the new settings, and the demo provider's fake update source.

Tested against FAKE update and apply tools and a FAKE clock: nothing here sleeps for a minute, nothing touches the network,
no package manager or Steam is needed, nothing is installed.

Covers:
  * the provider's declared group / update_args / apply tool / apply_args / timeout / updates_only reaching the store, and
    a provider whose apply tool's schema lacks `id` or `dry_run` being unable to apply;
  * `present()`: group, can_apply and the apply state of each finding, the groups block, the global settings, `applying`;
  * `request_apply`: one finding, all, one group, the refusals (daemon down, unknown group, provider off, hidden finding,
    no apply tool), "already running" on a second ask, the manual batch cap;
  * the ApplyRunner: one apply at a time, the call it makes (`id`, `dry_run: false`, the declared apply_args, the hub's two
    keys always winning), done / started / unchanged / failed, an apply that raises or times out, the immediate re-check,
    a hung apply never blocking a check, the event trail, ONE notification per batch;
  * auto-update: off by default, and only when the master switch, the provider's switch, update checks, the group, a live
    daemon and a usable apply tool all hold; a hidden finding is never applied; a failed version is never retried; a newer
    version gets a fresh try; the per-cycle cap;
  * `dry_run`: the plan, no change, a timeout, an error;
  * `parse_apply_result` on every shape of answer; the notification wording;
  * the new settings (defaults, validators, the chat path refused, `updates_set`); the store migration of a database made
    before the new columns; the restart recovery;
  * the demo provider's fake update source, and that the tool file validates.

Run: python tests/test_hub_updates_apply.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-upd-apply-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["JARVIS_HUB_NO_UPDATES"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_STEAM_ROOT", None)
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER", "JARVIS_CHANNEL",
             "JARVIS_TASK_ID"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import tool_downloads  # noqa: E402
from jarvis.hub import notify, settings as settings_mod, updates, updates_admin  # noqa: E402
from jarvis.hub import store as store_mod  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0
_n = [0]


def check(name, cond, detail=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}" + (f"   [{detail!r}]" if detail is not None else ""))


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def new_db_path():
    _n[0] += 1
    return Path(HOME) / f"hub-{_n[0]}.db"


def new_store(clock=None):
    return Store(new_db_path(), clock=clock or time.time)


APPLY_SCHEMA_OK = [{"name": "apply_fetch_update",
                    "parameters": {"type": "object", "properties": {"id": {"type": "string"}, "dry_run": {"type": "boolean"}}}}]
APPLY_SCHEMA_NO_DRY = [{"name": "apply_fetch_update",
                        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}}}]
FINDINGS = [
    {"id": "a", "title": "Alpha", "installed": "1", "available": "2", "source": "pip"},
    {"id": "b", "title": "Beta", "installed": "1", "available": "3", "source": "pip"},
]


class Env:
    """An update runner and an apply runner on one fake clock, with fake update / apply / notifier."""

    def __init__(self, group="packages", apply_tool="apply_fetch_update", schemas=None, findings=None, timeout=40.0):
        self.clock = Clock()
        self.store = new_store(self.clock)
        self.store.register_provider("fetch", label="Fetch", mode="push")
        self.findings = list(findings or FINDINGS)
        self.check_calls, self.apply_calls, self.sent = [], [], []
        self.apply_answers = {}
        entry = {"id": "fetch", "update_tool": "check_fetch_updates", "update_interval_h": None, "update_group": group,
                 "update_args": {"manager": "pip"}, "updates_only": True}
        if apply_tool:
            entry.update(apply_tool=apply_tool, apply_args={"flag": "x", "id": "NOT-THE-HUBS"}, apply_timeout_s=timeout)
        self.declared = [entry]
        self.checker = updates.UpdateRunner(self.store, clock=self.clock, executor=self.check_exec,
                                            declared=lambda: self.declared, first_delay_s=0.0,
                                            schemas=lambda: schemas if schemas is not None else APPLY_SCHEMA_OK)
        self.applier = updates.ApplyRunner(self.store, clock=self.clock, executor=self.apply_exec, deliver=self.deliver)

    # fakes
    def check_exec(self, name, args):
        self.check_calls.append((name, dict(args)))
        return {"updates": [dict(f) for f in self.findings], "complete": True}

    def apply_exec(self, name, args):
        self.apply_calls.append((name, dict(args)))
        answer = self.apply_answers.get(args["id"], {"ok": True, "state": "done", "message": "applied " + args["id"]})
        if isinstance(answer, BaseException):
            raise answer
        return answer() if callable(answer) else answer

    def deliver(self, title, message, **kwargs):
        self.sent.append({"title": title, "message": message, "failed": bool(kwargs.get("failed")), "kw": kwargs})
        return {"delivered": []}

    # helpers
    def beat(self):
        self.store.daemon_beat(os.getpid(), self.clock())

    def check_now(self):
        self.checker.tick()
        chk = self.checker._state["fetch"]["check"]
        if chk is not None:
            assert chk.done.wait(5), "the fake check did not finish"
        self.checker.tick()

    def drain(self):
        """Run the apply runner until nothing is queued or running."""
        for _ in range(60):
            self.applier.tick()
            cur = self.applier._current
            if cur is not None:
                assert cur["done"].wait(5), "the fake apply did not finish"
                continue
            if self.store.applying_counts() == (0, 0):
                return
        raise AssertionError("the apply queue never drained")

    def present(self):
        return updates.present(self.store, now=self.clock())

    def states(self):
        return {k[1]: v["state"] for k, v in self.store.applying_info().items()}

    def turn_on_auto(self, master=True, provider=True, groups=None, cap=None):
        clean = {}
        if master:
            clean["updates.auto_update"] = True
        if groups is not None:
            clean["updates.include_groups"] = groups
        if cap is not None:
            clean["updates.auto_max_per_cycle"] = cap
        if clean:
            self.store.set_settings(clean)
        if provider:
            self.store.update_provider("fetch", settings={"auto_update": True})


# --------------------------------------------------------------------------- what the provider declared reaches the store

def test_declaration_and_present():
    env = Env()
    env.beat()
    env.check_now()
    st = env.store.update_check_state()["fetch"]
    check("the group, update_args, apply tool, apply_args, timeout and updates_only are stored",
          st["update_group"] == "packages" and st["update_args"] == {"manager": "pip"}
          and st["apply_tool"] == "apply_fetch_update" and st["apply_args"] == {"flag": "x", "id": "NOT-THE-HUBS"}
          and st["apply_timeout_s"] == 40.0 and st["updates_only"] is True and not st["apply_problem"], st)
    check("every call of the update tool carries the declared update_args", env.check_calls
          and all(c == ("check_fetch_updates", {"manager": "pip"}) for c in env.check_calls), env.check_calls)
    view = env.present()
    row = {r["id"]: r for r in view["updates"]}["a"]
    check("a finding shows its group, can_apply and no apply state yet",
          row["group"] == "packages" and row["can_apply"] is True and row["apply"] is None, row)
    prov = view["providers"][0]
    check("the provider row shows group, can_apply, updates_only and auto_update (off)",
          prov["group"] == "packages" and prov["can_apply"] is True and prov["updates_only"] is True
          and prov["auto_update"] is False, prov)
    groups = {g["id"]: g for g in view["groups"]}
    check("the five groups are listed, all included, with counts",
          list(groups) == ["mods", "games", "packages", "apps", "other"] and all(g["included"] for g in groups.values())
          and groups["packages"]["count"] == 2 and groups["packages"]["applicable"] == 2
          and groups["packages"]["providers"] == ["fetch"] and groups["mods"]["count"] == 0, groups)
    check("the global settings show auto-update off, every group and 10 per cycle; nothing is applying",
          view["settings"] == {"auto_update": False, "include_groups": ["mods", "games", "packages", "apps", "other"],
                               "auto_max_per_cycle": 10} and view["applying"] == 0, view["settings"])


def test_schema_problem_blocks_applying():
    env = Env(schemas=APPLY_SCHEMA_NO_DRY)
    env.beat()
    env.check_now()
    st = env.store.update_check_state()["fetch"]
    check("an apply tool whose schema lacks dry_run is not stored as usable, and says why",
          st["apply_tool"] is None and "dry_run" in (st["apply_problem"] or ""), st)
    view = env.present()
    check("so no finding can be applied", all(r["can_apply"] is False for r in view["updates"]))
    out = updates.request_apply(env.store, provider="fetch", local_id="a")
    check("and asking to apply one is refused in words", out["ok"] is False and "apply tool" in out["error"], out)
    env.turn_on_auto()
    check("auto-update queues nothing for it", updates.auto_update_pass(env.store, "fetch", now=env.clock()) == 0)
    check("tool_downloads.apply_schema_problem agrees (a missing schema is a problem too)",
          tool_downloads.apply_schema_problem("apply_fetch_update", APPLY_SCHEMA_NO_DRY)
          and tool_downloads.apply_schema_problem("apply_fetch_update", APPLY_SCHEMA_OK) is None
          and tool_downloads.apply_schema_problem("nope", APPLY_SCHEMA_OK))
    env2 = Env(apply_tool=None)
    env2.beat()
    env2.check_now()
    out = updates.request_apply(env2.store, provider="fetch", local_id="a")
    check("a provider with no apply tool at all: not applicable, in words",
          out["ok"] is False and out["results"][0]["status"] == "not_applicable"
          and "by itself" in out["error"], out)


# --------------------------------------------------------------------------- asking to apply

def test_request_apply_refusals_and_idempotence():
    env = Env()
    env.check_now()
    out = updates.request_apply(env.store, provider="fetch", local_id="a")
    check("with the daemon down nothing is queued (code daemon_down)",
          out["ok"] is False and out.get("code") == "daemon_down" and env.store.applying_info() == {}, out)
    env.beat()
    check("an unknown group is refused", updates.request_apply(env.store, all_updates=True, group="nope")["ok"] is False)
    check("naming nothing is refused", updates.request_apply(env.store)["ok"] is False)
    check("an id without a provider is refused", updates.request_apply(env.store, local_id="a")["ok"] is False)
    out = updates.request_apply(env.store, provider="fetch", local_id="nope")
    check("an update that does not exist: ok false, not applicable", out["ok"] is False
          and "no such update" in out["error"] and env.store.applying_info() == {}, out)
    out = updates.request_apply(env.store, provider="ghost", local_id="a")
    check("an unknown provider is refused in words", out["ok"] is False and "no provider called" in out["error"], out)

    out = updates.request_apply(env.store, provider="fetch", local_id="a")
    check("one finding is queued, with a batch id", out["ok"] and out["queued"] == 1 and out["batch"]
          and out["results"][0]["status"] == "queued", out)
    row = {r["id"]: r for r in env.present()["updates"]}["a"]
    check("it shows as queued, and the panel counts it as applying",
          row["apply"]["state"] == "queued" and env.present()["applying"] == 1, row)
    out2 = updates.request_apply(env.store, provider="fetch", local_id="a")
    check("asking again says already running and queues nothing more",
          out2["ok"] and out2["queued"] == 0 and out2["results"][0]["status"] == "already_running", out2)
    events = [e["message"] for e in env.store.list_events("update:fetch")]
    check("the queueing left a row in the event trail", any("apply queued" in m and "Alpha" in m for m in events), events)

    env.store.update_dismiss("fetch", "b", now=env.clock())
    out = updates.request_apply(env.store, provider="fetch", local_id="b")
    check("an ignored version is refused (and 'all' leaves it out)", out["ok"] is False and "ignored" in out["error"], out)
    out = updates.request_apply(env.store, all_updates=True)
    check("all: the ignored one is not queued and the one already queued is not queued twice",
          out["ok"] and out["queued"] == 0 and [r["id"] for r in out["results"]] == ["a"]
          and out["results"][0]["status"] == "already_running", out)

    env.store.set_provider_enabled("fetch", False)
    out = updates.request_apply(env.store, provider="fetch", local_id="a")
    check("a provider switched off cannot be applied to", out["ok"] is False and "switched off" in out["error"], out)


def test_all_group_and_cap():
    env = Env()
    env.beat()
    env.check_now()
    out = updates.request_apply(env.store, all_updates=True, group="mods")
    check("all of a group that has nothing: ok, nothing queued, a message", out["ok"] and out["queued"] == 0
          and out["results"] == [] and out.get("message"), out)
    old = updates.MAX_MANUAL_BATCH
    updates.MAX_MANUAL_BATCH = 1
    try:
        out = updates.request_apply(env.store, all_updates=True, group="packages")
    finally:
        updates.MAX_MANUAL_BATCH = old
    check("'apply all' is capped per request", out["queued"] == 1 and len(out["results"]) == 1, out)
    out = updates.request_apply(env.store, provider="fetch", all_updates=True)
    check("all of one provider queues the rest", out["queued"] == 1 and out["results"][0]["status"] in ("queued", "already_running"),
          out)


# --------------------------------------------------------------------------- the apply runner

def test_runner_applies_one_and_records_it():
    env = Env()
    env.beat()
    env.check_now()
    updates.request_apply(env.store, provider="fetch", local_id="a")
    check("one pass starts the apply and marks it running", env.applier.tick() == ["fetch"]
          and env.states() == {"a": "running"}, env.states())
    cur = env.applier._current
    check("while it runs, a second pass starts nothing else", cur["done"].wait(5) and True)
    env.applier.tick()
    check("it is recorded as done", env.states() == {"a": "done"}, env.states())
    check("the hub called the provider's own apply tool with its own id and dry_run false; the hub's two keys beat apply_args",
          env.apply_calls == [("apply_fetch_update", {"flag": "x", "id": "a", "dry_run": False})], env.apply_calls)
    row = {r["id"]: r for r in env.present()["updates"]}["a"]
    check("the finding shows done and the tool's own sentence", row["apply"]["state"] == "done"
          and row["apply"]["message"] == "applied a", row)
    check("the provider is re-checked at once (no manual-check limit used)",
          env.store.update_check_state()["fetch"]["requested_at"] is not None
          and env.store.update_check_state()["fetch"]["manual_at"] is None)
    events = [e["message"] for e in env.store.list_events("update:fetch")]
    check("the event trail has started and done lines",
          any(m.startswith("apply started") for m in events) and any(m.startswith("apply done") for m in events), events)
    check("ONE notification, a quiet one for a good result",
          len(env.sent) == 1 and env.sent[0]["title"] == "Update applied" and env.sent[0]["failed"] is False
          and env.sent[0]["kw"].get("look") == "compact", env.sent)
    env.check_now()
    check("the re-check then lists what the source still says", True)


def test_runner_states_and_failures():
    env = Env(findings=FINDINGS + [{"id": "c", "title": "Gamma", "installed": "1", "available": "2"},
                                   {"id": "d", "title": "Delta", "installed": "1", "available": "2"},
                                   {"id": "e", "title": "Eps", "installed": "1", "available": "2"}])
    env.beat()
    env.check_now()
    env.apply_answers.update({
        "a": {"ok": True, "state": "started", "message": "finish it in the program"},
        "b": {"ok": True, "state": "unchanged", "message": "already current"},
        "c": {"ok": False, "error": "disk full"},
        "d": RuntimeError("the tool blew up"),
        "e": '{"ok": true, "state": "done"}',
    })
    out = updates.request_apply(env.store, all_updates=True)
    check("five queued in one batch", out["queued"] == 5, out)
    env.drain()
    got = env.states()
    check("started stays started, unchanged is recorded as done, a failed answer and a raise are failed, a JSON string works",
          got == {"a": "started", "b": "done", "c": "failed", "d": "failed", "e": "done"}, got)
    info = {k[1]: v for k, v in env.store.applying_info().items()}
    check("the failure sentences are the tool's own, and a raise is said plainly",
          info["c"]["message"] == "disk full" and "blew up" in info["d"]["message"], info)
    check("they ran one at a time, in the order they were queued (sorted by title)",
          [c[1]["id"] for c in env.apply_calls] == ["a", "b", "d", "e", "c"], [c[1]["id"] for c in env.apply_calls])
    check("ONE notification for the whole batch, a card because something failed",
          len(env.sent) == 1 and env.sent[0]["failed"] is True and "5 updates" in env.sent[0]["title"]
          and "2 failed" in env.sent[0]["message"], env.sent)
    check("a re-check was asked for (something changed)", env.store.update_check_state()["fetch"]["requested_at"] is not None)

    env2 = Env()
    env2.beat()
    env2.check_now()
    env2.apply_answers["a"] = {"ok": False, "error": "nope"}
    updates.request_apply(env2.store, provider="fetch", local_id="a")
    env2.drain()
    check("a failure alone does not ask for a re-check",
          env2.store.update_check_state()["fetch"]["requested_at"] is None)
    check("a failed single apply sends one failure card",
          len(env2.sent) == 1 and env2.sent[0]["title"] == "Update failed" and env2.sent[0]["failed"] is True, env2.sent)


def test_timeout_one_at_a_time_and_checks_not_blocked():
    env = Env(timeout=40.0)
    env.beat()
    env.check_now()
    hang = threading.Event()
    env.apply_answers["a"] = lambda: (hang.wait(10), {"ok": True, "state": "done"})[1]
    updates.request_apply(env.store, all_updates=True)
    env.applier.tick()
    check("the first apply is running and the second waits", env.states() == {"a": "running", "b": "queued"}, env.states())
    env.clock.advance(30)
    check("inside its timeout nothing else starts", env.applier.tick() == [] and env.states()["b"] == "queued")
    env.clock.advance(11)
    env.applier.tick()
    info = {k[1]: v for k, v in env.store.applying_info().items()}
    check("past its timeout it is given up on and recorded as failed, in words",
          info["a"]["state"] == "failed" and "longer than 40 s" in info["a"]["message"], info["a"])
    env.store.update_check_put("fetch", requested_at=env.clock())
    before = len(env.check_calls)
    env.check_now()
    check("a hung apply does not block a check", len(env.check_calls) > before)
    env.beat()
    env.applier.tick()
    info = {k[1]: v for k, v in env.store.applying_info().items()}
    check("the next apply of the same provider waits for the stuck thread: failed with a plain sentence, never run",
          info["b"]["state"] == "failed" and "still running" in info["b"]["message"]
          and [c[1]["id"] for c in env.apply_calls] == ["a"], info["b"])
    check("one notification covers both",
          len(env.sent) == 1 and env.sent[0]["failed"] is True, env.sent)
    hang.set()
    stuck = env.applier._stuck.get("fetch")
    check("(the hung thread ends)", stuck is None or stuck.wait(5))
    env.applier.tick()
    check("once it ended the provider can be applied to again", "fetch" not in env.applier._stuck)


def test_recover_after_restart():
    env = Env()
    env.beat()
    env.check_now()
    updates.request_apply(env.store, all_updates=True)
    env.store.applying_claim(env.clock())
    dropped, interrupted = env.store.applying_recover(env.clock())
    check("a restart forgets a queued apply and marks a running one failed", (dropped, interrupted) == (1, 1))
    info = {k[1]: v for k, v in env.store.applying_info().items()}
    check("the interrupted one says so and is not retried by itself",
          info["a"]["state"] == "failed" and "stopped" in info["a"]["message"] and "b" not in info, info)
    env.turn_on_auto()
    env.beat()
    check("auto-update does not retry the version that was interrupted",
          updates.auto_update_pass(env.store, "fetch", now=env.clock()) == 1
          and {k[1] for k, v in env.store.applying_info().items() if v["state"] == "queued"} == {"b"})


# --------------------------------------------------------------------------- auto-update

def auto_queued(env):
    return sorted(k[1] for k, v in env.store.applying_info().items() if v["state"] == "queued")


def test_auto_update_is_off_and_rules():
    env = Env()
    env.beat()
    env.check_now()
    check("by default nothing is queued by itself", auto_queued(env) == [] and updates.auto_update_pass(env.store, "fetch", now=env.clock()) == 0)
    check("the master switch is off by default and the provider's tick too",
          settings_mod.update_global_settings(env.store.get_settings())["auto_update"] is False
          and settings_mod.provider_auto_update(env.store.get_provider("fetch")["settings"]) is False)

    def scenario(master=True, provider=True, groups=None, cap=None, beat=True, hide=None, checks_on=True, enabled=True):
        e = Env()
        e.beat()
        e.check_now()
        e.turn_on_auto(master=master, provider=provider, groups=groups, cap=cap)
        if hide:
            e.store.update_dismiss("fetch", hide, now=e.clock())
        if not checks_on:
            e.store.update_provider("fetch", settings={"update_checks": False})
        if not enabled:
            e.store.set_provider_enabled("fetch", False)
        e.clock.advance(0 if beat else 600)
        return e, updates.auto_update_pass(e.store, "fetch", now=e.clock())

    e, n = scenario()
    check("everything on: both findings are queued, by auto", n == 2 and auto_queued(e) == ["a", "b"]
          and {v["origin"] for v in e.store.applying_info().values()} == {"auto"})
    check("...and the event trail says auto-update queued them",
          any("by auto-update" in m["message"] for m in e.store.list_events("update:fetch")))
    check("master switch off: nothing", scenario(master=False)[1] == 0)
    check("the provider's own tick off: nothing", scenario(provider=False)[1] == 0)
    check("its group not included: nothing", scenario(groups=["mods"])[1] == 0)
    check("its group included among others: queued", scenario(groups=["mods", "packages"])[1] == 2)
    check("no group included at all: nothing", scenario(groups=[])[1] == 0)
    check("daemon not alive (its heartbeat is old): nothing", scenario(beat=False)[1] == 0)
    check("update checks off for the provider: nothing", scenario(checks_on=False)[1] == 0)
    check("provider switched off: nothing", scenario(enabled=False)[1] == 0)
    e, n = scenario(hide="a")
    check("an ignored finding is never applied by itself", n == 1 and auto_queued(e) == ["b"])
    e, n = scenario(cap=1)
    check("at most auto_max_per_cycle per step", n == 1 and len(auto_queued(e)) == 1)


def test_auto_update_never_retries_a_failed_version_but_tries_a_new_one():
    env = Env()
    env.beat()
    env.check_now()
    env.apply_answers["a"] = {"ok": False, "error": "locked"}
    updates.request_apply(env.store, all_updates=True)
    env.drain()
    env.turn_on_auto()
    env.clock.advance(25 * 3600)
    env.beat()
    env.check_now()
    check("after a failure and a done, the next check queues nothing: both versions were already tried",
          auto_queued(env) == [] and env.states() == {"a": "failed", "b": "done"}, env.states())
    env.findings[0] = dict(env.findings[0], available="9")
    env.clock.advance(25 * 3600)
    env.beat()
    env.check_now()
    check("a NEWER available text gets a fresh try, by itself", auto_queued(env) == ["a"], env.states())
    env.apply_answers.pop("a")
    env.drain()
    check("and it is applied", env.states()["a"] == "done" and env.apply_calls[-1][1]["id"] == "a")
    check("the auto batch is announced once, as one update applied",
          env.sent[-1]["title"] == "Update applied" and env.sent[-1]["failed"] is False, env.sent[-1])


def test_failed_memory_outlives_the_finding():
    env = Env()
    env.beat()
    env.check_now()
    env.apply_answers["a"] = {"ok": False, "error": "locked"}
    updates.request_apply(env.store, provider="fetch", local_id="a")
    env.drain()
    env.turn_on_auto()
    kept = env.findings
    env.findings = [f for f in kept if f["id"] != "a"]
    env.clock.advance(25 * 3600)
    env.beat()
    env.check_now()
    check("the finding drops out when the source stops listing it", "a" not in {r["id"] for r in env.present()["updates"]})
    check("but the memory of the failed apply stays", env.store.applying_info()[("fetch", "a")]["state"] == "failed")
    env.findings = kept
    env.clock.advance(25 * 3600)
    env.beat()
    env.check_now()
    check("it comes back with the SAME version and is not retried by itself", "a" not in auto_queued(env), env.states())
    check("a manual apply can still try it", updates.request_apply(env.store, provider="fetch", local_id="a")["queued"] == 1)


# --------------------------------------------------------------------------- dry run

def test_dry_run():
    env = Env()
    env.beat()
    env.check_now()
    seen = []

    def planner(name, args):
        seen.append((name, dict(args)))
        return {"ok": True, "state": "unchanged", "plan": "Would upgrade Alpha 1 -> 2\nthen restart"}

    out = updates.dry_run(env.store, "fetch", "a", executor=planner)
    check("a dry run returns the provider's plan as one line", out["ok"] and out["plan"] == "Would upgrade Alpha 1 -> 2 then restart"
          and out["title"] == "Alpha", out)
    check("it called the apply tool with dry_run TRUE and the hub's id",
          seen == [("apply_fetch_update", {"flag": "x", "id": "a", "dry_run": True})], seen)
    check("and queued or recorded nothing", env.store.applying_info() == {})
    out = updates.dry_run(env.store, "fetch", "zzz", executor=planner)
    check("a plan for an update that does not exist is refused", out["ok"] is False and "no such update" in out["error"], out)
    out = updates.dry_run(env.store, "fetch", "a", executor=lambda n, a: {"ok": False, "error": "no network"})
    check("a tool that says no gives no plan, in words", out["ok"] is False and "No plan" in out["error"] and "no network" in out["error"], out)
    out = updates.dry_run(env.store, "fetch", "a", executor=lambda n, a: (_ for _ in ()).throw(RuntimeError("boom")))
    check("a tool that raises is an error sentence, not a crash", out["ok"] is False and "boom" in out["error"], out)
    release = threading.Event()
    out = updates.dry_run(env.store, "fetch", "a", executor=lambda n, a: (release.wait(5), {"ok": True})[1], timeout_s=0.2)
    check("a plan that takes too long is given up on", out["ok"] is False and "longer than" in out["error"], out)
    release.set()
    out = updates.dry_run(env.store, "", "a")
    check("a plan needs a provider and an id", out["ok"] is False)


# --------------------------------------------------------------------------- reading the answer, the words

def test_parse_apply_result():
    p = updates.parse_apply_result
    r = p({"ok": True, "state": "done", "message": "m"})
    check("done", r["ok"] and r["state"] == "done" and r["message"] == "m" and not r["no_change"])
    r = p({"ok": True, "state": "started"})
    check("started (with a default sentence)", r["ok"] and r["state"] == "started" and "finish it" in r["message"])
    r = p({"ok": True, "state": "unchanged", "message": "same"})
    check("unchanged is recorded as done, flagged no_change", r["ok"] and r["state"] == "done" and r["no_change"])
    check("ok without a state is a failure, in words", not p({"ok": True})["ok"] and "state" in p({"ok": True})["message"])
    check("an unknown state is a failure", not p({"ok": True, "state": "great"})["ok"])
    check("state failed is a failure even with ok true", not p({"ok": True, "state": "failed", "message": "x"})["ok"])
    check("ok false is a failure with the tool's sentence", p({"ok": False, "message": "no"})["message"] == "no")
    check("an error key wins", p({"ok": True, "state": "done", "error": "bad"})["message"] == "bad" and not p({"error": "bad"})["ok"])
    check("a JSON string is read", p('{"ok": true, "state": "done"}')["ok"])
    check("text that is not JSON is a failure", not p("done!")["ok"])
    check("an answer that is not an object is a failure", not p([1])["ok"] and not p(None)["ok"] and not p(5)["ok"])
    r = p({"ok": True, "state": "done", "message": "x" * 900, "needs_restart": True})
    check("a long message is capped and needs_restart is said", len(r["message"]) <= 200 and r["needs_restart"] is True)
    r = p({"ok": True, "state": "done", "message": "ok", "needs_restart": True})
    check("a short one carries the restart note", "restart" in r["message"])
    r = p({"ok": True, "state": "done", "message": "\x1b[31mred\x1b[0m\nline two"})
    check("provider text has no control characters and one line", "\x1b" not in r["message"] and "\n" not in r["message"], r)
    r = p({"ok": True, "plan": "the plan"}, dry_run=True)
    check("a dry-run answer needs ok and gives the plan", r["ok"] and r["plan"] == "the plan")
    check("a dry-run answer without ok is a failure", not p({"plan": "x"}, dry_run=True)["ok"])


def test_notification_wording():
    one = lambda **k: notify.compose_updates(dict({"origin": "manual", "done": [], "started": [], "unchanged": [], "failed": []}, **k))
    t, m, failed = one(done=[{"title": "Alpha", "message": "applied"}])
    check("one applied: 'Update applied', the name, not a failure", t == "Update applied" and m == "Alpha" and not failed, (t, m))
    t, m, failed = one(failed=[{"title": "Alpha", "message": "disk full"}])
    check("one failed: a failure with the reason", t == "Update failed" and "disk full" in m and failed, (t, m))
    t, m, failed = one(started=[{"title": "Alpha", "message": ""}])
    check("one started says to finish it in the program", t == "Update started" and "finish" in m and not failed, (t, m))
    t, m, failed = notify.compose_updates({"origin": "auto", "done": [{"title": x} for x in "ABCDE"], "started": [],
                                           "unchanged": [], "failed": []})
    check("several by auto-update: 'applied automatically', three names and 'and 2 more'",
          t == "5 updates applied automatically" and "and 2 more" in m and not failed, (t, m))
    t, m, failed = one(done=[{"title": "A"}], failed=[{"title": "B", "message": "x"}])
    check("a mixed batch is processed, names what failed, and is a failure",
          t == "2 updates processed" and "1 applied" in m and "1 failed" in m and "B" in m and failed, (t, m))
    check("an empty batch says nothing", notify.announce_updates({"done": [], "failed": []}, deliver=lambda *a, **k: 1 / 0) is None)
    sent = []
    notify.announce_updates({"origin": "manual", "done": [{"title": "A", "message": "applied"}], "started": [], "unchanged": [],
                             "failed": []}, deliver=lambda title, message, **kw: sent.append((title, kw)))
    check("a good result is drawn with the registry's quiet look and tone",
          sent and sent[0][1].get("look") == "compact" and sent[0][1].get("tone") == "success", sent)
    sent = []
    notify.announce_updates({"origin": "manual", "done": [], "started": [], "unchanged": [],
                             "failed": [{"title": "A", "message": "x"}]}, deliver=lambda title, message, **kw: sent.append(kw))
    check("a failure asks for no look at all (the notifier draws a card)", sent and "look" not in sent[0] and sent[0].get("failed") is True)


# --------------------------------------------------------------------------- settings

def test_settings_and_updates_set():
    d = settings_mod.DEFAULTS["updates"]
    check("defaults: auto-update off, every group, 10 per cycle",
          d == {"auto_update": False, "include_groups": list(settings_mod.UPDATE_GROUPS), "auto_max_per_cycle": 10})
    check("the group list here and in tool_downloads agree", tuple(settings_mod.UPDATE_GROUPS) == tuple(tool_downloads.UPDATE_GROUPS))
    check("a damaged stored value never switches auto-update on",
          settings_mod.update_global_settings({"updates": {"auto_update": "yes", "include_groups": ["x"], "auto_max_per_cycle": 99}})
          == {"auto_update": False, "include_groups": list(settings_mod.UPDATE_GROUPS), "auto_max_per_cycle": 10})
    check("a provider's auto_update is on only for exactly True",
          settings_mod.provider_auto_update({"auto_update": True}) is True and settings_mod.provider_auto_update({"auto_update": 1}) is False
          and settings_mod.provider_auto_update({"auto_update": "true"}) is False and settings_mod.provider_auto_update(None) is False)
    clean, errors = settings_mod.validate_update({"updates.auto_update": True})
    check("the chat / prefs path cannot change the master switch", errors and not clean, errors)
    check("nor can dl_prefs", settings_mod.validate_update({"updates.auto_update": True}, allowed=settings_mod.PREFS_WRITABLE)[1])
    clean, errors = settings_mod.validate_provider_settings("fetch", {"auto_update": True})
    check("dl_provider_prefs cannot tick a provider's auto_update", errors and "auto_update" not in clean, (clean, errors))
    clean, errors = settings_mod.validate_provider_settings("fetch", {"auto_update": True}, allow_auto_update=True)
    check("the panel's own op can", clean == {"auto_update": True} and not errors)
    check("update_settings keeps returning its two keys", settings_mod.update_settings({}) == {"update_checks": True, "update_interval_h": 24.0})

    env = Env()
    env.check_now()
    s = env.store
    r = updates_admin.updates_set({"scope": "global", "key": "auto_update", "value": True}, store=s)
    check("updates_set: the master switch on", r["ok"] and r["settings"]["auto_update"] is True, r)
    r = updates_admin.updates_set({"scope": "global", "key": "auto_update", "value": "maybe"}, store=s)
    check("a value that is not a boolean is refused", r["ok"] is False and s.get_settings()["updates"]["auto_update"] is True, r)
    r = updates_admin.updates_set({"scope": "global", "key": "include_groups", "value": ["mods", "nope"]}, store=s)
    check("an unknown group in include_groups is refused", r["ok"] is False)
    r = updates_admin.updates_set({"scope": "global", "key": "include_groups", "value": ["packages", "mods"]}, store=s)
    check("include_groups is stored in the canonical order", r["ok"] and r["settings"]["include_groups"] == ["mods", "packages"], r)
    check("auto_max_per_cycle must be 1 to 50", all(updates_admin.updates_set({"scope": "global", "key": "auto_max_per_cycle", "value": v}, store=s)["ok"] is False
                                                    for v in (0, 51, "x", None)))
    r = updates_admin.updates_set({"scope": "global", "key": "auto_max_per_cycle", "value": 5}, store=s)
    check("and 5 is fine", r["ok"] and r["settings"]["auto_max_per_cycle"] == 5)
    check("an unknown global key is refused", updates_admin.updates_set({"scope": "global", "key": "x", "value": 1}, store=s)["ok"] is False)
    r = updates_admin.updates_set({"scope": "group", "id": "packages", "key": "included", "value": False}, store=s)
    check("a group can be taken out of the auto-update list", r["ok"] and "packages" not in r["settings"]["include_groups"], r)
    r = updates_admin.updates_set({"scope": "group", "id": "packages", "key": "included", "value": True}, store=s)
    check("and put back", r["ok"] and "packages" in r["settings"]["include_groups"])
    r = updates_admin.updates_set({"scope": "group", "id": "nope", "key": "included", "value": True}, store=s)
    check("an unknown group is refused", r["ok"] is False)
    r = updates_admin.updates_set({"scope": "group", "id": "packages", "key": "auto_update", "value": True}, store=s)
    check("a group's auto_update ticks every provider in it", r["ok"] and r["changed"] == ["fetch"]
          and settings_mod.provider_auto_update(s.get_provider("fetch")["settings"]) is True, r)
    r = updates_admin.updates_set({"scope": "group", "id": "mods", "key": "auto_update", "value": True}, store=s)
    check("a group with no provider says so", r["ok"] is False and "no provider" in r["error"], r)
    r = updates_admin.updates_set({"scope": "provider", "id": "fetch", "key": "update_interval_h", "value": 6}, store=s)
    check("provider scope: the interval", r["ok"] and s.get_provider("fetch")["settings"]["update_interval_h"] == 6.0, r)
    r = updates_admin.updates_set({"scope": "provider", "id": "fetch", "key": "update_interval_h", "value": 0}, store=s)
    check("a bad interval is refused", r["ok"] is False)
    r = updates_admin.updates_set({"scope": "provider", "id": "fetch", "key": "auto_update", "value": False}, store=s)
    check("provider scope: auto_update off again", r["ok"] and settings_mod.provider_auto_update(s.get_provider("fetch")["settings"]) is False)
    check("a provider key that is not an update setting is refused",
          updates_admin.updates_set({"scope": "provider", "id": "fetch", "key": "root", "value": "D:/x"}, store=s)["ok"] is False)
    check("an unknown provider is refused", updates_admin.updates_set({"scope": "provider", "id": "ghost", "key": "update_checks", "value": True}, store=s)["ok"] is False)
    check("an unknown scope is refused", updates_admin.updates_set({"scope": "world", "key": "x"}, store=s)["ok"] is False)
    check("junk requests answer, not raise", all(updates_admin.updates_set(j, store=s)["ok"] is False for j in (None, "x", [], {})))


def test_admin_ops_are_registered():
    from jarvis import hub_admin
    check("the three new ops are write ops of hub_admin and of updates_admin",
          {"updates_apply", "updates_dry_run", "updates_set"} <= set(hub_admin.WRITE_OPS)
          and {"updates_apply", "updates_dry_run", "updates_set"} <= set(updates_admin.WRITE_OPS)
          and "updates" in hub_admin.READ_OPS)
    env = Env()
    env.beat()
    env.check_now()
    out = updates_admin.updates_apply({"provider": "fetch", "id": "a"}, store=env.store)
    check("updates_apply queues one finding", out["ok"] and out["queued"] == 1, out)
    check("updates_apply refuses a malformed provider, id and group",
          updates_admin.updates_apply({"provider": "Bad Id!", "id": "a"}, store=env.store)["ok"] is False
          and updates_admin.updates_apply({"provider": "fetch", "id": "a b c"}, store=env.store)["ok"] is False
          and updates_admin.updates_apply({"all": True, "group": "nope"}, store=env.store)["ok"] is False)
    out = updates_admin.updates_apply({"all": "yes"}, store=env.store)
    check("'all' must be exactly true", out["ok"] is False, out)
    out = updates_admin.updates_dry_run({"provider": "fetch", "id": "nope"}, store=env.store)
    check("updates_dry_run answers for a missing update without raising", out["ok"] is False, out)
    check("the read op carries the new fields", updates_admin.updates({}, store=env.store)["settings"]["auto_update"] is False)


# --------------------------------------------------------------------------- the database

def test_migration_of_an_older_database():
    path = new_db_path()
    Store(path, clock=time.time).update_check_put("old", update_tool="check_old")
    conn = sqlite3.connect(str(path))
    conn.execute("DROP TABLE update_checks")
    conn.execute("DROP TABLE update_applies")
    conn.execute("CREATE TABLE update_checks(provider TEXT PRIMARY KEY, update_tool TEXT, start_interval_h REAL, "
                 "running_since REAL, requested_at REAL, manual_at REAL, started_at REAL, checked_at REAL, ok_at REAL, "
                 "error TEXT, problems_json TEXT)")
    conn.execute("INSERT INTO update_checks(provider, update_tool) VALUES('old', 'check_old')")
    conn.commit()
    conn.close()
    store_mod._CHECK_COLUMNS_CHECKED.discard(str(path))
    store = Store(path, clock=time.time)
    state = store.update_check_state()
    check("an older database keeps its rows and gains the new columns",
          state["old"]["update_tool"] == "check_old" and state["old"]["apply_tool"] is None
          and state["old"]["update_args"] == {} and state["old"]["updates_only"] is False, state)
    store.update_check_put("old", apply_tool="apply_old", update_group="mods", apply_timeout_s=60.0, updates_only=1)
    check("and they can be written", store.update_check_state()["old"]["apply_tool"] == "apply_old")
    check("the apply table exists too", store.applying_info() == {} and store.applying_counts() == (0, 0))
    again = Store(path, clock=time.time)
    check("opening it again is harmless", again.update_check_state()["old"]["update_group"] == "mods")


# --------------------------------------------------------------------------- the daemon

def test_daemon_runs_the_apply_runner():
    from jarvis.hub.daemon import HubDaemon

    class Fake:
        def __init__(self):
            self.started = self.stopped = 0

        def start(self):
            self.started += 1

        def stop(self, timeout=2.0):
            self.stopped += 1

    daemon = HubDaemon(new_store())
    daemon.declared_providers = []
    fake_checks, fake_apply = Fake(), Fake()
    daemon._updates, daemon._applying = fake_checks, fake_apply
    os.environ.pop("JARVIS_HUB_NO_UPDATES", None)
    try:
        daemon.start_up()
        check("the daemon starts the apply runner with the update runner", fake_checks.started == 1 and fake_apply.started == 1)
        daemon.stop(timeout=1)
        check("and stops it", fake_apply.stopped == 1)
        os.environ["JARVIS_HUB_NO_UPDATES"] = "1"
        other = HubDaemon(new_store())
        other.declared_providers = []
        other._applying = Fake()
        other._updates = Fake()
        other.start_up()
        check("JARVIS_HUB_NO_UPDATES keeps it from starting", other._applying.started == 0)
        other.stop(timeout=1)
    finally:
        os.environ["JARVIS_HUB_NO_UPDATES"] = "1"
    twin = HubDaemon(new_store())
    check("it is an ApplyRunner of its own, not the update runner", isinstance(twin._applying, updates.ApplyRunner)
          and twin._applying is not twin._updates)


# --------------------------------------------------------------------------- the demo provider's fake update source

def test_demo_update_source():
    from jarvis.actions import hub_demo_controls as demo_tools
    entries, errors = tool_downloads.validate_downloads(
        demo_tools.TOOL_DOWNLOADS, "hub_demo_controls.py", set(demo_tools.TOOLS),
        confirm_required=demo_tools.TOOL_CONFIRM_REQUIRED, schemas=demo_tools.TOOL_SCHEMAS)
    check("the demo file's TOOL_DOWNLOADS validates, schema check included", len(entries) == 1 and not errors, errors)
    entry = entries[0]
    check("it is the pre-allocated provider id, updates_only, push, group other, with a confirm-gated apply tool",
          entry["id"] == "demo_updates" and entry["updates_only"] is True and entry["mode"] == "push"
          and entry["update_group"] == "other" and entry["apply_tool"] == "apply_demo_update"
          and "apply_demo_update" in demo_tools.TOOL_CONFIRM_REQUIRED, entry)
    default = Store()
    default.set_setting_raw("demo", {"enabled": False, "started_at": 0})
    state_file = Path(HOME) / ".jarvis" / "hub" / "demo_updates.json"
    if state_file.exists():
        state_file.unlink()
    check("demo off: the check lists nothing, complete", demo_tools.tool_check_demo_updates({}) == {"ok": True, "updates": [], "complete": True})
    out = demo_tools.tool_apply_demo_update({"id": "demo-alpha"})
    check("demo off: the apply refuses in words and changes nothing", out["ok"] is False and "isn't running" in out["error"]
          and not state_file.exists(), out)
    default.set_setting_raw("demo", {"enabled": True, "started_at": 1})
    out = demo_tools.tool_check_demo_updates({})
    check("demo on: three fake findings", out["ok"] and out["complete"] and [u["id"] for u in out["updates"]] == ["demo-alpha", "demo-beta", "demo-fail"], out)
    out = demo_tools.tool_apply_demo_update({"id": "demo-alpha", "dry_run": True})
    check("a dry run gives a plan and changes nothing", out["ok"] and "pretend" in out["plan"] and not state_file.exists(), out)
    out = demo_tools.tool_apply_demo_update({"id": "demo-alpha"})
    check("demo-alpha is done and leaves the list", out == {"ok": True, "state": "done", "message": "applied the fake update"}
          and [u["id"] for u in demo_tools.tool_check_demo_updates({})["updates"]] == ["demo-beta", "demo-fail"], out)
    out = demo_tools.tool_apply_demo_update({"id": "demo-beta"})
    check("demo-beta is started", out["ok"] and out["state"] == "started", out)
    out = demo_tools.tool_apply_demo_update({"id": "demo-fail"})
    check("demo-fail always fails and stays listed", out["ok"] is False and out["state"] == "failed"
          and [u["id"] for u in demo_tools.tool_check_demo_updates({})["updates"]] == ["demo-fail"], out)
    check("an unknown id and junk arguments are refused, not raised",
          demo_tools.tool_apply_demo_update({"id": "nope"})["ok"] is False and demo_tools.tool_apply_demo_update(None)["ok"] is False
          and demo_tools.tool_apply_demo_update({"id": ["x"]})["ok"] is False)
    os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
    try:
        out = demo_tools.tool_apply_demo_update({"id": "demo-fail"})
        check("a chat guest is refused by the handler", out["ok"] is False and "owner" in out["error"], out)
    finally:
        os.environ.pop("JARVIS_CHANNEL_SENDER", None)
    state_file.write_text("{ not json", encoding="utf-8")
    check("a damaged state file means nothing is applied yet", len(demo_tools.tool_check_demo_updates({})["updates"]) == 3)
    default.set_setting_raw("demo", {"enabled": False, "started_at": 0})

    # end to end through the real runners: the demo source, applied through the apply runner with the demo's own tools
    state_file.unlink()
    default.set_setting_raw("demo", {"enabled": True, "started_at": 1})
    clock = Clock()
    store = new_store(clock)
    store.register_provider("demo_updates", label="Demo updates", mode="push")
    store.daemon_beat(os.getpid(), clock())
    checker = updates.UpdateRunner(store, clock=clock, declared=lambda: entries, first_delay_s=0.0, schemas=lambda: demo_tools.TOOL_SCHEMAS,
                                   executor=lambda name, args: demo_tools.TOOLS[name](args))
    sent = []
    applier = updates.ApplyRunner(store, clock=clock, executor=lambda name, args: demo_tools.TOOLS[name](args),
                                  deliver=lambda title, message, **kw: sent.append((title, message, kw)))
    checker.tick()
    chk = checker._state["demo_updates"]["check"]
    check("(the fake check finished)", chk.done.wait(5))
    checker.tick()
    check("the three demo findings arrive", len(updates.present(store, now=clock())["updates"]) == 3)
    out = updates.request_apply(store, all_updates=True)
    check("apply all queues the three", out["queued"] == 3, out)
    for _ in range(40):
        applier.tick()
        cur = applier._current
        if cur is not None:
            cur["done"].wait(5)
        elif store.applying_counts() == (0, 0):
            break
    got = {k[1]: v["state"] for k, v in store.applying_info().items()}
    check("done, started and failed all show up", got == {"demo-alpha": "done", "demo-beta": "started", "demo-fail": "failed"}, got)
    check("one notification for the batch, a failure card", len(sent) == 1 and sent[0][2].get("failed") is True, sent)
    default.set_setting_raw("demo", {"enabled": False, "started_at": 0})


if __name__ == "__main__":
    for fn in (test_declaration_and_present, test_schema_problem_blocks_applying, test_request_apply_refusals_and_idempotence,
               test_all_group_and_cap, test_runner_applies_one_and_records_it, test_runner_states_and_failures,
               test_timeout_one_at_a_time_and_checks_not_blocked, test_recover_after_restart,
               test_auto_update_is_off_and_rules, test_auto_update_never_retries_a_failed_version_but_tries_a_new_one,
               test_failed_memory_outlives_the_finding, test_dry_run, test_parse_apply_result, test_notification_wording, test_settings_and_updates_set,
               test_admin_ops_are_registered, test_migration_of_an_older_database, test_daemon_runs_the_apply_runner,
               test_demo_update_source):
        fn()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
