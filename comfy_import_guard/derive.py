"""Derive a ``requires-comfyui`` floor (and ceiling) for one custom-node pack.

A release satisfies a pack when every hard reference resolves *and* every
hard call binds. Presence alone is not enough: a pack calling
``pick_operations(scaled_fp8=...)`` needs a ComfyUI old enough to still have
that parameter, and nothing about the symbol's presence says so.
"""

from dataclasses import dataclass

from .errors import BadInputError
from .extract import STAR, scan_pack
from .resolve import Resolver
from .signature import PARSE_FAILED, SignatureResolver, check_call_sites
from .version import sort_tags, tag_to_version

SIGNATURE = "SIGNATURE"


@dataclass(frozen=True)
class _Problem:
    """One reason a release does not satisfy the pack."""

    key: tuple           # identity across tags, for the per-item binary search
    dotted: str
    file: str
    line: int
    status: str
    detail: str = ""

    @property
    def is_call(self):
        return self.status == SIGNATURE

    @property
    def label(self):
        return self.dotted + " (call)" if self.is_call else self.dotted


def hard_references(scan):
    """References that must resolve: not star, not guarded by try/except."""
    seen = {}
    for ref in scan.references:
        if ref.kind == STAR or ref.soft:
            continue
        seen.setdefault((ref.module, ref.symbol, ref.kind), ref)
    return list(seen.values())


def hard_calls(scan):
    """Calls that must bind: not guarded, deduped by the shape they pass."""
    seen = {}
    for call in scan.call_sites:
        if call.soft:
            continue
        seen.setdefault((call.file, call.dotted, call.nargs, call.keywords), call)
    return list(seen.values())


class _Prober:
    """Answers "does this release satisfy the pack" once per tag, then caches."""

    def __init__(self, repo, refs, calls=()):
        self.repo = repo
        self.refs = refs
        self.calls = list(calls)
        self.cache = {}
        self.probes = 0
        self.unparsed = set()

    def missing_at(self, tag):
        """Everything that fails at ``tag``: absent names and unbindable calls."""
        if tag in self.cache:
            return self.cache[tag]
        self.probes += 1
        resolver = Resolver(self.repo, tag)
        out = []
        for ref in self.refs:
            res = resolver.resolve(ref)
            if res.breaking:
                out.append(_Problem(
                    key=("ref", ref.module, ref.symbol, ref.kind),
                    dotted=_dotted(res), file=ref.file, line=ref.lineno,
                    status=res.status, detail=res.detail))
        if self.calls:
            # check_call_sites is shared with the check engine, so a version
            # shim is dismissed here exactly as it is there. That matters more
            # in this command: one wrongly hard call makes every release look
            # unsatisfiable and the pack gets no range at all.
            sig = SignatureResolver(resolver)
            for chk in check_call_sites(sig, self.calls):
                if chk.lookup.status == PARSE_FAILED:
                    self.unparsed.add(chk.lookup.module)
                    continue
                if not chk.hard:
                    continue
                call = chk.call
                out.append(_Problem(
                    key=("call", call.file, call.dotted, call.nargs, call.keywords),
                    dotted="%s.%s" % (chk.lookup.module, chk.lookup.qualname),
                    file=call.file, line=call.lineno,
                    status=SIGNATURE, detail=chk.detail))
        self.cache[tag] = out
        return out

    def one_missing_at(self, tag, key):
        """Whether one specific problem is present at ``tag``.

        Answered from the full per-tag result rather than by re-resolving the
        single item, because a call's verdict depends on its siblings.
        """
        return any(p.key == key for p in self.missing_at(tag))


