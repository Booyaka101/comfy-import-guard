"""Static extraction of every reference a custom-node pack makes into ``comfy.*``.

Everything here is pure ``ast``. A text search would be wrong in both
directions: it misses aliased attribute access and it reports a name as present
when the only remaining occurrence is a private class method.
"""

import ast
import os
from dataclasses import dataclass, field

from .errors import BadInputError

SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "site-packages",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    ".egg-info",
}

# Names that, when caught, mean the author already tolerates the import failing.
SOFT_EXCEPTIONS = {
    "ImportError",
    "ModuleNotFoundError",
    "AttributeError",
    "Exception",
    "BaseException",
}

# kind values
FROM = "from"          # from comfy.x import y
ATTR = "attr"          # comfy.x.y  /  alias.y
GETATTR = "getattr"     # getattr(comfy.x, "y")
STAR = "star"          # from comfy.x import *
MODULE = "module"      # import comfy.x  (no symbol touched)


@dataclass(frozen=True)
class Reference:
    module: str
    symbol: str          # "" for MODULE, "*" for STAR
    file: str
    lineno: int
    kind: str = FROM
    soft: bool = False   # guarded by try/except ImportError

    @property
    def dotted(self):
        return self.module if not self.symbol else "%s.%s" % (self.module, self.symbol)


@dataclass
class PackScan:
    name: str
    path: str
    references: list = field(default_factory=list)
    unparseable: list = field(default_factory=list)   # (relpath, message)
    vendored_comfy: bool = False
    python_files: int = 0


def iter_python_files(root):
    """Yield .py files under ``root``, skipping caches, vendored trees and venvs."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.endswith(".egg-info") and not d.startswith(".")
        ]
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def has_vendored_comfy(pack_dir):
    """A pack shipping its own ``comfy/`` resolves against that, not ComfyUI."""
    p = os.path.join(pack_dir, "comfy")
    if os.path.isdir(p) and os.path.exists(os.path.join(p, "__init__.py")):
        return True
    return os.path.isfile(os.path.join(pack_dir, "comfy.py"))


def scan_pack(pack_dir, name=None):
    """Extract every comfy.* reference in one custom-node pack."""
    pack_dir = os.path.abspath(os.path.expanduser(str(pack_dir)))
    if not os.path.isdir(pack_dir):
        raise BadInputError(
            "No such custom-node pack directory: %s\n"
            "Pass the folder that holds the pack's __init__.py." % pack_dir
        )
    scan = PackScan(name=name or os.path.basename(pack_dir.rstrip(os.sep)), path=pack_dir)
    if has_vendored_comfy(pack_dir):
        scan.vendored_comfy = True
        return scan
    for path in iter_python_files(pack_dir):
        scan.python_files += 1
        rel = os.path.relpath(path, pack_dir).replace(os.sep, "/")
        try:
            with open(path, "rb") as fh:
                source = fh.read()
        except OSError as exc:
            scan.unparseable.append((rel, "unreadable: %s" % exc))
            continue
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            scan.unparseable.append((rel, "SyntaxError line %s: %s" % (exc.lineno, exc.msg)))
            continue
        except ValueError as exc:  # null bytes, absurd nesting
            scan.unparseable.append((rel, str(exc)))
            continue
        scan.references.extend(extract_references(tree, rel))
    scan.references = _dedupe(scan.references)
    return scan


def extract_references(tree, filename):
    """All comfy.* references in one parsed module."""
    soft = _soft_line_ranges(tree)
    aliases = _alias_map(tree)
    refs = []
    refs.extend(_import_refs(tree, filename, soft))
    refs.extend(_attribute_refs(tree, filename, aliases, soft))
    return refs


# ------------------------------------------------------------------ internals


def _is_soft(lineno, soft_ranges):
    return any(lo <= lineno <= hi for lo, hi in soft_ranges)


def _soft_line_ranges(tree):
    """Line spans of try-bodies whose handlers swallow an import failure."""
    ranges = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(_handler_is_soft(h) for h in node.handlers):
            continue
        for stmt in node.body:
            lo = stmt.lineno
            hi = getattr(stmt, "end_lineno", None) or lo
            ranges.append((lo, hi))
    return ranges


def _handler_is_soft(handler):
    t = handler.type
    if t is None:
        return True
    names = []
    if isinstance(t, ast.Tuple):
        names = [_name_of(e) for e in t.elts]
    else:
        names = [_name_of(t)]
    return any(n in SOFT_EXCEPTIONS for n in names if n)


def _name_of(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _alias_map(tree):
    """Local name -> comfy dotted module it is bound to."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if not _is_comfy(a.name):
                    continue
                if a.asname:
                    aliases[a.asname] = a.name
                else:
                    # `import comfy.ldm.x` binds the top package name only.
                    aliases[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module or not _is_comfy(node.module):
                continue
            for a in node.names:
                if a.name == "*":
                    continue
                # `from comfy import model_management` may bind a submodule; the
                # resolver decides. Binding it here lets attribute chains through.
                aliases[a.asname or a.name] = "%s.%s" % (node.module, a.name)
    return aliases


def _is_comfy(dotted):
    return dotted == "comfy" or dotted.startswith("comfy.")


def _import_refs(tree, filename, soft):
    refs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level or not node.module or not _is_comfy(node.module):
                continue
            s = _is_soft(node.lineno, soft)
            for a in node.names:
                if a.name == "*":
                    refs.append(Reference(node.module, "*", filename, node.lineno, STAR, s))
                else:
                    refs.append(Reference(node.module, a.name, filename, node.lineno, FROM, s))
        elif isinstance(node, ast.Import):
            s = _is_soft(node.lineno, soft)
            for a in node.names:
                if _is_comfy(a.name):
                    refs.append(Reference(a.name, "", filename, node.lineno, MODULE, s))
    return refs


def _attribute_refs(tree, filename, aliases, soft):
    """Outermost attribute chains rooted at a comfy alias, plus getattr()."""
    if not aliases:
        return []
    inner = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
            inner.add(id(node.value))

    refs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_getattr(node) and len(node.args) >= 2:
            dotted = _chain(node.args[0], aliases)
            target = node.args[1]
            if dotted and isinstance(target, ast.Constant) and isinstance(target.value, str):
                refs.append(
                    Reference(dotted, target.value, filename, node.lineno, GETATTR,
                              _is_soft(node.lineno, soft))
                )
            continue
        if not isinstance(node, ast.Attribute) or id(node) in inner:
            continue
        dotted = _chain(node, aliases)
        if not dotted:
            continue
        parts = dotted.split(".")
        if len(parts) < 2:
            continue
        refs.append(
            Reference(".".join(parts[:-1]), parts[-1], filename, node.lineno, ATTR,
                      _is_soft(node.lineno, soft))
        )
    return refs


def _is_getattr(call):
    return isinstance(call.func, ast.Name) and call.func.id == "getattr"


def _chain(node, aliases):
    """Expand an attribute chain to a comfy dotted path, or None."""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    root = aliases.get(cur.id)
    if root is None:
        return None
    parts.append(root)
    parts.reverse()
    dotted = ".".join(parts)
    return dotted if _is_comfy(dotted) else None


def _dedupe(refs):
    seen = set()
    out = []
    for r in sorted(refs, key=lambda r: (r.file, r.lineno, r.module, r.symbol)):
        key = (r.module, r.symbol, r.file, r.lineno, r.kind)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
