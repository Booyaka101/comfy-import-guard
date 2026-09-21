"""Command line entry point: check / blame / derive-requires / crawl."""

import argparse
import json
import os
import sys

from . import __version__
from .blame import (
    blame_signature,
    blame_symbol,
    ledger_hit,
    record,
    split_signature_target,
)
from .crawl import BOARD_JSON, BOARD_MD, DEFAULT_LIMIT, DEFAULT_MAX_ZIP_MB, crawl
from .derive import derive_requires
from .errors import BadInputError, GuardError
from .ledger import Ledger
from .report import SAFE, SKIPPED, WARN, WILL_BREAK, check
from .repo import Repo

DEFAULT_TARGET = "origin/master"


def _add_globals(parser, suppress):
    """Global flags are accepted both before and after the subcommand."""
    kw = {"default": argparse.SUPPRESS} if suppress else {}
    parser.add_argument("--cache-dir", help="where the ComfyUI clone lives", **kw)
    parser.add_argument("--ledger", help="path to ledger.json", **kw)
    parser.add_argument("--offline", action="store_true",
                        help="never touch the network; use the existing clone and the ledger",
                        **({"default": argparse.SUPPRESS} if suppress else {"default": False}))
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text",
                        **({"default": argparse.SUPPRESS} if suppress else {"default": False}))
    parser.add_argument("--quiet", "-q", action="store_true", help="suppress progress notes",
                        **({"default": argparse.SUPPRESS} if suppress else {"default": False}))


