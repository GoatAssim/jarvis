"""The hosters engine: a file-host PAGE link in, a finished file out (link-resolver plan, section 3).

It moves no bytes itself.  Each attempt it
  1. finds the resolver for the row (the row's `options.resolver` first, else by host),
  2. asks it for the real file link (`hub/hosters/<host>.py`),
  3. hands that link and any headers the resolver learned to `http.stream_to_file`, which does the transfer:
     pause, resume, segments, progress, the SSRF guard, safe names, the library-root rule.

The row keeps the PAGE link as its `source`.  A resolved link is signed and expires, so resolving happens again
at every attempt: a retry, a resume and a restart all get a fresh link without any code of their own.

What a resolver says maps to an outcome here and only here (resolvers never decide retry policy):
    NeedsWait / QuotaExceeded   -> failed, retryable, after the host's own wait
    Transient                   -> failed, retryable
    NeedsHuman / Dead / Unsupported / ResolveFailed / a refused link -> failed, not retryable, with words to act on
    Stopped                     -> paused / cancelled / requeued, like any engine
A list (a folder, a project) becomes a group of child rows, each with its own page link and `options.resolver`.
When the file server refuses a freshly resolved link (HTTP 401, 403 or 410: it expired, or the Referer was
wrong) the attempt is retried so the next one resolves again; `hosters.max_retries` bounds that.
"""

import re

from .. import hosters, naming, netguard, router
from .base import Engine, Outcome, RouteRule
from . import http as http_engine

MAX_CHILDREN = 200                  # a folder bigger than this is cut here (and says so), never queued whole
_REFUSED = re.compile(r"HTTP (?:401|403|410)\b")
_INHERIT_NOT = ("filename", "resolver")   # one file name must not become every child's name


def _match_hoster(text, settings):
    text = (text or "").strip()
    resolver = hosters.find(text, settings)
    if resolver is None:
        return None
    return {"reason": "%s link (hosters engine)" % resolver.label, "kind": "file", "source": text}


ROUTE_RULES = [RouteRule("hosters", 60, _match_hoster)]


def _stop_outcome(kind):
    if kind == "pause":
        return Outcome.paused()
    if kind == "cancel":
        return Outcome.cancelled()
    return Outcome.requeue()


