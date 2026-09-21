"""ComfyUI version strings: release tags (``v0.19.3``) and requires-comfyui ranges.

The tag half orders history. The range half exists so ``crawl`` can put a
publisher's declared ``requires-comfyui`` next to the one derived from the
pack's real usage and say whether they agree.
"""

import re
from dataclasses import dataclass

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def parse_tag(tag):
    """Return (major, minor, patch) or None if ``tag`` is not a release tag.

    ComfyUI also carries non-release tags such as ``latest``; those sort nowhere
    and must never be emitted as a requires-comfyui floor.
    """
    m = TAG_RE.match(tag.strip())
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def sort_tags(tags):
    """Oldest-first list of release tags, non-release tags dropped."""
    pairs = [(parse_tag(t), t) for t in tags]
    return [t for key, t in sorted((p for p in pairs if p[0] is not None))]


def tag_to_version(tag):
    """``v0.19.3`` -> ``0.19.3``. The registry field wants no ``v``."""
    parsed = parse_tag(tag)
    if parsed is None:
        return tag
    return "%d.%d.%d" % parsed


# ------------------------------------------------------- requires-comfyui ranges

# The operators the registry specification lists for requires-comfyui:
# https://docs.comfy.org/registry/specifications
CLAUSE_RE = re.compile(r"^(<=|>=|==|!=|~=|<>|<|>)?\s*v?(\d+(?:\.\d+)*)$")

_PAD = 4


@dataclass(frozen=True)
class Bound:
    version: str        # as the publisher wrote it, minus any leading v
    key: tuple          # zero-padded, so 0.3 and 0.3.0 compare equal
    inclusive: bool


@dataclass(frozen=True)
class Range:
    """A requires-comfyui string reduced to the endpoints a board can compare."""

    lower: Bound = None
    upper: Bound = None
    excludes: tuple = ()        # != / <>, which pin holes rather than endpoints

    @property
    def bounded(self):
        return self.lower is not None or self.upper is not None

    def same_bounds_as(self, other):
        return endpoint(self.lower) == endpoint(other.lower) and \
            endpoint(self.upper) == endpoint(other.upper)


def parse_range(text):
    """Reduce a requires-comfyui string to a ``Range``, or None if it cannot be.

    ``~=`` is deliberately not modelled. PEP 440 reads ``~=1.0.0`` as
    ``<1.1.0`` while the registry's own specification page describes it as
    "not version 2.0.0", and a board that guesses which one a publisher meant
    would be inventing agreement it cannot demonstrate.
    """
    if not text or not text.strip():
        return None
    lower = upper = None
    excludes = []
    for raw in text.split(","):
        clause = raw.strip()
        if not clause:
            continue
        m = CLAUSE_RE.match(clause)
        if m is None:
            return None
        op = m.group(1) or "=="
        version = m.group(2)
        key = _pad(tuple(int(p) for p in version.split(".")))
        if op in ("!=", "<>"):
            excludes.append(version)
        elif op == "~=":
            return None
        elif op == "==":
            lower = _tighter(lower, Bound(version, key, True), higher=True)
            upper = _tighter(upper, Bound(version, key, True), higher=False)
        elif op in (">=", ">"):
            lower = _tighter(lower, Bound(version, key, op == ">="), higher=True)
        else:
            upper = _tighter(upper, Bound(version, key, op == "<="), higher=False)
    rng = Range(lower, upper, tuple(excludes))
    return rng if rng.bounded else None


def _pad(parts):
    return (parts + (0,) * _PAD)[:_PAD]


def endpoint(bound):
    """A bound reduced to what equality between two ranges turns on."""
    return None if bound is None else (bound.key, bound.inclusive)


def _tighter(current, candidate, higher):
    """Keep the narrower of two bounds; on a tie the exclusive one wins."""
    if current is None:
        return candidate
    if current.key == candidate.key:
        return current if not current.inclusive else candidate
    if higher:
        return candidate if candidate.key > current.key else current
    return candidate if candidate.key < current.key else current


def render_bound(bound, lower):
    """One endpoint as a requires-comfyui clause, or ``none`` if there isn't one."""
    if bound is None:
        return "none"
    if lower:
        return (">=" if bound.inclusive else ">") + bound.version
    return ("<=" if bound.inclusive else "<") + bound.version
