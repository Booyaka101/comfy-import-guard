"""Run the whole registry through the pipeline one pack already goes through.

``derive-requires`` answers one pack on disk. ``crawl`` answers the registry:
it walks the public node listing, pulls each pack's latest ``node.zip`` from
the CDN into the same cache directory the ComfyUI clone lives in, then checks
and derives against one pinned ComfyUI ref.

None of the analysis is new. The archive enters through the same file-walk seam
a directory does (``extract.ZipSource``), so a crawled pack and an installed one
are scanned, resolved, signature-checked and derived identically.

Two things make a long run survivable. Downloads are cached by publisher, pack
and version, so a second crawl re-reads archives instead of re-fetching them.
And a checkpoint in the output directory holds the pinned ref, the pack
selection and every finished record, so a run that is interrupted, rate-limited
or simply stopped resumes where it left off rather than starting over.
"""

import collections
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .derive import derive_requires
from .errors import BadInputError, RegistryError
from .extract import ZipSource
from .registry import MAX_PAGE_LIMIT, NODES_PATH, RegistryClient
from .report import WILL_BREAK, check_pack
from .resolve import Resolver
from .signature import SignatureResolver
from .version import endpoint, parse_range, render_bound, tag_to_version

SCHEMA_VERSION = 1

BOARD_JSON = "board.json"
BOARD_MD = "board.md"
CHECKPOINT = "crawl-checkpoint.json"

DEFAULT_LIMIT = 100
DEFAULT_MAX_ZIP_MB = 64

# Flushing after every pack would rewrite the whole checkpoint thousands of
# times. Ten packs is the most work a hard kill can cost.
CHECKPOINT_EVERY = 10

ANALYSED = "analysed"
SKIPPED = "skipped"

AGREE = "agree"
DISAGREE = "disagree"
NOT_COMPARABLE = "not-comparable"
NOT_DERIVED = "not-derived"
REGISTRY_SILENT = "registry-silent"

ACTIVE_VERSION = "NodeVersionStatusActive"

SIGNATURE_STATUSES = ("SIGNATURE", "SIGNATURE_SHIM")

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# Publisher-supplied, so a link is only made from one that cannot break out of
# the markdown it lands in.
_LINKABLE = re.compile(r"^https?://[^\s<>()\\[\]]+$")


def crawl(repo, out_dir, client=None, limit=DEFAULT_LIMIT, target="origin/master",
          max_zip_bytes=DEFAULT_MAX_ZIP_MB * 1000 * 1000, min_downloads=0,
          ledger=None, fresh=False, log=None):
    """Build a compatibility board for the registry and write it to ``out_dir``.

    Raises only for conditions that make the whole run pointless: an
    unresolvable ref, an unwritable output directory, a network that stays down
    through the backoff. Anything one pack can do wrong lands in that pack's
    record as a skip reason and the crawl carries on.
    """
    if limit < 0:
        raise BadInputError("--limit cannot be negative. Use 0 for the whole registry.")
    if max_zip_bytes <= 0:
        raise BadInputError("--max-zip-mb must be greater than zero.")
    if min_downloads < 0:
        raise BadInputError("--min-downloads cannot be negative.")

    log = log or (lambda msg: None)
    client = client or RegistryClient(log=log)
    out = _prepare_out(out_dir)
    checkpoint = out / CHECKPOINT
    sha = repo.resolve_ref(target)

    state = None if fresh else _resume(checkpoint, target, sha, limit, min_downloads, log)
    if state is None:
        log("comfy-import-guard: listing packs from %s ..." % client.api_root)
        state = _start(client, target, sha, limit, min_downloads)
        _save(checkpoint, state)

    done = {r["id"] for r in state["packs"]}
    pending = [e for e in state["selection"] if e["id"] not in done]
    total = len(state["selection"])
    if done and pending:
        log("comfy-import-guard: resuming, %d of %d pack(s) already done" % (len(done), total))

    analyser = _Analyser(repo, client, sha, Path(repo.cache_dir) / "packs",
                         max_zip_bytes, ledger)
    try:
        for i, entry in enumerate(pending, 1):
            record = analyser.run(entry)
            state["packs"].append(record)
            log("  [%d/%d] %s" % (len(done) + i, total, _progress(record)))
            if i % CHECKPOINT_EVERY == 0:
                _save(checkpoint, state)
    finally:
        _save(checkpoint, state)

    board = _board(state)
    _write_text(out / BOARD_JSON, board_json(board))
    _write_text(out / BOARD_MD, render_markdown(board))
    return board


