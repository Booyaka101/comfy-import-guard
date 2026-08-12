"""Attribute a missing ``comfy.*`` symbol to the commit and PR that removed it."""

import re

from .errors import BadInputError
from .resolve import Resolver, exported_names
from .version import sort_tags

PR_RE = re.compile(r"\(#(\d+)\)\s*$")


def split_dotted(repo, dotted, ref="origin/master"):
    """Split ``comfy.a.b.Symbol`` into (module, symbol) using the real tree.

    Tries the longest module prefix that exists at ``ref``; falls back to the
    longest prefix that has ever existed, because the whole point of ``blame``
    is that things get deleted.
    """
    if not dotted.startswith("comfy."):
        raise BadInputError(
            "Expected a dotted path into ComfyUI internals, e.g. "
            "comfy.ldm.minimax.model.time_shift_slope (got %r)." % dotted
        )
    parts = dotted.split(".")
    if len(parts) < 2:
        raise BadInputError("%r has no symbol part." % dotted)

    resolver = Resolver(repo, ref)
    for i in range(len(parts) - 1, 0, -1):
        # A bare directory is not blameable; require a real .py to attribute to.
        if resolver.module_source(".".join(parts[:i]))[0] is not None:
            return ".".join(parts[:i]), parts[i], resolver

    for i in range(len(parts) - 1, 0, -1):
        module = ".".join(parts[:i])
        for path in resolver.module_paths(module):
            proc = repo._git(["log", "--all", "-1", "--format=%H", "--", path], check=False)
            if proc.stdout.strip():
                return module, parts[i], resolver

    raise BadInputError(
        "No module in %r has ever existed in ComfyUI. Check the spelling; the "
        "path must be a real module such as comfy.ldm.lightricks.model." % dotted
    )


def module_path_at(repo, module, ref):
    """Repo path of ``module`` at ``ref``, preferring whichever form exists."""
    base = module.replace(".", "/")
    for path in (base + ".py", base + "/__init__.py"):
        if repo.read_file(ref, path) is not None:
            return path
    for path in (base + ".py", base + "/__init__.py"):
        proc = repo._git(["log", "--all", "-1", "--format=%H", "--", path], check=False)
        if proc.stdout.strip():
            return path
    return base + ".py"


def _defined_at(repo, ref, path, symbol):
    src = repo.read_file(ref, path)
    if src is None:
        return False
    return symbol in exported_names(src)


def ledger_hit(ledger, dotted):
    """Find a ledger row for any module/symbol split of ``dotted``. No git."""
    if ledger is None:
        return None
    parts = dotted.split(".")
    for i in range(len(parts) - 1, 0, -1):
        hit = ledger.lookup(".".join(parts[:i]), parts[i])
        if hit and hit.get("removed_in_commit"):
            return hit
    return None


def blame_symbol(repo, dotted, ledger=None, use_ledger=True, head="origin/master"):
    """Return a report dict describing when and by whom a symbol vanished.

    The ledger is consulted first and answers without touching git at all, so
    ``blame`` still works on a machine with no clone and no network.
    """
    if use_ledger:
        cached = ledger_hit(ledger, dotted)
        if cached:
            out = dict(cached)
            out.update({
                "dotted": "%s.%s" % (cached["module"], cached["symbol"]),
                "source": "ledger",
                "present_at_head": False,
            })
            return out

    module, symbol, _ = split_dotted(repo, dotted, head)
    path = module_path_at(repo, module, head)
    present_now = _defined_at(repo, head, path, symbol)
    commits = repo.pickaxe(symbol, path)

    removal = None
    introduction = None
    for c in commits:  # newest first
        try:
            in_commit = _defined_at(repo, c["sha"], path, symbol)
            in_parent = _defined_at(repo, c["sha"] + "^", path, symbol)
        except Exception:
            continue
        if in_parent and not in_commit and removal is None:
            removal = c
        if in_commit and not in_parent:
            introduction = c  # keeps walking; ends on the oldest introduction

    report = {
        "dotted": "%s.%s" % (module, symbol),
        "module": module,
        "symbol": symbol,
        "module_path": path,
        "source": "git",
        "present_at_head": present_now,
        "head": head,
        "candidates": len(commits),
        "introduced_in_commit": introduction["sha"] if introduction else None,
        "introduced_on": introduction["date"] if introduction else None,
        "introduced_subject": introduction["subject"] if introduction else None,
        "removed_in_commit": None,
        "pr": None,
        "subject": None,
        "removed_on": None,
        "last_good_tag": None,
        "first_bad_tag": None,
        "packs": [],
    }

    if removal is None:
        report["note"] = (
            "still defined at %s" % head if present_now
            else "never defined at module scope in %s" % path
        )
        return report

    report["removed_in_commit"] = removal["sha"]
    report["subject"] = removal["subject"]
    report["removed_on"] = removal["date"]
    m = PR_RE.search(removal["subject"])
    if m:
        report["pr"] = int(m.group(1))

    tags = sort_tags(repo.all_tags())
    containing = set(repo.tags_containing(removal["sha"]))
    bad = [t for t in tags if t in containing]
    report["first_bad_tag"] = bad[0] if bad else None

    if bad:
        idx = tags.index(bad[0])
        for t in reversed(tags[:idx]):
            if _defined_at(repo, t, path, symbol):
                report["last_good_tag"] = t
                break
    else:
        for t in reversed(tags):
            if _defined_at(repo, t, path, symbol):
                report["last_good_tag"] = t
                break
        report["note"] = "removal is not in any release tag yet"

    return report


def record(ledger, report, packs=()):
    """Persist a git-derived blame result into the ledger."""
    if not report.get("removed_in_commit"):
        return None
    return ledger.upsert({
        "symbol": report["symbol"],
        "module": report["module"],
        "removed_in_commit": report["removed_in_commit"],
        "pr": report.get("pr"),
        "removed_on": report.get("removed_on"),
        "subject": report.get("subject"),
        "last_good_tag": report.get("last_good_tag"),
        "first_bad_tag": report.get("first_bad_tag"),
        "packs": list(packs) + list(report.get("packs") or []),
    })
