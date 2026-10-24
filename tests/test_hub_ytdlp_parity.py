"""Download Hub, phase 8 (yt-dlp Level B): the option-builder parity test.

The plan's guard for the Level B refactor: "`ytdl_download` options before and after the refactor
are identical".  `tests/_ytdl_opts_golden.json` holds what the ORIGINAL (pre-refactor)
`ytdl_download` did for a matrix of arguments: the exact options it handed to `yt_dlp.YoutubeDL`
and exactly what it returned.  This test replays the matrix against the current code with a
recording stand-in for `YoutubeDL`, so no network, yt-dlp download or ffmpeg is needed, and checks

  * `ytdl_download` still builds the same options and returns the same answers (including every
    validation error and its wording);
  * `build_ydl_opts(arguments, job_dir)` -- the shared builder the hub's worker uses -- builds the
    same options as `ytdl_download` does, for every case that gets as far as running yt-dlp;
  * a bad argument is an error from the builder, with the same message `ytdl_download` gives;
  * `download_with_hooks` adds `progress_hooks` / `postprocessor_hooks` and nothing else.

Run: python tests/test_hub_ytdlp_parity.py
Regenerate the golden file ONLY on purpose (it records current behaviour as the truth):
     python tests/test_hub_ytdlp_parity.py --regen
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-ytdl-parity-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
GOLDEN = ROOT / "tests" / "_ytdl_opts_golden.json"

import importlib.util  # noqa: E402

if importlib.util.find_spec("yt_dlp") is None:
    # The whole file replays the option matrix against a stand-in for yt_dlp.YoutubeDL, so it needs the
    # module itself. Say so out loud and stop; do not let a missing module look like a pass.
    print("SKIPPED test_hub_ytdlp_parity: yt_dlp is not installed; this is not a pass.")
    sys.exit(0)

import yt_dlp  # noqa: E402

from jarvis import ytdl_tools  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


URL = "https://video.example/watch?v=abc"
CASES = [
    {},
    {"url": 5},
    {"url": URL},
    {"url": URL, "mode": "audio"},
    {"url": URL, "mode": " AUDIO "},
    {"url": URL, "mode": "movie"},
    {"url": URL, "quality": "720p"},
    {"url": URL, "quality": "id:137"},
    {"url": URL, "quality": "bv*[height<=900]+ba"},
    {"url": URL, "quality": "weird-name"},
    {"url": URL, "container": "mkv"},
    {"url": URL, "container": "GIF"},
    {"url": URL, "video_codec": "av1"},
    {"url": URL, "video_codec": "x265"},
    {"url": URL, "mode": "audio", "audio_format": "flac", "audio_quality": "0"},
    {"url": URL, "mode": "audio", "audio_format": "wma"},
    {"url": URL, "audio_quality": "320K"},
    {"url": URL, "embed_thumbnail": True, "embed_metadata": True, "embed_chapters": True},
    {"url": URL, "write_subs": True, "sub_langs": "en, es"},
    {"url": URL, "write_subs": True, "embed_subs": True},
    {"url": URL, "mode": "audio", "embed_subs": True},
    {"url": URL, "sponsorblock_remove": "sponsor, selfpromo"},
    {"url": URL, "sponsorblock_remove": " , "},
    {"url": URL, "rate_limit_kbps": 500},
    {"url": URL, "rate_limit_kbps": "250"},
    {"url": URL, "rate_limit_kbps": "fast"},
    {"url": URL, "rate_limit_kbps": 0},
    {"url": URL, "playlist": True},
    {"url": URL, "playlist": True, "playlist_items": "2-4"},
    {"url": URL, "skip_if_downloaded": True},
    {"url": URL, "output_dir": "some/where"},
    {"url": URL, "mode": "audio", "audio_format": "opus", "quality": "480p", "container": "webm",
     "video_codec": "vp9", "embed_metadata": True, "rate_limit_kbps": 100, "playlist": True,
     "playlist_items": "1,3,5", "skip_if_downloaded": True, "sponsorblock_remove": "sponsor"},
]


class _Recorder:
    """Stands in for yt_dlp.YoutubeDL: remembers its options, 'downloads' one file."""
    calls = []

    def __init__(self, opts):
        self.opts = opts
        _Recorder.calls.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        out = Path(self.opts["outtmpl"]).parent
        (out / "Fake Title [abc].mp4").write_bytes(b"x" * 10)
        return {"title": "Fake Title", "duration": 61, "id": "abc"}

    @staticmethod
    def sanitize_info(info):
        return info


def _normalise(value, job_dir, job_id):
    """Make a recorded structure independent of the temp folder and the random job id."""
    text = json.dumps(value, sort_keys=True, default=str)
    text = text.replace(json.dumps(str(job_dir))[1:-1], "<JOB>")
    text = text.replace(str(job_dir), "<JOB>")
    if job_id:
        text = text.replace(job_id, "<ID>")
    text = text.replace(json.dumps(str(ytdl_tools.DOWNLOAD_DIR))[1:-1], "<DLDIR>")
    text = text.replace(str(ytdl_tools.DOWNLOAD_DIR), "<DLDIR>")
    text = text.replace(HOME, "<HOME>")

    def slashes(node):                       # Windows paths compare equal to POSIX ones
        if isinstance(node, str):
            return node.replace("\\", "/")
        if isinstance(node, list):
            return [slashes(x) for x in node]
        if isinstance(node, dict):
            return {k: slashes(v) for k, v in node.items()}
        return node

    return slashes(json.loads(text))


def _install_stubs():
    yt_dlp.YoutubeDL = _Recorder
    ytdl_tools._ffmpeg_path = lambda: "/usr/bin/ffmpeg"
    ytdl_tools._emit_media = lambda *a, **k: None


def run_case(case, fn=None):
    """-> {"opts": recorded options or None, "result": what the function returned}."""
    fn = fn or ytdl_tools.ytdl_download
    _Recorder.calls.clear()
    # a fresh custom folder per case so folders from earlier cases never leak in
    case = dict(case)
    if case.get("output_dir"):
        case["output_dir"] = str(Path(HOME) / case["output_dir"])
    result = fn(case)
    opts = _Recorder.calls[-1] if _Recorder.calls else None
    job_dir = Path(opts["outtmpl"]).parent if opts else Path("/nonexistent-job")
    job_id = (result or {}).get("job_id") if isinstance(result, dict) else None
    result = _normalise(result, job_dir, job_id)
    return {"opts": _normalise(opts, job_dir, job_id) if opts else None, "result": result}


def main():
    _install_stubs()
    recorded = [run_case(c) for c in CASES]
    if "--regen" in sys.argv:
        GOLDEN.write_text(json.dumps({"cases": CASES, "expected": recorded}, indent=1, sort_keys=True))
        print("wrote", GOLDEN)
        shutil.rmtree(HOME, ignore_errors=True)
        return 0

    golden = json.loads(GOLDEN.read_text())
    check("the golden file covers the whole matrix", golden["cases"] == json.loads(json.dumps(CASES)))
    for case, want, got in zip(CASES, golden["expected"], recorded):
        label = json.dumps(case, sort_keys=True)[:90]
        check("ytdl_download options unchanged: %s" % label, got["opts"] == want["opts"])
        check("ytdl_download answer unchanged: %s" % label, got["result"] == want["result"])

    # --- the shared builder -------------------------------------------------------------------
    job = Path(HOME) / "builder-job"
    for case, want in zip(CASES, golden["expected"]):
        label = json.dumps(case, sort_keys=True)[:90]
        args = dict(case)
        args.pop("url", None)
        args.pop("output_dir", None)
        opts, err = ytdl_tools.build_ydl_opts(args, job)
        ran = want["opts"] is not None
        if ran:
            check("build_ydl_opts builds the same options: %s" % label,
                  err is None and _normalise(opts, job, None) == want["opts"])
        else:
            is_arg_error = isinstance(want["result"], dict) and want["result"].get("error") and "ok" not in want["result"] \
                and "url is required" not in want["result"]["error"]
            if is_arg_error:
                check("build_ydl_opts refuses what ytdl_download refuses, same words: %s" % label,
                      err == want["result"]["error"] and opts == {})
    check("build_ydl_opts takes the folder from output_dir when none is passed",
          ytdl_tools.build_ydl_opts({"output_dir": str(job)})[0]["outtmpl"].startswith(str(job)))
    check("build_ydl_opts without any folder says so",
          ytdl_tools.build_ydl_opts({"mode": "audio"})[1] and ytdl_tools.build_ydl_opts({"mode": "audio"})[0] == {})

    # --- the hooks are the only difference ----------------------------------------------------
    hook = lambda d: None  # noqa: E731
    pp_hook = lambda d: None  # noqa: E731
    args = {"url": URL, "mode": "audio", "audio_format": "mp3", "output_dir": str(Path(HOME) / "hooked")}
    plain = run_case(args)
    _Recorder.calls.clear()
    res = ytdl_tools.download_with_hooks(args, progress_hooks=[hook], postprocessor_hooks=[pp_hook])
    opts = _Recorder.calls[-1]
    check("download_with_hooks adds progress_hooks, postprocessor_hooks and quiets the console bar",
          opts.get("progress_hooks") == [hook] and opts.get("postprocessor_hooks") == [pp_hook]
          and opts.get("noprogress") is True and res.get("ok"))
    stripped = {k: v for k, v in opts.items() if k not in ("progress_hooks", "postprocessor_hooks", "noprogress")}
    check("...and nothing else differs from the plain call",
          _normalise(stripped, Path(HOME) / "hooked", None) == plain["opts"])
    _Recorder.calls.clear()
    ytdl_tools.download_with_hooks(args)
    check("without hooks no hook keys (and no noprogress) are added at all",
          not ({"progress_hooks", "postprocessor_hooks", "noprogress"} & set(_Recorder.calls[-1])))
    bg = ytdl_tools.download_with_hooks(dict(args, background=True))
    check("download_with_hooks never takes the background (queue) path", bg.get("ok") and "queued" not in bg)

    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(HOME, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