def _minutes(seconds):
    seconds = int(seconds)
    return "%d min" % -(-seconds // 60) if seconds >= 90 else "%d s" % seconds


def _error_outcome(exc, resolver, page_url):
    """A resolver's exception -> an Outcome.  The one place retry policy for hosters lives."""
    from ..hosters import base as hb
    label = resolver.label
    if isinstance(exc, hb.NeedsWait):
        return Outcome.failed("%s asks for a wait of %s before the download%s" % (
            label, _minutes(exc.seconds), (": " + exc.message) if exc.message else ""),
            retryable=True, delay_s=exc.seconds)
    if isinstance(exc, hb.QuotaExceeded):
        return Outcome.failed("%s: %s (trying again in %s)" % (label, exc.message or "download limit reached",
                                                               _minutes(exc.retry_after_s)),
                              retryable=True, delay_s=exc.retry_after_s)
    if isinstance(exc, hb.Transient):
        return Outcome.failed("%s: %s" % (label, exc.message or "a temporary problem"), retryable=True)
    if isinstance(exc, hb.NeedsHuman):
        where = exc.open_url or page_url
        return Outcome.failed("Needs you: %s. Open %s in your browser to finish." % (
            exc.reason or "a check only a person can pass", netguard.mask_url(where)))
    if isinstance(exc, hb.Dead):
        return Outcome.failed(exc.message or "%s: the file isn't available" % label)
    if isinstance(exc, hb.Unsupported):
        return Outcome.failed(exc.message or "%s: this kind of link isn't supported" % label)
    if isinstance(exc, hb.ResolveFailed):
        return Outcome.failed("%s: %s. hoster_doctor can check whether the site changed." % (
            label, exc.message or "the page wasn't understood"))
    return Outcome.failed("%s: %s" % (label, exc.message or type(exc).__name__))


def _pick_resolver(item, settings):
    """The row's own `options.resolver` (children carry it), else the host.  Disabled resolvers don't count."""
    own = (item.get("options") or {}).get("resolver")
    if isinstance(own, str):
        resolver = hosters.get(own)
        if resolver is not None and hosters.is_enabled(own, settings):
            return resolver
    return hosters.find(item.get("source_url") or "", settings)


def _problem_with(resolved, settings):
    """Why a resolver's answer can't be used, or None.  The resolved link passes the same guard as any link."""
    if not isinstance(resolved, hosters.Resolved):
        return "the resolver returned something that isn't a link"
    if resolved.decrypt:
        return "this host's files need local decryption (%s), which isn't built yet" % resolved.decrypt
    try:
        netguard.check_url(resolved.url, settings.get("allow_private_hosts") or [])
    except netguard.GuardError as exc:
        return "the link the host gave was refused: %s" % exc
    return None


class HostersEngine(Engine):
    name = "hosters"
    label = "File hosts"
    supports_pause = True

    def available(self):
        try:
            import requests  # noqa: F401
        except ImportError:
            return False, "the `requests` package is missing"
        return True, ""

    def run(self, item, ctx):
        try:
            return self._run(item, ctx)
        except Exception as exc:  # noqa: BLE001 - say what happened instead of letting the daemon guess
            return Outcome.failed("the hosters engine hit %s: %s" % (type(exc).__name__, str(exc)[:200]))

    def _run(self, item, ctx):
        from ..hosters import base as hb
        settings = ctx.settings
        page_url = item.get("source_url") or ""
        resolver = _pick_resolver(item, settings)
        if resolver is None:
            return Outcome.failed("no file-host resolver takes this link any more: it was switched off in "
                                  "settings, or its host list changed")
        stop = ctx.stop_requested()
        if stop:
            return _stop_outcome(stop)
        ctx.update(message="finding the file link on %s" % resolver.label)
        rctx = hosters.make_context(settings, stop=ctx.stop_requested)
        try:
            result = resolver.resolve(page_url, rctx)
        except hb.Stopped as stopped:
            return _stop_outcome(stopped.kind)
        except hb.HosterError as exc:
            return _error_outcome(exc, resolver, page_url)
        except netguard.GuardError as exc:
            return Outcome.failed("%s: the link was refused: %s" % (resolver.label, exc))
        finally:
            rctx.fetch.close()
        stop = ctx.stop_requested()
        if stop:
            return _stop_outcome(stop)
        if isinstance(result, (list, tuple)):
            return self._expand(item, ctx, resolver, list(result))
        problem = _problem_with(result, settings)
        if problem:
            return Outcome.failed("%s: %s" % (resolver.label, problem))
        ctx.event("info", "%s gave the file link %s" % (resolver.label, netguard.mask_url(result.url)))
        return self._transfer(item, ctx, resolver, result)

    def _transfer(self, item, ctx, resolver, resolved):
        view = dict(item)
        opts = dict(item.get("options") or {})
        fields = {"message": None}
        if resolved.filename and not opts.get("filename"):
            opts["filename"] = resolved.filename          # stream_to_file sanitises it like any name
            fields["title"] = naming.sanitize_filename(resolved.filename)
        view["options"] = opts
        ctx.update(**fields)
        wanted = http_engine.wanted_segments(ctx, item)
        cap = resolver.max_segments
        segments = min(wanted, int(cap)) if cap else wanted
        out = http_engine.stream_to_file(ctx, view, resolved.url, segments=segments,
                                         trusted_headers=resolved.headers)
        if out.kind == Outcome.FAILED and not out.retryable and _REFUSED.search(out.error or ""):
            return Outcome.failed("%s refused the file link (%s): asking for a fresh one" % (
                resolver.label, _REFUSED.search(out.error).group(0)), retryable=True, delay_s=3)
        return out

    def _expand(self, item, ctx, resolver, results):
        """A folder or project: one child row per file, each with its own page link and `options.resolver`."""
        children = [r for r in results if isinstance(r, hosters.Resolved) and r.page_url]
        if not children:
            return Outcome.failed("%s: the link held nothing to download" % resolver.label)
        if len(children) > MAX_CHILDREN:
            ctx.event("warn", "%s listed %d files; only the first %d were queued" % (
                resolver.label, len(children), MAX_CHILDREN))
            children = children[:MAX_CHILDREN]
        store = ctx.store
        group_id = item.get("group_id") or store.ensure_group(
            "%s: %s" % (resolver.label, (item.get("title") or "folder")[:80]), kind="batch",
            meta={"source": netguard.mask_url(item.get("source_url") or "")})
        base_opts = {k: v for k, v in (item.get("options") or {}).items() if k not in _INHERIT_NOT}
        made = 0
        for child in children:
            opts = dict(base_opts, resolver=resolver.id)
            _, created = store.add_item(
                engine="hosters", source_url=child.page_url, title=naming.sanitize_filename(child.filename or "")
                if child.filename else "", kind=naming.kind_for(child.filename or ""), options=opts,
                group_id=group_id, dedupe_key=router.dedupe_key("hosters", child.page_url, opts))
            made += 1 if created else 0
        ctx.event("info", "%s: %d file(s) queued as a group (%d were already queued)" % (
            resolver.label, made, len(children) - made))
        ctx.update(message=None)
        return Outcome.done()


ENGINE = HostersEngine()