# ------------------------------------------------------------------ selection

def _start(client, target, sha, limit, min_downloads):
    """Pin the run: which packs, which ref, which moment.

    ``generatedAt`` is pinned here rather than at write time. A crawl that is
    resumed, or simply re-run once finished, then reproduces the same board
    instead of one that differs only in its timestamp. The selection is pinned
    for the same reason: the listing is ordered by search ranking, so it shifts
    under a long run.
    """
    selection = []
    page = 1
    registry_total = None
    while True:
        payload = client.nodes_page(page, MAX_PAGE_LIMIT)
        registry_total = _count(payload.get("total"), registry_total)
        nodes = payload["nodes"]
        if not nodes:
            break
        for node in nodes:
            if not isinstance(node, dict):
                continue
            entry = _entry(node)
            if not entry["id"] or entry["downloads"] < min_downloads:
                continue
            selection.append(entry)
            if limit and len(selection) >= limit:
                break
        if limit and len(selection) >= limit:
            break
        if page >= (_count(payload.get("totalPages"), 0) or page):
            break
        page += 1

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": "comfy-import-guard %s" % __version__,
        "comfyRef": target,
        "comfySha": sha,
        "limit": limit,
        "minDownloads": min_downloads,
        "registrySource": client.api_root + NODES_PATH,
        "registryTotal": registry_total,
        "selection": selection,
        "packs": [],
    }


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _count(value, fallback):
    """A count the listing reported, or ``fallback`` if it reported nonsense."""
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _entry(node):
    """The fields a board needs, trimmed out of one listing record."""
    latest = _mapping(node.get("latest_version"))
    publisher = _mapping(node.get("publisher"))
    return {
        "id": node.get("id") or "",
        "name": node.get("name") or node.get("id") or "",
        "publisher": publisher.get("id") or publisher.get("name") or "",
        "version": latest.get("version") or "",
        "repository": node.get("repository") or "",
        "downloads": _count(node.get("downloads"), 0),
        "declared": (latest.get("supported_comfyui_version")
                     or node.get("supported_comfyui_version") or ""),
        "declaredFrom": ("version" if latest.get("supported_comfyui_version")
                         else "node" if node.get("supported_comfyui_version") else ""),
    }


