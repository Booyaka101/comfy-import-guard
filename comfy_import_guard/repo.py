"""Manages the local clone of ComfyUI that every answer is resolved against.

The clone starts shallow (``check`` only needs one ref) and is unshallowed
lazily the first time a command needs history (``blame``, ``derive-requires``).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .errors import GitError, NetworkError, RefError

COMFY_URL = "https://github.com/comfyanonymous/ComfyUI.git"
CACHE_ENV = "COMFY_IMPORT_GUARD_CACHE"

_NETWORK_MARKERS = (
    "could not resolve host",
    "unable to access",
    "failed to connect",
    "connection timed out",
    "connection refused",
    "network is unreachable",
    "operation timed out",
    "proxy",
    "ssl",
    "tls",
    "early eof",
    "the remote end hung up",
)


def default_cache_dir():
    """Where the clone lives when ``--cache-dir`` is not given."""
    env = os.environ.get(CACHE_ENV)
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "comfy-import-guard" / "cache"
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "comfy-import-guard"


class Repo:
    """A ComfyUI checkout, queried entirely through plumbing-ish git commands."""

    def __init__(self, cache_dir=None, url=COMFY_URL, offline=False, quiet=False):
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.path = self.cache_dir / "ComfyUI"
        self.url = url
        self.offline = offline
        self.quiet = quiet
        self._ready = False
        self._deep = False
        self._file_cache = {}
        self._tags = None

    # ---------------------------------------------------------------- plumbing

    def _git(self, args, check=True, cwd=None, timeout=900):
        if shutil.which("git") is None:
            raise GitError(
                "git is not on PATH. comfy-import-guard needs git to read ComfyUI history.\n"
                "Install git from https://git-scm.com/downloads and re-run."
            )
        try:
            proc = subprocess.run(
                ["git"] + list(args),
                cwd=str(cwd or self.path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise GitError("git %s timed out after %ss." % (args[0], timeout))
        if check and proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            low = err.lower()
            if any(m in low for m in _NETWORK_MARKERS):
                raise NetworkError(
                    "Cannot reach github.com to update the ComfyUI clone.\n"
                    "  git said: %s\n"
                    "Re-run with --offline to answer from the shipped ledger and the "
                    "existing clone only." % err.splitlines()[0]
                )
            raise GitError("git %s failed:\n  %s" % (" ".join(args), err))
        return proc

    def _log(self, msg):
        if not self.quiet:
            print(msg, file=sys.stderr)

    # ------------------------------------------------------------------ clone

    def ensure(self, deep=False):
        """Make sure the clone exists; unshallow it when history is needed."""
        if not (self.path / ".git").exists():
            if self.offline:
                raise NetworkError(
                    "No ComfyUI clone at %s and --offline was given.\n"
                    "Drop --offline (or set %s to a directory that already holds one)."
                    % (self.path, CACHE_ENV)
                )
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._log("comfy-import-guard: cloning ComfyUI into %s (one time)..." % self.path)
            args = ["clone", "--quiet"]
            if not deep:
                args += ["--depth", "1"]
            args += [self.url, str(self.path)]
            self._git(args, cwd=self.cache_dir)
        self._ready = True
        if deep:
            self._ensure_deep()
        return self

    def _ensure_deep(self):
        if self._deep:
            return
        shallow = (self.path / ".git" / "shallow").exists()
        if shallow:
            if self.offline:
                raise NetworkError(
                    "The clone at %s is shallow and --offline was given, so history "
                    "is unavailable. Re-run without --offline once to unshallow it." % self.path
                )
            self._log("comfy-import-guard: fetching full ComfyUI history (one time)...")
            self._git(["fetch", "--unshallow", "--tags", "--quiet"])
        elif not self.offline:
            self._git(["fetch", "--tags", "--quiet"], check=False)
        self._deep = True
        self._tags = None

    def update(self):
        """Refresh remote refs. No-op when offline."""
        if self.offline:
            return
        self._git(["fetch", "--tags", "--quiet", "origin"], check=False)
        self._file_cache.clear()
        self._tags = None

    def is_dirty(self):
        """True when the cached clone has local edits, which taint ``git show``."""
        if not (self.path / ".git").exists():
            return False
        proc = self._git(["status", "--porcelain"], check=False)
        return bool(proc.stdout.strip())

    # ----------------------------------------------------------------- queries

    def resolve_ref(self, ref):
        """Return the sha for ``ref``, with a message that names the fix."""
        proc = self._git(["rev-parse", "--verify", "%s^{commit}" % ref], check=False)
        if proc.returncode != 0:
            if not self.offline and not self._deep:
                # A shallow clone only has origin/HEAD; older refs need history.
                self._ensure_deep()
                proc = self._git(["rev-parse", "--verify", "%s^{commit}" % ref], check=False)
            if proc.returncode != 0:
                raise RefError(
                    "Ref %r does not exist in the ComfyUI clone at %s.\n"
                    "Try a release tag (v0.32.0), a sha, or origin/master."
                    % (ref, self.path)
                )
        return proc.stdout.strip()

    def read_file(self, ref, path):
        """Source of ``path`` at ``ref``, or None when the file is absent there."""
        key = (ref, path)
        if key in self._file_cache:
            return self._file_cache[key]
        proc = self._git(["show", "%s:%s" % (ref, path)], check=False)
        text = proc.stdout if proc.returncode == 0 else None
        self._file_cache[key] = text
        return text

    def is_dir(self, ref, path):
        """True when ``path`` is a directory at ``ref``.

        ComfyUI's ``comfy/`` has no ``__init__.py`` - it is imported as a
        namespace package off sys.path - so a file-only existence test would
        call every ``comfy.<submodule>`` reference missing.
        """
        key = ("tree", ref, path)
        if key in self._file_cache:
            return self._file_cache[key]
        proc = self._git(["cat-file", "-t", "%s:%s" % (ref, path)], check=False)
        result = proc.stdout.strip() == "tree"
        self._file_cache[key] = result
        return result

    def pickaxe(self, symbol, path):
        """Commits whose diff changes the number of occurrences of ``symbol``.

        The regex is word-anchored on purpose. A plain ``-S<symbol>`` counts
        substrings, so a commit that deletes ``def precompute_freqs_cis`` while
        adding two uses of ``_precompute_freqs_cis`` nets to zero and is
        invisible - which is exactly the real ComfyUI #11632 removal.
        """
        self._ensure_deep()
        # \b, not a lookaround: git's --pickaxe-regex is POSIX ERE plus GNU
        # extensions, and a lookbehind silently matches nothing.
        pattern = r"\b%s\b" % _escape(symbol)
        out = self._pickaxe_raw(["-S" + pattern, "--pickaxe-regex"], path)
        if not out:
            # Symbols that are not plain identifiers get a literal search.
            out = self._pickaxe_raw(["-S" + symbol], path)
        return out

    def _pickaxe_raw(self, search_args, path):
        proc = self._git(
            ["log"] + list(search_args) + ["--format=%H%x09%s%x09%cI", "--", path],
            check=False,
        )
        out = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                out.append({"sha": parts[0], "subject": parts[1], "date": parts[2]})
        return out

    def commit_info(self, ref):
        proc = self._git(["log", "-1", "--format=%H%x09%s%x09%cI%x09%an", ref], check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise RefError("Commit %r not found in the clone." % ref)
        sha, subject, date, author = proc.stdout.strip().split("\t", 3)
        return {"sha": sha, "subject": subject, "date": date, "author": author}

    def tags_containing(self, sha):
        self._ensure_deep()
        proc = self._git(["tag", "--contains", sha], check=False)
        return [t.strip() for t in proc.stdout.splitlines() if t.strip()]

    def all_tags(self):
        self._ensure_deep()
        if self._tags is None:
            proc = self._git(["tag"], check=False)
            self._tags = [t.strip() for t in proc.stdout.splitlines() if t.strip()]
        return self._tags


def _escape(symbol):
    return "".join("\\" + c if not (c.isalnum() or c == "_") else c for c in symbol)
