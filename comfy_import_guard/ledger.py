"""The removal ledger: known ``comfy.*`` symbol removals, consulted before git.

Rows are derived from the real repository by ``blame``. The two shipped seeds
are real commits, resolved with ``git log -S`` and ``git tag --contains``.
"""

import json
import os
from pathlib import Path

from .errors import BadInputError

SCHEMA = 1

_HERE = Path(__file__).resolve().parent
# Wheels get ledger.json copied inside the package; a source checkout (and a
# ComfyUI custom_nodes clone) keeps the canonical copy at the repo root.
DEFAULT_LEDGER = (
    _HERE / "ledger.json" if (_HERE / "ledger.json").exists()
    else _HERE.parent / "ledger.json"
)

FIELDS = (
    "symbol",
    "module",
    "removed_in_commit",
    "pr",
    "removed_on",
    "subject",
    "last_good_tag",
    "first_bad_tag",
    "packs",
)


class Ledger:
    def __init__(self, path=None):
        self.path = Path(path) if path else DEFAULT_LEDGER
        self.entries = []
        self.load()

    # ------------------------------------------------------------------- io

    def load(self):
        if not self.path.exists():
            self.entries = []
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise BadInputError(
                "Ledger at %s is not readable JSON: %s\n"
                "Delete it or pass --ledger with a good file." % (self.path, exc)
            )
        if isinstance(raw, list):
            entries = raw
        elif isinstance(raw, dict):
            entries = raw.get("entries", [])
        else:
            raise BadInputError("Ledger at %s must be a list or an object." % self.path)
        self.entries = [_normalise(e) for e in entries if isinstance(e, dict)]
        return self

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": SCHEMA, "entries": self.sorted_entries()}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        return self

    # --------------------------------------------------------------- queries

    def sorted_entries(self):
        return sorted(self.entries, key=lambda e: (e.get("module") or "", e.get("symbol") or ""))

    def lookup(self, module, symbol):
        for e in self.entries:
            if e.get("module") == module and e.get("symbol") == symbol:
                return e
        return None

    def lookup_any(self, symbol):
        return [e for e in self.entries if e.get("symbol") == symbol]

    def upsert(self, entry):
        """Insert or merge a row. Packs are unioned, never dropped."""
        entry = _normalise(entry)
        existing = self.lookup(entry["module"], entry["symbol"])
        if existing is None:
            self.entries.append(entry)
            return entry
        packs = sorted(set(existing.get("packs", [])) | set(entry.get("packs", [])))
        for k in FIELDS:
            if k == "packs":
                continue
            if entry.get(k) not in (None, "", []):
                existing[k] = entry[k]
        existing["packs"] = packs
        return existing

    def add_pack(self, module, symbol, pack):
        e = self.lookup(module, symbol)
        if e is None:
            return None
        if pack and pack not in e.setdefault("packs", []):
            e["packs"].append(pack)
            e["packs"].sort()
        return e


def _normalise(entry):
    out = {k: entry.get(k) for k in FIELDS}
    out["packs"] = sorted(set(entry.get("packs") or []))
    if out["pr"] is not None:
        try:
            out["pr"] = int(out["pr"])
        except (TypeError, ValueError):
            out["pr"] = None
    return out
