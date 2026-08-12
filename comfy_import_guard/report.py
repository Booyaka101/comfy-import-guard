"""The ``check`` engine: walk custom_nodes, resolve everything, build a report.

Returns plain dicts so the CLI and the read-only HTTP route render the same data.
"""

import os

from .errors import BadInputError
from .extract import STAR, scan_pack
from .resolve import MODULE_MISSING, Resolver

SAFE = "SAFE"
WILL_BREAK = "WILL BREAK"
SKIPPED = "SKIPPED"
WARN = "WARN"

IGNORED_DIRS = {"__pycache__", ".git", ".ipynb_checkpoints"}


def find_custom_nodes(comfy_dir):
    """Accept either a ComfyUI root or a custom_nodes directory."""
    comfy_dir = os.path.abspath(os.path.expanduser(str(comfy_dir)))
    if not os.path.isdir(comfy_dir):
        raise BadInputError(
            "No such directory: %s\n"
            "Pass --comfy-dir pointing at your ComfyUI install "
            "(the folder that contains custom_nodes)." % comfy_dir
        )
    candidate = os.path.join(comfy_dir, "custom_nodes")
    if os.path.isdir(candidate):
        return candidate
    if os.path.basename(comfy_dir.rstrip(os.sep)) == "custom_nodes":
        return comfy_dir
    raise BadInputError(
        "%s has no custom_nodes subdirectory.\n"
        "Point --comfy-dir at your ComfyUI install root, or directly at a "
        "custom_nodes folder." % comfy_dir
    )


def list_packs(custom_nodes):
    """Directories only. Loose .py files and __pycache__ are not packs."""
    out = []
    for name in sorted(os.listdir(custom_nodes)):
        path = os.path.join(custom_nodes, name)
        if not os.path.isdir(path):
            continue
        if name in IGNORED_DIRS or name.startswith("."):
            continue
        out.append((name, path))
    return out


def check(repo, comfy_dir, target="origin/master", ledger=None, packs=None):
    custom_nodes = find_custom_nodes(comfy_dir)
    entries = [(n, p) for n, p in list_packs(custom_nodes) if not packs or n in set(packs)]
    resolver = Resolver(repo, target)

    report = {
        "comfy_dir": os.path.abspath(str(comfy_dir)),
        "custom_nodes": custom_nodes,
        "target": target,
        "target_sha": repo.resolve_ref(target),
        "packs": [],
        "totals": {"packs": len(entries), "will_break": 0, "safe": 0,
                   "warn": 0, "skipped": 0, "breaking_symbols": 0},
    }

    for name, path in entries:
        report["packs"].append(_check_pack(resolver, name, path, ledger))

    for p in report["packs"]:
        key = {SAFE: "safe", WILL_BREAK: "will_break", WARN: "warn", SKIPPED: "skipped"}[
            p["verdict"]
        ]
        report["totals"][key] += 1
        report["totals"]["breaking_symbols"] += len(p["breaking"])
    return report


def _check_pack(resolver, name, path, ledger):
    scan = scan_pack(path, name)
    out = {
        "pack": name,
        "path": path,
        "python_files": scan.python_files,
        "references": len(scan.references),
        "vendored_comfy": scan.vendored_comfy,
        "unparseable": [{"file": f, "error": e} for f, e in scan.unparseable],
        "breaking": [],
        "soft": [],
        "unresolvable": [],
        "verdict": SAFE,
        "note": "",
    }
    if scan.vendored_comfy:
        out["verdict"] = SKIPPED
        out["note"] = "vendors its own comfy/ package; resolved pack-locally, not checked"
        return out
    if scan.python_files == 0:
        out["verdict"] = SAFE
        out["note"] = "no Python files"
        return out

    for ref in scan.references:
        res = resolver.resolve(ref)
        row = {
            "module": res.module,
            "symbol": res.symbol,
            "dotted": "%s.%s" % (res.module, res.symbol) if res.symbol else res.module,
            "file": ref.file,
            "line": ref.lineno,
            "kind": ref.kind,
            "status": res.status,
            "detail": res.detail,
        }
        if res.status == "UNRESOLVABLE" or (ref.kind == STAR and res.status != MODULE_MISSING):
            out["unresolvable"].append(row)
        elif res.breaking and ref.soft:
            row["soft"] = True
            out["soft"].append(row)
        elif res.breaking:
            if ledger is not None:
                hit = ledger.lookup(res.module, res.symbol)
                if hit:
                    row["attribution"] = {
                        "removed_in_commit": hit.get("removed_in_commit"),
                        "pr": hit.get("pr"),
                        "removed_on": hit.get("removed_on"),
                        "last_good_tag": hit.get("last_good_tag"),
                        "first_bad_tag": hit.get("first_bad_tag"),
                        "source": "ledger",
                    }
            out["breaking"].append(row)

    if out["breaking"]:
        out["verdict"] = WILL_BREAK
    elif out["unresolvable"] or out["soft"] or out["unparseable"]:
        out["verdict"] = WARN
        bits = []
        if out["unresolvable"]:
            bits.append("%d unresolvable reference(s)" % len(out["unresolvable"]))
        if out["soft"]:
            bits.append("%d guarded import(s) that would fail" % len(out["soft"]))
        if out["unparseable"]:
            bits.append("%d file(s) this interpreter could not parse" % len(out["unparseable"]))
        out["note"] = "; ".join(bits)
    return out
