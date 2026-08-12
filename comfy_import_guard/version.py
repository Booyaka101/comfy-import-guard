"""Semver-ish ordering for ComfyUI release tags (``v0.19.3``)."""

import re

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
