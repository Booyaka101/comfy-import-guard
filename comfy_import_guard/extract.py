"""Static extraction of every reference a custom-node pack makes into ``comfy.*``.

Everything here is pure ``ast``. A text search would be wrong in both
directions: it misses aliased attribute access and it reports a name as present
when the only remaining occurrence is a private class method.
"""

import ast
import os
from dataclasses import dataclass, field

from .errors import BadInputError
from .signature import ParamSpec

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


@dataclass(frozen=True)
class CallSite:
    """A direct call into comfy.*: enough shape to try binding it upstream."""

    dotted: str
    file: str
    lineno: int
    nargs: int              # positional arguments at the call
    keywords: tuple         # keyword names, in order
    star_args: bool         # *xs spread at the call
    star_kwargs: bool       # **kw spread at the call
    soft: bool = False


@dataclass(frozen=True)
class Monkeypatch:
    """An assignment that replaces a comfy.* callable with a local function."""

    dotted: str             # the comfy target being replaced
    file: str
    lineno: int
    replacement: ParamSpec  # the replacement's own parameter list
    soft: bool = False


@dataclass
class PackScan:
    name: str
    path: str
    references: list = field(default_factory=list)
    call_sites: list = field(default_factory=list)
    monkeypatches: list = field(default_factory=list)
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
        calls, patches = extract_calls(tree, rel)
        scan.call_sites.extend(calls)
        scan.monkeypatches.extend(patches)
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


def extract_calls(tree, filename):
    """Call sites into comfy.* and monkeypatches over comfy.* in one module.

    Kept apart from ``extract_references``: a call site needs the argument
    shape and a monkeypatch needs the replacement's parameter list, neither of
    which fits the Reference model.
    """
    soft = _soft_line_ranges(tree)
    aliases = _alias_map(tree)
    if not aliases:
        return [], []
    defs = _function_def_map(tree)
    # `orig = comfy.lora.calculate_weight` binds a local alias to a callable;
    # a later `orig(...)` is a call site against that dotted path. This map is
    # deliberately not fed into _attribute_refs, which keeps reference
    # extraction byte-identical to 1.0.x.
    value_aliases = _value_alias_map(tree, aliases)
    calls = []
    patches = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            patches.extend(_patches_from_assign(node, aliases, defs, filename, soft))
        elif isinstance(node, ast.Call):
            if _is_getattr(node):
                continue
            if _is_setattr(node):
                patch = _patch_from_setattr(node, aliases, defs, filename, soft)
                if patch:
                    patches.append(patch)
                continue
            dotted = _call_target(node.func, aliases, value_aliases)
            if not dotted or len(dotted.split(".")) < 2:
                continue
            calls.append(CallSite(
                dotted, filename, node.lineno,
                nargs=sum(1 for a in node.args if not isinstance(a, ast.Starred)),
                keywords=tuple(k.arg for k in node.keywords if k.arg is not None),
                star_args=any(isinstance(a, ast.Starred) for a in node.args),
                star_kwargs=any(k.arg is None for k in node.keywords),
                soft=_is_soft(node.lineno, soft),
            ))
    return calls, patches


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


def _is_setattr(call):
    return isinstance(call.func, ast.Name) and call.func.id == "setattr"


def _call_target(func, aliases, value_aliases):
    """Dotted comfy path a call's ``func`` resolves to, or None."""
    if isinstance(func, ast.Attribute):
        return _chain(func, aliases)
    if isinstance(func, ast.Name):
        dotted = value_aliases.get(func.id) or aliases.get(func.id)
        if dotted and _is_comfy(dotted):
            return dotted
    return None


def _function_def_map(tree):
    """Name -> parameter spec for every plain function defined in this file.

    A decorated def, or two same-named defs whose parameter lists differ, maps
    to None: the actual signature is not statically knowable, and a wrong
    verdict is worse than silence.
    """
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        spec = None if node.decorator_list else ParamSpec.from_arguments(node.args)
        if node.name in out and out[node.name] != spec:
            out[node.name] = None
        else:
            out[node.name] = spec
    return out


def _value_alias_map(tree, aliases):
    """Local name -> comfy dotted path, from ``name = comfy.x.y`` assignments."""
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not isinstance(node.value, ast.Attribute):
            continue
        dotted = _chain(node.value, aliases)
        if dotted and len(dotted.split(".")) >= 2:
            out[target.id] = dotted
    return out


def _patches_from_assign(node, aliases, defs, filename, soft):
    """``comfy.x.y = fn`` where fn's parameter list is knowable in this file."""
    spec = _replacement_spec(node.value, defs)
    if spec is None:
        return
    for target in node.targets:
        if not isinstance(target, ast.Attribute):
            continue
        dotted = _chain(target, aliases)
        if dotted and len(dotted.split(".")) >= 2:
            yield Monkeypatch(dotted, filename, node.lineno, spec,
                              _is_soft(node.lineno, soft))


def _patch_from_setattr(call, aliases, defs, filename, soft):
    """``setattr(comfy.x, "y", fn)`` with a literal name."""
    if len(call.args) != 3:
        return None
    base = _chain(call.args[0], aliases) if isinstance(call.args[0], ast.Attribute) else (
        aliases.get(call.args[0].id) if isinstance(call.args[0], ast.Name) else None)
    name = call.args[1]
    if not base or not _is_comfy(base):
        return None
    if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
        return None
    spec = _replacement_spec(call.args[2], defs)
    if spec is None:
        return None
    return Monkeypatch("%s.%s" % (base, name.value), filename, call.lineno, spec,
                       _is_soft(call.lineno, soft))


def _replacement_spec(value, defs):
    """Parameter spec of the replacement expression, or None when unknowable.

    A lambda carries its own arguments; a bare name must be a plain FunctionDef
    in the same file. functools.partial, decorated defs and anything imported
    stay None - the brief for all of them is silence, not a guess.
    """
    if isinstance(value, ast.Lambda):
        return ParamSpec.from_arguments(value.args)
    if isinstance(value, ast.Name):
        return defs.get(value.id)
    return None


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
