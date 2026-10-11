"""L.55 - backlog manual order (`order`, `backlog.move`, `backlog-move`) and the
`--key=value` form the web panel now sends.

Nothing here touches the real ~/.jarvis: BACKLOG_FILE is pointed at a temp dir.
Run: python3 tests/test_backlog_order.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import backlog, workspace_cli  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


TMP = Path(tempfile.mkdtemp())
HOME = TMP / "home"


def fresh():
    backlog.BACKLOG_FILE = TMP / f"b{fresh.n}.json"
    fresh.n += 1


fresh.n = 0


def titles(state):
    return [i["title"] for i in backlog.board()[state]]


def mk(title, state="todo", priority="normal"):
    item, err = backlog.add(title, state=state, priority=priority)
    assert item, err
    return item


def test_default_order_is_unchanged():
    fresh()
    a = mk("aaa", priority="low")
    b = mk("bbb", priority="high")
    c = mk("ccc")
    check("no manual order: priority decides, as before", titles("todo") == ["bbb", "ccc", "aaa"])
    check("no item gets a rank just from being added", all("order" not in i for i in (a, b, c)))


def test_move_within_column():
    fresh()
    a, b, c, d = (mk(t) for t in ("a1", "b1", "c1", "d1"))
    base = titles("todo")
    ids = {i["title"]: i["id"] for i in backlog.board()["todo"]}
    # Items added in the same instant tie on priority and time; which comes first then
    # depends on the clock's resolution. Move whichever is last to the top, so the test
    # means the same on every machine.
    mover = base[-1]
    item, err = backlog.move(ids[mover], before=ids[base[0]])
    check("move returns the item", item is not None and err == "")
    check("moved to the top", titles("todo")[0] == mover)
    expect = [mover] + [t for t in base if t != mover]
    check("everything else keeps its relative order", titles("todo") == expect)
    backlog.move(ids[mover])  # no anchor = bottom
    check("no anchor means the bottom", titles("todo")[-1] == mover)
    ranks = [i["order"] for i in backlog.board()["todo"]]
    check("the whole column was renumbered 1..n", ranks == [1, 2, 3, 4])


def test_ranked_before_unranked_and_new_items_go_last():
    fresh()
    a, b = mk("one"), mk("two")
    backlog.move(a["id"])  # ranks both
    new = mk("three", priority="high")
    check("a new high-priority card does not jump a manual order", titles("todo")[-1] == "three")
    check("ranked cards first, in rank order", titles("todo")[:2] == ["two", "one"])
    del new


def test_move_across_columns():
    fresh()
    t1 = mk("t1")
    d1 = mk("d1", state="doing")
    d2 = mk("d2", state="doing")
    item, err = backlog.move(t1["id"], state="doing", before=d2["id"])
    check("cross-column move ok", item is not None)
    check("lands at the chosen position", titles("doing").index("t1") == titles("doing").index("d2") - 1)
    check("left the old column", titles("todo") == [])
    blocked = mk("bl", state="blocked")
    backlog.update(blocked["id"], blocked_on="review")
    moved, _ = backlog.move(blocked["id"], state="todo")
    check("leaving Blocked clears blocked_on", moved["blocked_on"] == "" and moved["state"] == "todo")
    done, _ = backlog.move(t1["id"], state="done")
    check("moving to Done sets done_at", bool(done["done_at"]))
    back, _ = backlog.move(t1["id"], state="todo")
    check("leaving Done clears done_at", back["done_at"] is None)


def test_move_errors():
    fresh()
    a = mk("aa")
    b = mk("bb", state="doing")
    check("unknown id", backlog.move("b_nope")[0] is None)
    item, err = backlog.move(a["id"], before=a["id"])
    check("not before itself", item is None and "itself" in err)
    item, err = backlog.move(a["id"], state="todo", before=b["id"])
    check("anchor must be in the destination column", item is None and "isn't in" in err)
    check("a failed move changed nothing", titles("todo") == ["aa"] and "order" not in backlog.board()["todo"][0])
    check("titles are not ids", backlog.move("aa")[0] is None)


def test_state_change_clears_rank():
    fresh()
    a, b = mk("a2"), mk("b2")
    backlog.move(a["id"])
    check("ranked", all("order" in i for i in backlog.board()["todo"]))
    backlog.update(a["id"], state="doing")
    check("update(state) drops the rank", "order" not in backlog.board()["doing"][0])


def test_hand_edited_garbage_rank_does_not_crash():
    fresh()
    a, b = mk("g1"), mk("g2")
    data = json.loads(backlog.BACKLOG_FILE.read_text())
    data["items"][0]["order"] = "first"
    data["items"][1]["order"] = float("nan")
    backlog.BACKLOG_FILE.write_text(json.dumps(data))
    check("a non-number rank reads as no rank", len(backlog.board()["todo"]) == 2)


def test_opts_equals_form():
    p, f = workspace_cli._opts(["b_1", "--note=--careful", "--title=", "--state", "doing", "--x"])
    check("--k=v keeps a value that starts with --", f["note"] == ["--careful"])
    check("--k= is an empty value", f["title"] == [""])
    check("--k v still works", f["state"] == ["doing"] and p == ["b_1"])
    check("a bare flag is still a boolean", f["x"] == [True])
    p, f = workspace_cli._opts(["x", "--env", "K=V"])
    check("a space-separated K=V value is untouched", f["env"] == ["K=V"])


def run_cli(*args):
    env_home = str(HOME)
    return subprocess.run(
        [sys.executable, "-m", "jarvis", *args],
        cwd=str(ROOT / "jarvis-cli"), capture_output=True, text=True,
        env={**__import__("os").environ, "HOME": env_home, "USERPROFILE": env_home},
    )


def test_cli_end_to_end():
    HOME.mkdir(exist_ok=True)
    r = run_cli("backlog-add", "--title=--odd title", "--note=--n")
    out = json.loads(r.stdout)
    check("backlog-add accepts a title that starts with --", out.get("ok") and out["item"]["title"] == "--odd title" and out["item"]["note"] == "--n")
    first = out["item"]["id"]
    second = json.loads(run_cli("backlog-add", "second one").stdout)["item"]["id"]
    r = run_cli("backlog-move", second, f"--before={first}")
    check("backlog-move exits 0", r.returncode == 0)
    board = json.loads(run_cli("backlog-board").stdout)["board"]["todo"]
    check("board shows the new order", [i["id"] for i in board] == [second, first])
    r = run_cli("backlog-update", first, "--note=--changed", "--title=new title")
    upd = json.loads(r.stdout)["item"]
    check("backlog-update takes --k=v", upd["note"] == "--changed" and upd["title"] == "new title")
    r = run_cli("backlog-move", "b_zzzzzz")
    check("backlog-move on an unknown id fails", r.returncode != 0 and json.loads(r.stdout)["ok"] is False)


for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]:
    fn()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
