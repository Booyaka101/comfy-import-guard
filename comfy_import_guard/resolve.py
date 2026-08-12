"""Resolve a reference against ComfyUI's source at a given ref.

A name is importable only if it is bound at module scope. That is why the
exported-name set is built from top-level ``FunctionDef`` / ``AsyncFunctionDef``
/ ``ClassDef`` / ``Assign`` / ``AnnAssign`` / ``Import*`` nodes and never from a
text search: ComfyUI #11632 deleted ``def precompute_freqs_cis`` and added a
private ``_precompute_freqs_cis`` method on the model class, so the string
is still in the file twice while the import is dead.
"""

import ast
from dataclasses import dataclass

from .extract import FROM, GETATTR, MODULE, STAR

PRESENT = "PRESENT"
MISSING = "MISSING"
MODULE_MISSING = "MODULE_MISSING"
UNRESOLVABLE = "UNRESOLVABLE"

BREAKING = (MISSING, MODULE_MISSING)


@dataclass
class Resolution:
    reference: object
    status: str
    module: str          # module the symbol was actually looked up in
    symbol: str
    detail: str = ""

    @property
    def breaking(self):
        return self.status in BREAKING


class Resolver:
    """Answers "does this name exist at this ref" with a per-ref cache."""

    def __init__(self, repo, ref):
        self.repo = repo
        self.ref = ref
        self._exports = {}     # module -> set | None
        self._is_module = {}   # module -> bool

    # ------------------------------------------------------------- module I/O

    def module_paths(self, module):
        base = module.replace(".", "/")
        return [base + ".py", base + "/__init__.py"]

    def module_source(self, module):
        for path in self.module_paths(module):
            src = self.repo.read_file(self.ref, path)
            if src is not None:
                return src, path
        return None, None

    def module_exists(self, module):
        """A module exists if it has a .py, an __init__.py, or is a plain dir."""
        if module not in self._is_module:
            src, _ = self.module_source(module)
            if src is not None:
                self._is_module[module] = True
                self._exports.setdefault(module, exported_names(src))
            else:
                is_ns = self.repo.is_dir(self.ref, module.replace(".", "/"))
                self._is_module[module] = is_ns
                if is_ns:
                    self._exports.setdefault(module, set())
        return self._is_module[module]

    def exports(self, module):
        """Set of module-scope names at ``ref``, or None when the module is gone."""
        if module in self._exports:
            return self._exports[module]
        if not self.module_exists(module):
            self._exports[module] = None
        return self._exports.get(module)

    # -------------------------------------------------------------- resolving

    def resolve(self, ref):
        if ref.kind == MODULE:
            if self.module_exists(ref.module):
                return Resolution(ref, PRESENT, ref.module, "", "module present")
            return Resolution(ref, MODULE_MISSING, ref.module, "",
                              "module %s does not exist at %s" % (ref.module, self.ref))

        if ref.kind == STAR:
            if not self.module_exists(ref.module):
                return Resolution(ref, MODULE_MISSING, ref.module, "*",
                                  "module %s does not exist at %s" % (ref.module, self.ref))
            return Resolution(ref, UNRESOLVABLE, ref.module, "*",
                              "star import: names cannot be checked statically")

        if ref.kind == FROM:
            # `from a.b import c` names its module unambiguously - no guessing.
            if not self.module_exists(ref.module):
                return Resolution(ref, MODULE_MISSING, ref.module, ref.symbol,
                                  "module %s does not exist at %s" % (ref.module, self.ref))
            return self._lookup(ref, ref.module, ref.symbol)

        parts = (ref.module + "." + ref.symbol).split(".")
        # Longest module prefix wins: comfy.samplers.KSampler.SAMPLERS resolves
        # against comfy.samplers with symbol KSampler.
        for i in range(len(parts) - 1, 0, -1):
            module = ".".join(parts[:i])
            if not self.module_exists(module):
                continue
            return self._lookup(ref, module, parts[i])

        return Resolution(ref, MODULE_MISSING, ref.module, ref.symbol,
                          "no module in %s exists at %s" % (ref.dotted, self.ref))

    def _lookup(self, ref, module, symbol):
        names = self.exports(module) or set()
        if symbol in names:
            return Resolution(ref, PRESENT, module, symbol)
        if self.module_exists(module + "." + symbol):
            return Resolution(ref, PRESENT, module, symbol, "resolved as submodule")
        if ref.kind == GETATTR:
            return Resolution(ref, MISSING, module, symbol,
                              "dynamic getattr target not defined at module scope")
        return Resolution(ref, MISSING, module, symbol)


def exported_names(source):
    """Module-scope names bound by ``source``.

    Descends into top-level ``if`` / ``try`` / ``with`` / ``for`` bodies because
    those still bind at module scope, but never into a class or function body.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    names = set()
    _collect(tree.body, names)
    return names


_BLOCKS = (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)


def _collect(body, names):
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                _bind(target, names)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            _bind(node.target, names)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    names.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, _BLOCKS):
            _collect(node.body, names)
            _collect(getattr(node, "orelse", []) or [], names)
            _collect(getattr(node, "finalbody", []) or [], names)
            for handler in getattr(node, "handlers", []) or []:
                _collect(handler.body, names)


def _bind(target, names):
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for el in target.elts:
            _bind(el, names)
