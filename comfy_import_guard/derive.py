"""Derive a ``requires-comfyui`` floor (and ceiling) for one custom-node pack."""

from .errors import BadInputError
from .extract import STAR, scan_pack
from .resolve import Resolver
from .version import sort_tags, tag_to_version


def hard_references(scan):
    """References that must resolve: not star, not guarded by try/except."""
    seen = {}
    for ref in scan.references:
        if ref.kind == STAR or ref.soft:
            continue
        seen.setdefault((ref.module, ref.symbol, ref.kind), ref)
    return list(seen.values())


class _Prober:
    def __init__(self, repo, refs):
        self.repo = repo
        self.refs = refs
        self.cache = {}
        self.probes = 0

    def missing_at(self, tag):
        """Dotted names that do not resolve at ``tag``."""
        if tag in self.cache:
            return self.cache[tag]
        self.probes += 1
        resolver = Resolver(self.repo, tag)
        out = []
        for ref in self.refs:
            res = resolver.resolve(ref)
            if res.breaking:
                out.append(res)
        self.cache[tag] = out
        return out

    def one_missing_at(self, tag, ref):
        resolver = Resolver(self.repo, tag)
        return resolver.resolve(ref).breaking


def derive_requires(repo, pack_dir, pack_name=None):
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
        "floor_tag": None,
        "ceiling_tag": None,
        "line": None,
        "determined_by": [],
        "broken_at_head": [],
        "probes": 0,
        "note": None,
    }
    if scan.vendored_comfy:
        result["note"] = "pack vendors its own comfy/ package; no ComfyUI floor applies"
        return result

    refs = hard_references(scan)
    result["references"] = len(refs)
    if not refs:
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

    prober = _Prober(repo, refs)
    newest_idx = len(tags) - 1
    head_missing = prober.missing_at(tags[newest_idx])
    result["broken_at_head"] = [
        {"dotted": _dotted(r), "file": r.reference.file,
         "line": r.reference.lineno, "status": r.status}
        for r in head_missing
    ]

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

    if prober.missing_at(tags[search_hi]):
        result["note"] = (
            "no single ComfyUI release satisfies every referenced symbol; the pack "
            "references symbols whose lifetimes do not overlap"
        )
        result["probes"] = prober.probes
        return result

    floor_idx = _earliest_satisfying(prober, tags, search_hi)
    result["floor_tag"] = tags[floor_idx]

    if floor_idx > 0:
        result["determined_by"] = sorted(
            _dotted(r) for r in prober.missing_at(tags[floor_idx - 1])
        )

    floor_v = tag_to_version(result["floor_tag"])
    if result["ceiling_tag"]:
        result["line"] = 'requires-comfyui = ">=%s,<%s"' % (
            floor_v, tag_to_version(result["ceiling_tag"])
        )
    else:
        result["line"] = 'requires-comfyui = ">=%s"' % floor_v
    result["probes"] = prober.probes
    return result


def _dotted(res):
    return "%s.%s" % (res.module, res.symbol) if res.symbol else res.module


# Both searches below binary-search over release tags, which assumes each symbol
# has a single contiguous lifetime: once removed it stays removed. That holds for
# every removal seen so far, and a symbol removed then re-added would give a floor
# that is too new rather than too old. The endpoints are probed explicitly by
# tests/test_derive.py::test_floor_tag_actually_satisfies_the_pack, so a violation
# shows up as a test failure and not as a silently wrong version range.

def _earliest_removal(prober, tags, missing):
    """Index of the first release in which any already-broken symbol vanished."""
    first_bad = len(tags)
    for res in missing:
        idx = _first_index_missing(prober, tags, res.reference)
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


def _first_index_missing(prober, tags, ref):
    """Smallest tag index at which ``ref`` stops resolving, or None."""
    if not prober.one_missing_at(tags[-1], ref):
        return None
    lo, hi = 0, len(tags) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if prober.one_missing_at(tags[mid], ref):
            hi = mid
        else:
            lo = mid + 1
    return lo