def derive_requires(repo, pack_dir, pack_name=None, signatures=True):
    scan = scan_pack(pack_dir, pack_name)
    result = {
        "pack": scan.name,
        "path": scan.path,
        "python_files": scan.python_files,
        "vendored_comfy": scan.vendored_comfy,
        "unparseable": scan.unparseable,
        "star_imports": sorted({r.module for r in scan.references if r.kind == STAR}),
        "soft_references": sorted({r.dotted for r in scan.references if r.soft}),
        "references": 0,
        "call_sites": 0,
        "floor_tag": None,
        "ceiling_tag": None,
        "line": None,
        "determined_by": [],
        "broken_at_head": [],
        "unbindable_at_head": [],
        "conflict": [],
        "unparsed_modules": [],
        "probes": 0,
        "note": None,
    }
    if scan.vendored_comfy:
        result["note"] = "pack vendors its own comfy/ package; no ComfyUI floor applies"
        return result

    refs = hard_references(scan)
    calls = hard_calls(scan) if signatures else []
    result["references"] = len(refs)
    result["call_sites"] = len(calls)
    if not refs and not calls:
        result["note"] = (
            "no comfy.* references found; this pack does not need a requires-comfyui floor"
        )
        return result

    tags = sort_tags(repo.all_tags())
    if not tags:
        raise BadInputError(
            "The ComfyUI clone has no release tags. Run without --offline once so "
            "`git fetch --tags` can populate them."
        )

    prober = _Prober(repo, refs, calls)
    newest_idx = len(tags) - 1
    head_missing = prober.missing_at(tags[newest_idx])
    result["broken_at_head"] = [_row(p) for p in head_missing if not p.is_call]
    result["unbindable_at_head"] = [_row(p) for p in head_missing if p.is_call]

    search_hi = newest_idx
    if head_missing:
        first_bad = _earliest_removal(prober, tags, head_missing)
        if first_bad == 0:
            result["note"] = (
                "some referenced symbols never existed in any release; cannot derive a floor"
            )
            result["probes"] = prober.probes
            return result
        result["ceiling_tag"] = tags[first_bad] if first_bad <= newest_idx else None
        search_hi = first_bad - 1

    blocking = prober.missing_at(tags[search_hi])
    if blocking:
        result["conflict"] = [_row(p) for p in blocking]
        result["note"] = (
            "no single ComfyUI release satisfies this pack; the requirements listed "
            "under conflict do not overlap"
        )
        result["probes"] = prober.probes
        result["unparsed_modules"] = sorted(prober.unparsed)
        return result

    floor_idx = _earliest_satisfying(prober, tags, search_hi)
    result["floor_tag"] = tags[floor_idx]

    if floor_idx > 0:
        result["determined_by"] = sorted(
            p.label for p in prober.missing_at(tags[floor_idx - 1])
        )

    floor_v = tag_to_version(result["floor_tag"])
    if result["ceiling_tag"]:
        result["line"] = 'requires-comfyui = ">=%s,<%s"' % (
            floor_v, tag_to_version(result["ceiling_tag"])
        )
    else:
        result["line"] = 'requires-comfyui = ">=%s"' % floor_v
    result["probes"] = prober.probes
    result["unparsed_modules"] = sorted(prober.unparsed)
    return result


def _dotted(res):
    return "%s.%s" % (res.module, res.symbol) if res.symbol else res.module


def _row(problem):
    return {
        "dotted": problem.dotted,
        "file": problem.file,
        "line": problem.line,
        "status": problem.status,
        "detail": problem.detail,
    }


# Both searches below binary-search over release tags, which assumes each symbol
# has a single contiguous lifetime: once removed it stays removed. That holds for
# every removal seen so far, and a symbol removed then re-added would give a floor
# that is too new rather than too old. The endpoints are probed explicitly by
# tests/test_derive.py::test_floor_tag_actually_satisfies_the_pack, so a violation
# shows up as a test failure and not as a silently wrong version range.

def _earliest_removal(prober, tags, missing):
    """Index of the first release in which any already-broken requirement failed."""
    first_bad = len(tags)
    for problem in missing:
        idx = _first_index_missing(prober, tags, problem.key)
        if idx is not None:
            first_bad = min(first_bad, idx)
    return first_bad


def _earliest_satisfying(prober, tags, hi):
    """Index of the oldest release, at or below ``hi``, where every ref resolves."""
    lo = 0
    while lo < hi:
        mid = (lo + hi) // 2
        if prober.missing_at(tags[mid]):
            lo = mid + 1
        else:
            hi = mid
    return lo


def _first_index_missing(prober, tags, key):
    """Smallest tag index at which ``key`` stops being satisfied, or None."""
    if not prober.one_missing_at(tags[-1], key):
        return None
    lo, hi = 0, len(tags) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if prober.one_missing_at(tags[mid], key):
            hi = mid
        else:
            lo = mid + 1
    return lo
