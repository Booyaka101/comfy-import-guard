"""The ``check`` engine: walk custom_nodes, resolve everything, build a report.

Returns plain dicts so the CLI and the read-only HTTP route render the same data.
"""

import os

from .blame import blame_signature
from .errors import BadInputError
from .extract import STAR, scan_pack
from .resolve import MODULE_MISSING, Resolver
from .signature import (
    FOUND,
    PARSE_FAILED,
    SignatureResolver,
    check_call_sites,
    replacement_drops,
)

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


def check(repo, comfy_dir, target="origin/master", ledger=None, packs=None, signatures=True):
    custom_nodes = find_custom_nodes(comfy_dir)
    entries = [(n, p) for n, p in list_packs(custom_nodes) if not packs or n in set(packs)]
    resolver = Resolver(repo, target)
    sig = SignatureResolver(resolver) if signatures else None
    blame_cache = {}

    report = {
        "comfy_dir": os.path.abspath(str(comfy_dir)),
        "custom_nodes": custom_nodes,
        "target": target,
        "target_sha": repo.resolve_ref(target),
        "packs": [],
        "totals": {"packs": len(entries), "will_break": 0, "safe": 0,
                   "warn": 0, "skipped": 0, "breaking_symbols": 0,
                   "signature_drift": 0},
    }

    for name, path in entries:
        report["packs"].append(_check_pack(resolver, name, path, ledger, sig, blame_cache))

    for p in report["packs"]:
        key = {SAFE: "safe", WILL_BREAK: "will_break", WARN: "warn", SKIPPED: "skipped"}[
            p["verdict"]
        ]
        report["totals"][key] += 1
        report["totals"]["breaking_symbols"] += len(p["breaking"])
        report["totals"]["signature_drift"] += len(p["signature_drift"])
    return report


def _check_pack(resolver, name, path, ledger, sig=None, blame_cache=None):
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
        "signature_drift": [],
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

    if sig is not None:
        _check_signatures(sig, scan, out, blame_cache if blame_cache is not None else {})

    if out["breaking"]:
        out["verdict"] = WILL_BREAK
    elif out["unresolvable"] or out["soft"] or out["unparseable"] or out["signature_drift"]:
        out["verdict"] = WARN
        bits = []
        if out["unresolvable"]:
            bits.append("%d unresolvable reference(s)" % len(out["unresolvable"]))
        shims = [r for r in out["soft"] if r.get("status") == "SIGNATURE_SHIM"]
        if len(out["soft"]) > len(shims):
            bits.append("%d guarded reference(s) that would fail"
                        % (len(out["soft"]) - len(shims)))
        if shims:
            bits.append("%d version-shim call(s) that do not bind here" % len(shims))
        if out["signature_drift"]:
            bits.append("%d monkeypatch(es) behind the upstream signature" % len(out["signature_drift"]))
        if out["unparseable"]:
            bits.append("%d file(s) this interpreter could not parse" % len(out["unparseable"]))
        out["note"] = "; ".join(bits)
    return out


def _check_signatures(sig, scan, out, blame_cache):
    """Bind every comfy.* call site and monkeypatch against the target ref."""
    parse_failed = set()

    def target_unparsed(lk, file, line):
        if lk.module in parse_failed:
            return
        parse_failed.add(lk.module)
        out["unresolvable"].append({
            "module": lk.module, "symbol": lk.qualname,
            "dotted": "%s.%s" % (lk.module, lk.qualname),
            "file": file, "line": line, "kind": "call",
            "status": "TARGET_UNPARSED", "detail": lk.detail,
        })

    for chk in check_call_sites(sig, scan.call_sites):
        call, lk = chk.call, chk.lookup
        if lk.status == PARSE_FAILED:
            target_unparsed(lk, call.file, call.lineno)
            continue
        if chk.binds:
            continue
        row = {
            "module": lk.module,
            "symbol": lk.qualname,
            "dotted": "%s.%s" % (lk.module, lk.qualname),
            "file": call.file,
            "line": call.lineno,
            "kind": "call",
            "status": "SIGNATURE",
            "detail": chk.detail,
            "upstream_params": lk.spec.render(),
        }
        if chk.shim:
            row["status"] = "SIGNATURE_SHIM"
            row["detail"] += "; another call to it in this file binds, so this "\
                             "looks like a version shim"
            out["soft"].append(row)
            continue
        if call.soft:
            row["soft"] = True
            out["soft"].append(row)
            continue
        att = _signature_attribution(sig, lk, chk.first_param, blame_cache)
        if att:
            row["attribution"] = att
        out["breaking"].append(row)

    for patch in scan.monkeypatches:
        lk = sig.lookup(patch.dotted)
        if lk.status == PARSE_FAILED:
            target_unparsed(lk, patch.file, patch.lineno)
            continue
        if lk.status != FOUND:
            continue
        dropped = replacement_drops(lk.spec, patch.replacement)
        if not dropped:
            continue
        row = {
            "module": lk.module,
            "symbol": lk.qualname,
            "dotted": "%s.%s" % (lk.module, lk.qualname),
            "file": patch.file,
            "line": patch.lineno,
            "kind": "monkeypatch",
            "status": "SIGNATURE_DRIFT",
            "missing": dropped,
            "detail": "replacement drops %s present upstream" % (
                ", ".join("'%s'" % d for d in dropped)),
            "upstream_params": lk.spec.render(),
            "soft": patch.soft,
        }
        att = _signature_attribution(sig, lk, dropped[0], blame_cache)
        if att:
            row["attribution"] = att
        out["signature_drift"].append(row)


def _signature_attribution(sig, lk, param, blame_cache):
    """Name the commit that moved the signature, when git can still say."""
    if not param:
        return None
    key = (lk.module, lk.qualname, param)
    if key in blame_cache:
        return blame_cache[key]
    att = None
    try:
        rep = blame_signature(sig.resolver.repo, lk.module, lk.qualname, param,
                              head=sig.ref)
        if rep.get("changed_in_commit"):
            att = {
                "param": param,
                "direction": rep["direction"],
                "changed_in_commit": rep["changed_in_commit"],
                "pr": rep.get("pr"),
                "changed_on": rep.get("changed_on"),
                "last_good_tag": rep.get("last_good_tag"),
                "first_bad_tag": rep.get("first_bad_tag"),
                "source": "git",
            }
    except Exception:
        att = None   # offline shallow clone, or history the pickaxe cannot see
    blame_cache[key] = att
    return att