def build_parser():
    p = argparse.ArgumentParser(
        prog="comfy-import-guard",
        description=(
            "Predict and attribute ComfyUI custom-node import breakage caused by "
            "removals in comfy.* internals."
        ),
    )
    p.add_argument("--version", action="version", version="comfy-import-guard %s" % __version__)
    _add_globals(p, suppress=False)

    common = argparse.ArgumentParser(add_help=False)
    _add_globals(common, suppress=True)

    sub = p.add_subparsers(dest="command", metavar="{check,blame,derive-requires,crawl}",
                           parser_class=lambda **kw: argparse.ArgumentParser(
                               parents=[common], **kw))

    c = sub.add_parser("check", help="check every pack under a ComfyUI install")
    c.add_argument("--comfy-dir", required=True, help="ComfyUI install root (holds custom_nodes)")
    c.add_argument("--target", default=DEFAULT_TARGET,
                   help="ComfyUI ref to resolve against (default: %s)" % DEFAULT_TARGET)
    c.add_argument("--pack", action="append", dest="packs",
                   help="only check this pack (repeatable)")
    c.add_argument("--no-update", action="store_true", help="skip git fetch")
    c.add_argument("--no-signatures", action="store_true",
                   help="skip call-site and monkeypatch signature checks")

    b = sub.add_parser("blame", help="name the commit and PR that removed a symbol")
    b.add_argument("symbol", help="e.g. comfy.ldm.minimax.model.time_shift_slope")
    b.add_argument("--param", help="blame a parameter of that symbol instead of the symbol itself")
    b.add_argument("--no-ledger", action="store_true", help="ignore the ledger, always use git")
    b.add_argument("--record", action="store_true", help="write the result back to the ledger")
    b.add_argument("--head", default=DEFAULT_TARGET, help="ref treated as current")

    d = sub.add_parser("derive-requires", help="emit a requires-comfyui line for a pack")
    d.add_argument("pack_dir", help="path to one custom-node pack")
    d.add_argument("--no-update", action="store_true", help="skip git fetch")
    d.add_argument("--no-signatures", action="store_true",
                   help="derive from symbol presence only, ignoring call signatures")

    w = sub.add_parser("crawl", help="build a compatibility board from the Comfy Registry")
    w.add_argument("--out", required=True,
                   help="directory to write %s and %s into" % (BOARD_JSON, BOARD_MD))
    w.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help="how many packs to take (default: %d; 0 means the whole registry)"
                        % DEFAULT_LIMIT)
    w.add_argument("--target", default=DEFAULT_TARGET,
                   help="ComfyUI ref to resolve against (default: %s)" % DEFAULT_TARGET)
    w.add_argument("--min-downloads", type=int, default=0,
                   help="skip packs the registry reports fewer downloads for")
    w.add_argument("--max-zip-mb", type=float, default=DEFAULT_MAX_ZIP_MB,
                   help="refuse pack archives larger than this (default: %d)"
                        % DEFAULT_MAX_ZIP_MB)
    w.add_argument("--fresh", action="store_true",
                   help="ignore any checkpoint in --out and start the crawl over")
    w.add_argument("--no-update", action="store_true", help="skip git fetch")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    try:
        return _dispatch(args)
    except GuardError as exc:
        print("comfy-import-guard: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("comfy-import-guard: interrupted", file=sys.stderr)
        return 130


def _dispatch(args):
    repo = Repo(cache_dir=args.cache_dir, offline=args.offline, quiet=args.quiet)
    ledger = Ledger(args.ledger)

    if args.command == "check":
        repo.ensure(deep=False)
        if not args.no_update and not args.offline:
            repo.update()
        if repo.is_dirty() and not args.quiet:
            print("comfy-import-guard: warning: clone at %s has local modifications; "
                  "results reflect the working tree, not the ref." % repo.path, file=sys.stderr)
        rep = check(repo, args.comfy_dir, args.target, ledger, args.packs,
                    signatures=not args.no_signatures)
        if args.json:
            print(json.dumps(rep, indent=2))
        else:
            _print_check(rep)
        return 1 if rep["totals"]["will_break"] else 0

    if args.command == "blame":
        if args.param:
            repo.ensure(deep=True)
            module, qualname = split_signature_target(repo, args.symbol, args.head)
            rep = blame_signature(repo, module, qualname, args.param, head=args.head)
            if args.json:
                print(json.dumps(rep, indent=2))
            else:
                _print_param_blame(rep)
            return 0 if rep.get("changed_in_commit") else 1

        use_ledger = not args.no_ledger
        if not (use_ledger and ledger_hit(ledger, args.symbol)):
            repo.ensure(deep=True)   # only clone when the ledger cannot answer
        rep = blame_symbol(repo, args.symbol, ledger,
                           use_ledger=not args.no_ledger, head=args.head)
        if args.record and rep.get("source") == "git":
            record(ledger, rep)
            ledger.save()
        if args.json:
            print(json.dumps(rep, indent=2))
        else:
            _print_blame(rep)
        return 0 if rep.get("removed_in_commit") or rep.get("present_at_head") else 1

    if args.command == "derive-requires":
        repo.ensure(deep=True)
        if not args.no_update and not args.offline:
            repo.update()
        rep = derive_requires(repo, args.pack_dir, signatures=not args.no_signatures)
        if args.json:
            print(json.dumps(rep, indent=2))
        else:
            _print_derive(rep)
        return 0 if rep.get("line") else 1

    if args.command == "crawl":
        if args.offline:
            raise BadInputError(
                "crawl reads the Comfy Registry over the network, so --offline cannot "
                "apply to it. Use `check` or `derive-requires` offline instead."
            )
        repo.ensure(deep=True)
        if not args.no_update:
            repo.update()
        board = crawl(repo, args.out, limit=args.limit, target=args.target,
                      max_zip_bytes=int(args.max_zip_mb * 1000 * 1000),
                      min_downloads=args.min_downloads, ledger=ledger,
                      fresh=args.fresh,
                      log=None if args.quiet else _stderr)
        if args.json:
            print(json.dumps(board, indent=2))
        else:
            _print_crawl(board, args.out)
        return 1 if board["totals"]["breaksAtRef"] else 0

    raise AssertionError("unreachable")


def _stderr(msg):
    print(msg, file=sys.stderr)


# ------------------------------------------------------------------ rendering

_MARK = {SAFE: "ok", WILL_BREAK: "!!", WARN: "??", SKIPPED: "--"}


def _print_check(rep):
    print("comfy-import-guard check")
    print("  install : %s" % rep["custom_nodes"])
    print("  target  : %s (%s)" % (rep["target"], rep["target_sha"][:9]))
    print()
    if not rep["packs"]:
        print("  no custom-node packs found (directories only; loose .py files are ignored)")
        print()
    for p in rep["packs"]:
        print("[%s] %s  %s" % (_MARK[p["verdict"]], p["pack"], p["verdict"]))
        meta = "     %d python file(s), %d comfy.* reference(s)" % (
            p["python_files"], p["references"])
        print(meta)
        for row in p["breaking"]:
            if row.get("status") == "SIGNATURE":
                print("       BADCALL  %s" % row["dotted"])
                print("                %s:%s  (%s)" % (row["file"], row["line"], row["kind"]))
                print("                %s" % row["detail"])
                print("                upstream accepts %s" % row["upstream_params"])
                _print_signature_attribution(row.get("attribution"))
                continue
            print("       MISSING  %s" % row["dotted"])
            print("                %s:%s  (%s)" % (row["file"], row["line"], row["kind"]))
            att = row.get("attribution")
            if att:
                print("                removed by %s%s%s" % (
                    (att.get("removed_in_commit") or "?")[:9],
                    " in PR #%s" % att["pr"] if att.get("pr") else "",
                    " on %s" % att["removed_on"][:10] if att.get("removed_on") else "",
                ))
                if att.get("last_good_tag") or att.get("first_bad_tag"):
                    print("                last good %s, first bad %s" % (
                        att.get("last_good_tag") or "?", att.get("first_bad_tag") or "?"))
            elif row["detail"]:
                print("                %s" % row["detail"])
        for row in p["signature_drift"]:
            print("       SIGDRIFT %s  %s:%s" % (row["dotted"], row["file"], row["line"]))
            print("                %s" % row["detail"])
            _print_signature_attribution(row.get("attribution"))
        for row in p["soft"]:
            if row.get("status") == "SIGNATURE_SHIM":
                print("       SHIM     %s  %s:%s" % (
                    row["dotted"], row["file"], row["line"]))
                print("                %s" % row["detail"])
                continue
            print("       SOFT     %s  %s:%s (guarded by try/except)" % (
                row["dotted"], row["file"], row["line"]))
        for row in p["unresolvable"]:
            print("       UNKNOWN  %s  %s:%s  %s" % (
                row["dotted"], row["file"], row["line"], row["detail"]))
        for row in p["unparseable"]:
            print("       UNPARSED %s  %s" % (row["file"], row["error"]))
        if p["note"]:
            print("     note: %s" % p["note"])
        print()

    t = rep["totals"]
    line = "%d pack(s): %d will break, %d safe, %d warn, %d skipped; %d breaking reference(s)" % (
        t["packs"], t["will_break"], t["safe"], t["warn"], t["skipped"], t["breaking_symbols"])
    if t.get("signature_drift"):
        line += ", %d signature drift(s)" % t["signature_drift"]
    print(line)
    if t["will_break"]:
        print("Run `comfy-import-guard blame <module.Symbol>` for the commit that removed it.")


def _print_signature_attribution(att):
    if not att:
        return
    print("                parameter '%s' %s by %s%s%s" % (
        att["param"], att.get("direction") or "changed",
        (att.get("changed_in_commit") or "?")[:9],
        " in PR #%s" % att["pr"] if att.get("pr") else "",
        " on %s" % att["changed_on"][:10] if att.get("changed_on") else "",
    ))
    if att.get("last_good_tag") or att.get("first_bad_tag"):
        print("                last good %s, first bad %s" % (
            att.get("last_good_tag") or "?", att.get("first_bad_tag") or "?"))


def _print_blame(rep):
    print("%s" % rep["dotted"])
    if rep.get("source") == "ledger":
        print("  (from ledger; pass --no-ledger to re-derive from git)")
    if rep.get("present_at_head") and not rep.get("removed_in_commit"):
        print("  status        : still defined at %s" % rep.get("head", "head"))
        if rep.get("introduced_in_commit"):
            print("  introduced    : %s  %s" % (
                rep["introduced_in_commit"][:9], rep.get("introduced_subject") or ""))
        return
    if not rep.get("removed_in_commit"):
        print("  status        : no removal found")
        print("  note          : %s" % rep.get("note", "symbol never defined at module scope"))
        return
    print("  removed in    : %s" % rep["removed_in_commit"][:9])
    if rep.get("subject"):
        print("  commit        : %s" % rep["subject"])
    if rep.get("pr"):
        print("  pull request  : Comfy-Org/ComfyUI#%s" % rep["pr"])
        print("                  https://github.com/comfyanonymous/ComfyUI/pull/%s" % rep["pr"])
    if rep.get("removed_on"):
        print("  removed on    : %s" % rep["removed_on"])
    print("  last good tag : %s" % (rep.get("last_good_tag") or "none"))
    print("  first bad tag : %s" % (rep.get("first_bad_tag") or "not released yet"))
    if rep.get("introduced_in_commit"):
        print("  introduced in : %s  (%s)" % (
            rep["introduced_in_commit"][:9], (rep.get("introduced_on") or "")[:10]))
    if rep.get("packs"):
        print("  known packs   : %s" % ", ".join(rep["packs"]))
    if rep.get("note"):
        print("  note          : %s" % rep["note"])


def _print_param_blame(rep):
    print("%s.%s  parameter '%s'" % (rep["module"], rep["qualname"], rep["param"]))
    if not rep.get("changed_in_commit"):
        print("  status        : no change found in %s" % rep["module_path"])
        print("  note          : the parameter may predate the file, or the symbol "
              "may not be a plain def")
        return
    print("  %-14s: %s" % (rep["direction"], rep["changed_in_commit"][:9]))
    if rep.get("subject"):
        print("  commit        : %s" % rep["subject"])
    if rep.get("pr"):
        print("  pull request  : Comfy-Org/ComfyUI#%s" % rep["pr"])
        print("                  https://github.com/comfyanonymous/ComfyUI/pull/%s" % rep["pr"])
    if rep.get("changed_on"):
        print("  changed on    : %s" % rep["changed_on"])
    print("  last good tag : %s" % (rep.get("last_good_tag") or "none"))
    print("  first bad tag : %s" % (rep.get("first_bad_tag") or "not released yet"))


def _print_derive(rep):
    print("derive-requires: %s" % rep["pack"])
    counts = "  %d python file(s), %d hard comfy.* reference(s)" % (
        rep["python_files"], rep["references"])
    if rep.get("call_sites"):
        counts += ", %d call site(s)" % rep["call_sites"]
    print(counts)
    if rep["star_imports"]:
        print("  star imports (not resolvable): %s" % ", ".join(rep["star_imports"]))
    if rep["soft_references"]:
        print("  %d guarded reference(s) ignored for the floor" % len(rep["soft_references"]))
    for f, e in rep["unparseable"]:
        print("  unparsed: %s  %s" % (f, e))
    if rep["broken_at_head"]:
        print("  already removed at head:")
        for row in rep["broken_at_head"]:
            print("    %s  (%s:%s)" % (row["dotted"], row["file"], row["line"]))
    if rep.get("unbindable_at_head"):
        print("  calls that no longer bind at head:")
        for row in rep["unbindable_at_head"]:
            print("    %s  (%s:%s)" % (row["dotted"], row["file"], row["line"]))
            print("      %s" % row["detail"])
    for module in rep.get("unparsed_modules") or []:
        print("  unparsed at some probed tag: %s" % module)
    if not rep["line"]:
        print("  no line emitted: %s" % (rep.get("note") or "unknown"))
        for row in rep.get("conflict") or []:
            print("    conflict: %s  (%s:%s)%s" % (
                row["dotted"], row["file"], row["line"],
                "  %s" % row["detail"] if row["detail"] else ""))
        if any(r["status"] == "SIGNATURE" for r in rep.get("conflict") or []):
            print("  re-run with --no-signatures to derive from imports alone")
        return
    if rep["determined_by"]:
        print("  floor set by  : %s" % ", ".join(rep["determined_by"]))
    print("  probed %d release tag(s)" % rep["probes"])
    print()
    print("Paste under [tool.comfy] in the pack's pyproject.toml:")
    print()
    print("  %s" % rep["line"])


def _print_crawl(board, out_dir):
    t = board["totals"]
    print("crawl: %d pack(s) from %s, ComfyUI @ %s (%s)" % (
        t["packs"], _netloc(board["registrySource"]),
        board["comfyRef"], board["comfySha"][:9]))
    print("  analysed %d, skipped %d" % (t["analysed"], t["skipped"]))
    print("  breaks at %s: %d" % (board["comfyRef"], t["breaksAtRef"]))
    print("  registry declares a range: %d   derived by us: %d"
          % (t["registryDeclared"], t["derivedHere"]))
    if t["disagree"] or t["notComparable"]:
        print("  of those, %d agree, %d disagree, %d not comparable"
              % (t["agree"], t["disagree"], t["notComparable"]))
    print("wrote %s, %s" % (os.path.join(out_dir, BOARD_JSON),
                            os.path.join(out_dir, BOARD_MD)))


def _netloc(url):
    return url.split("//", 1)[-1].split("/", 1)[0] or url


if __name__ == "__main__":
    sys.exit(main())