def _prepare_out(out_dir):
    out = Path(out_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BadInputError("Cannot write the board to %s: %s" % (out, exc))
    return out


# ------------------------------------------------------------------- one pack

class _Analyser:
    """Everything one pack needs, built once and reused down the crawl.

    The resolver and signature caches are why this is an object rather than a
    function. They hold ComfyUI's parsed modules at the pinned ref, and
    rebuilding them per pack would re-parse the same files once per pack.
    """

    def __init__(self, repo, client, sha, pack_cache, max_zip_bytes, ledger=None):
        self.repo = repo
        self.client = client
        self.pack_cache = Path(pack_cache)
        self.max_zip_bytes = max_zip_bytes
        self.ledger = ledger
        self.resolver = Resolver(repo, sha)
        self.sig = SignatureResolver(self.resolver)
        self.blame = {}

    def run(self, entry):
        record = _blank(entry)
        if not entry["version"]:
            return _skip(record, "no published version in the registry")

        try:
            version = self.client.version_record(entry["id"], entry["version"])
        except RegistryError as exc:
            return _skip(record, str(exc))

        # The version record, not the listing's embedded copy, is authoritative.
        if version.get("supported_comfyui_version"):
            record["registry"] = {
                "supportedComfyuiVersion": version["supported_comfyui_version"],
                "declaredIn": "version",
            }
        if version.get("deprecated"):
            return _skip(record, "latest version %s is deprecated" % entry["version"])
        status = version.get("status") or ""
        if status and status != ACTIVE_VERSION:
            return _skip(record, "latest version %s is %s" % (entry["version"], status))

        url = (version.get("downloadUrl") or "").strip()
        if not url:
            return _skip(record, "version %s has no downloadUrl" % entry["version"])

        archive = _cache_path(self.pack_cache, entry)
        if not archive.exists():
            try:
                self.client.download(url, archive, self.max_zip_bytes)
            except RegistryError as exc:
                return _skip(record, str(exc))

        try:
            source = ZipSource(archive, entry["id"])
        except BadInputError as exc:
            # A cached archive that will not open is worth one refetch, so it
            # goes rather than pinning the pack to a skip on every future run.
            self.client.discard(archive)
            return _skip(record, "%s; dropped the cached copy, the next run refetches"
                                 % exc)

        with source:
            checked = check_pack(self.resolver, entry["id"], source,
                                 self.ledger, self.sig, self.blame)
            if checked["python_files"] == 0:
                return _skip(record, "archive holds no Python files")
            derived = derive_requires(self.repo, source)

        record["pythonFiles"] = checked["python_files"]
        record["unparseable"] = checked["unparseable"]
        record["verdictAtRef"] = checked["verdict"]
        record["breaksAtRef"] = checked["verdict"] == WILL_BREAK
        record["removedAtRef"] = [_finding(r) for r in checked["breaking"]
                                  if r["status"] not in SIGNATURE_STATUSES]
        record["unbindableAtRef"] = [_finding(r) for r in checked["breaking"]
                                     if r["status"] in SIGNATURE_STATUSES]
        record["hardReferences"] = derived["references"]
        record["callSites"] = derived["call_sites"]
        record["note"] = derived["note"] or checked["note"] or None
        record["derived"] = _derived(derived)
        record.update(_compare(record["registry"]["supportedComfyuiVersion"],
                               record["derived"] and record["derived"]["range"]))
        return record


def _blank(entry):
    return {
        "id": entry["id"],
        "publisher": entry["publisher"],
        "name": entry["name"],
        "version": entry["version"],
        "repository": entry["repository"],
        "downloads": entry["downloads"],
        "status": ANALYSED,
        "skipReason": None,
        "registry": {
            "supportedComfyuiVersion": entry["declared"],
            "declaredIn": entry["declaredFrom"],
        },
        "pythonFiles": 0,
        "unparseable": [],
        "hardReferences": 0,
        "callSites": 0,
        "derived": None,
        "verdictAtRef": None,
        "removedAtRef": [],
        "unbindableAtRef": [],
        "breaksAtRef": False,
        "agreement": REGISTRY_SILENT,
        "disagreement": None,
        "note": None,
    }


def _skip(record, reason):
    record["status"] = SKIPPED
    record["skipReason"] = reason
    return record


def _derived(report):
    if not report["floor_tag"]:
        return None
    text = ">=%s" % tag_to_version(report["floor_tag"])
    if report["ceiling_tag"]:
        text += ",<%s" % tag_to_version(report["ceiling_tag"])
    return {
        "range": text,
        "line": report["line"],
        "floorTag": report["floor_tag"],
        "ceilingTag": report["ceiling_tag"],
        "determinedBy": report["determined_by"],
    }


def _finding(row):
    out = {"dotted": row["dotted"], "file": row["file"], "line": row["line"],
           "status": row["status"], "detail": row.get("detail") or ""}
    if row.get("attribution"):
        out["attribution"] = row["attribution"]
    return out


def _cache_path(pack_cache, entry):
    return (Path(pack_cache) / _safe(entry["publisher"] or "unknown")
            / _safe(entry["id"]) / _safe(entry["version"]) / "node.zip")


def _safe(component):
    """Registry ids reach the filesystem, so they are sanitised, not trusted."""
    cleaned = _UNSAFE.sub("_", str(component))
    return cleaned if cleaned.strip(".") else "_"


def _progress(record):
    if record["status"] == SKIPPED:
        return "%s  skipped: %s" % (record["id"], record["skipReason"])
    return "%s %s  %s  (%d file(s), %d ref(s))%s" % (
        record["id"], record["version"],
        record["derived"]["range"] if record["derived"] else "no range derived",
        record["pythonFiles"], record["hardReferences"],
        "  BREAKS" if record["breaksAtRef"] else "")


# ----------------------------------------------------------------- comparison

def _compare(declared, derived):
    """Put the publisher's claim next to ours without choosing between them."""
    if not declared:
        return {"agreement": REGISTRY_SILENT, "disagreement": None}
    if not derived:
        return {"agreement": NOT_DERIVED, "disagreement": None}
    theirs, ours = parse_range(declared), parse_range(derived)
    if theirs is None or ours is None:
        return {"agreement": NOT_COMPARABLE,
                "disagreement": "no endpoint comparison between %s and %s"
                                % (declared, derived)}
    if theirs.same_bounds_as(ours):
        return {"agreement": AGREE, "disagreement": None}
    return {"agreement": DISAGREE, "disagreement": _explain(theirs, ours)}


def _explain(theirs, ours):
    bits = []
    for label, lower in (("floor", True), ("ceiling", False)):
        mine = ours.lower if lower else ours.upper
        yours = theirs.lower if lower else theirs.upper
        if endpoint(mine) == endpoint(yours):
            continue
        bits.append("%s: registry says %s, usage implies %s"
                    % (label, render_bound(yours, lower), render_bound(mine, lower)))
    return "; ".join(bits)


# ---------------------------------------------------------------- persistence

def _resume(path, target, sha, limit, min_downloads, log):
    """The checkpoint, if it still describes this crawl. None means start over."""
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log("comfy-import-guard: checkpoint at %s is unreadable (%s); starting fresh"
            % (path, exc))
        return None
    if not isinstance(state, dict) or state.get("schemaVersion") != SCHEMA_VERSION:
        return None
    if not isinstance(state.get("selection"), list) or not isinstance(state.get("packs"), list):
        return None
    if state.get("comfySha") != sha:
        log("comfy-import-guard: checkpoint was pinned to %s, this run is %s; starting fresh"
            % ((state.get("comfySha") or "?")[:9], sha[:9]))
        return None
    if (state.get("comfyRef"), state.get("limit"), state.get("minDownloads")) != \
            (target, limit, min_downloads):
        log("comfy-import-guard: checkpoint covers a different selection; starting fresh")
        return None
    return state


def _save(path, state):
    tmp = Path(str(path) + ".tmp")
    _write_text(tmp, json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    os.replace(str(tmp), str(path))


def _write_text(path, text):
    # newline="" keeps the bytes identical on Windows, where the default would
    # turn every "\n" into "\r\n" and the byte-for-byte re-run guarantee would
    # hold only per platform.
    with open(str(path), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


# --------------------------------------------------------------------- output

def _board(state):
    order = {e["id"]: i for i, e in enumerate(state["selection"])}
    packs = sorted(state["packs"], key=lambda p: order.get(p["id"], len(order)))
    analysed = [p for p in packs if p["status"] == ANALYSED]
    agreement = collections.Counter(p["agreement"] for p in analysed)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": state["generatedAt"],
        "tool": state["tool"],
        "comfyRef": state["comfyRef"],
        "comfySha": state["comfySha"],
        "registrySource": state["registrySource"],
        "registryTotal": state["registryTotal"],
        "totals": {
            "packs": len(packs),
            "analysed": len(analysed),
            "skipped": len(packs) - len(analysed),
            "breaksAtRef": sum(1 for p in analysed if p["breaksAtRef"]),
            "registryDeclared": sum(
                1 for p in analysed if p["registry"]["supportedComfyuiVersion"]),
            "derivedHere": sum(1 for p in analysed if p["derived"]),
            "agree": agreement[AGREE],
            "disagree": agreement[DISAGREE],
            "notComparable": agreement[NOT_COMPARABLE],
        },
        "packs": packs,
    }


def board_json(board):
    return json.dumps(board, indent=2, ensure_ascii=False) + "\n"


def board_order(packs):
    """Board reading order: what breaks first, then what is disputed."""
    return sorted(packs, key=lambda p: (
        not p["breaksAtRef"], p["agreement"] != DISAGREE, -p["hardReferences"], p["id"]))


def render_markdown(board):
    t = board["totals"]
    analysed = [p for p in board["packs"] if p["status"] == ANALYSED]
    skipped = [p for p in board["packs"] if p["status"] == SKIPPED]
    ordered = board_order(analysed)
    breaking = [p for p in ordered if p["breaksAtRef"]]
    disputed = [p for p in ordered if p["agreement"] == DISAGREE]

    out = [
        "# ComfyUI custom-node compatibility board",
        "",
        "Generated %s by %s." % (board["generatedAt"], board["tool"]),
        "ComfyUI ref `%s` at `%s`." % (board["comfyRef"], board["comfySha"]),
        "Source: %s%s." % (board["registrySource"],
                           " (%d pack(s) in the registry)" % board["registryTotal"]
                           if board["registryTotal"] else ""),
        "",
        "%d pack(s) taken: %d analysed, %d skipped. %d break at this ref."
        % (t["packs"], t["analysed"], t["skipped"], t["breaksAtRef"]),
        "%d declare a requires-comfyui range, %d have one derived here "
        "(%d agree, %d disagree, %d not comparable)."
        % (t["registryDeclared"], t["derivedHere"], t["agree"], t["disagree"],
           t["notComparable"]),
        "",
        "## Breaks at `%s`" % board["comfyRef"],
        "",
    ]
    if not breaking:
        out += ["No analysed pack in this slice references a symbol that is gone, or "
                "makes a call that cannot bind, at this ref.", ""]
    for p in breaking:
        out.append("### %s %s" % (_cell(p["id"]), _cell(p["version"])))
        out.append("")
        for row in p["removedAtRef"]:
            out.append("- gone: `%s` at `%s:%s`" % (row["dotted"], row["file"], row["line"]))
        for row in p["unbindableAtRef"]:
            out.append("- will not bind: `%s` at `%s:%s`, %s"
                       % (row["dotted"], row["file"], row["line"], _cell(row["detail"])))
        out.append("")

    out += [
        "## Packs",
        "",
        "| pack | version | refs | registry declares | derived here | agreement |",
        "| --- | --- | ---: | --- | --- | --- |",
    ]
    for p in ordered:
        out.append("| %s | %s | %d | %s | %s | %s |" % (
            _linked(p), _cell(p["version"]), p["hardReferences"],
            _code_or_dash(p["registry"]["supportedComfyuiVersion"]),
            _code_or_dash(p["derived"] and p["derived"]["range"]),
            p["agreement"] + (" **BREAKS**" if p["breaksAtRef"] else ""),
        ))
    out.append("")

    if disputed:
        out += ["## Where the registry and this tool disagree", "",
                "Neither side is corrected here. The declared range is what the publisher "
                "committed to, the derived one is what the pack's `comfy.*` usage needs.", ""]
        for p in disputed:
            out.append("- %s %s: %s" % (_cell(p["id"]), _cell(p["version"]),
                                        _cell(p["disagreement"])))
        out.append("")

    if skipped:
        out += ["## Skipped", "", "| pack | why |", "| --- | --- |"]
        for p in skipped:
            out.append("| %s | %s |" % (_cell(p["id"]), _cell(p["skipReason"])))
        out.append("")
    return "\n".join(out)


def _linked(pack):
    """The pack name, pointing at its repository when the registry gave a usable one."""
    label = _cell(pack["id"])
    url = (pack.get("repository") or "").strip()
    return "[%s](%s)" % (label, url) if _LINKABLE.match(url) else label


def _cell(text):
    """Registry-supplied text inside a markdown table cell."""
    return " ".join(str(text or "").split()).replace("|", "\\|")


def _code_or_dash(text):
    return "`%s`" % _cell(text) if text else "-"
