"""Parameter-level resolution: does this call still bind at the target ref?

Presence checking (resolve.py) answers "does the name exist". This module
answers the next failure mode up: the name exists but its parameter list
moved, so the call raises TypeError the moment it runs. ComfyUI #5355
(``calculate_weight`` gained ``intermediate_dtype`` under a pack's outdated
monkeypatch) and #12134 (``WanAttentionBlock.forward`` gained
``context_img_len``) are both this shape.

A dotted path resolves through the longest module prefix that has source at
the ref, then at most one class level, so
``comfy.ldm.wan.model.WanAttentionBlock.forward`` reaches the method. A bare
class binds against its own ``__init__`` with ``self`` dropped, because the
caller never passes it. Anything deeper, decorated, re-exported or dynamic is
UNRESOLVED and stays silent: a wrong TypeError prediction is worse than a
missed one.
"""

import ast
from dataclasses import dataclass

FOUND = "FOUND"
UNRESOLVED = "UNRESOLVED"
PARSE_FAILED = "PARSE_FAILED"

_METHOD_DECORATORS = {"staticmethod", "classmethod"}


@dataclass(frozen=True)
class ParamSpec:
    """One callable's parameter list, as far as ``ast.arguments`` can say."""

    posonly: tuple = ()
    args: tuple = ()            # positional-or-keyword
    defaults: int = 0           # trailing positional parameters with defaults
    kwonly: tuple = ()
    kwonly_required: tuple = ()
    vararg: bool = False
    kwarg: bool = False

    @classmethod
    def from_arguments(cls, a, drop_first=False):
        posonly = [x.arg for x in a.posonlyargs]
        args = [x.arg for x in a.args]
        defaults = len(a.defaults)
        if drop_first:
            if posonly:
                posonly = posonly[1:]
            elif args:
                args = args[1:]
            defaults = min(defaults, len(posonly) + len(args))
        kwonly = tuple(x.arg for x in a.kwonlyargs)
        required = tuple(x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is None)
        return cls(tuple(posonly), tuple(args), defaults, kwonly, required,
                   a.vararg is not None, a.kwarg is not None)

    @property
    def positional(self):
        return self.posonly + self.args

    @property
    def names(self):
        return self.posonly + self.args + self.kwonly

    def render(self):
        cap = len(self.positional)
        out = []
        for i, name in enumerate(self.positional):
            out.append(name + ("=..." if i >= cap - self.defaults else ""))
        if self.vararg:
            out.append("*args")
        elif self.kwonly:
            out.append("*")
        for name in self.kwonly:
            out.append(name + ("" if name in self.kwonly_required else "=..."))
        if self.kwarg:
            out.append("**kwargs")
        return "(%s)" % ", ".join(out)


@dataclass
class SignatureLookup:
    status: str
    module: str = ""
    qualname: str = ""
    spec: ParamSpec = None
    detail: str = ""


class SignatureResolver:
    """Signature lookups against one ref, sharing the Resolver's file cache."""

    def __init__(self, resolver):
        self.resolver = resolver
        self._trees = {}    # module -> ast.Module | PARSE_FAILED | None
        self._cache = {}    # dotted -> SignatureLookup

    @property
    def ref(self):
        return self.resolver.ref

    def lookup(self, dotted):
        if dotted in self._cache:
            return self._cache[dotted]
        out = self._lookup(dotted)
        self._cache[dotted] = out
        return out

    def _lookup(self, dotted):
        parts = dotted.split(".")
        for i in range(len(parts) - 1, 0, -1):
            module = ".".join(parts[:i])
            tree = self._module_tree(module)
            if tree is None:
                continue
            rest = parts[i:]
            if tree == PARSE_FAILED:
                return SignatureLookup(
                    PARSE_FAILED, module, ".".join(rest),
                    detail="module %s could not be parsed at %s" % (module, self.ref))
            return lookup_in_tree(tree, module, rest)
        return SignatureLookup(UNRESOLVED, qualname=dotted,
                               detail="no module prefix of %s has source" % dotted)

    def _module_tree(self, module):
        if module in self._trees:
            return self._trees[module]
        src, _ = self.resolver.module_source(module)
        if src is None:
            tree = None
        else:
            try:
                tree = ast.parse(src.lstrip("\ufeff"))
            except (SyntaxError, ValueError):
                tree = PARSE_FAILED
        self._trees[module] = tree
        return tree


def lookup_in_tree(tree, module, rest):
    """Resolve up to Class.method inside one parsed module."""
    qual = ".".join(rest)
    if not rest or len(rest) > 2:
        return SignatureLookup(UNRESOLVED, module, qual,
                               detail="only one class level is resolved")
    node = _find_def(tree.body, rest[0])
    if node is None:
        return SignatureLookup(UNRESOLVED, module, qual,
                               detail="not a top-level def or class")

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if len(rest) == 2:
            return SignatureLookup(UNRESOLVED, module, qual,
                                   detail="%s is a function, not a class" % rest[0])
        if node.decorator_list:
            return SignatureLookup(UNRESOLVED, module, qual,
                                   detail="decorated; the wrapper may change the signature")
        return SignatureLookup(FOUND, module, qual, ParamSpec.from_arguments(node.args))

    # ClassDef
    if len(rest) == 1:
        init = _find_def(node.body, "__init__")
        if not isinstance(init, (ast.FunctionDef, ast.AsyncFunctionDef)) or init.decorator_list:
            return SignatureLookup(UNRESOLVED, module, qual,
                                   detail="class without its own plain __init__")
        return SignatureLookup(FOUND, module, qual,
                               ParamSpec.from_arguments(init.args, drop_first=True))

    meth = _find_def(node.body, rest[1])
    if not isinstance(meth, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return SignatureLookup(UNRESOLVED, module, qual,
                               detail="no plain method %s on class %s" % (rest[1], rest[0]))
    deco = [name_of(d) for d in meth.decorator_list]
    if any(d not in _METHOD_DECORATORS for d in deco):
        return SignatureLookup(UNRESOLVED, module, qual,
                               detail="decorated; the wrapper may change the signature")
    # Access through the class yields the plain function: a call site passes
    # self explicitly and a replacement is written with self, so nothing is
    # dropped. classmethod is the exception - cls binds on access.
    drop = "classmethod" in deco
    return SignatureLookup(FOUND, module, qual,
                           ParamSpec.from_arguments(meth.args, drop_first=drop))


def spec_from_source(source, qualname):
    """ParamSpec of ``qualname`` in raw module source, or None."""
    try:
        tree = ast.parse(source.lstrip("\ufeff"))
    except (SyntaxError, ValueError):
        return None
    lk = lookup_in_tree(tree, "", qualname.split("."))
    return lk.spec if lk.status == FOUND else None


def _find_def(body, name):
    """Last def/class named ``name``, descending into top-level if/try bodies."""
    found = None
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                found = node
        elif isinstance(node, (ast.If, ast.Try)):
            for sub_body in ([node.body, getattr(node, "orelse", []) or []]
                             + [h.body for h in getattr(node, "handlers", []) or []]):
                found = _find_def(sub_body, name) or found
    return found


def name_of(node):
    """Trailing name of a Name or Attribute node, or None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


# ------------------------------------------------------------------- binding


def bind_call(spec, nargs, keywords, star_args=False, star_kwargs=False):
    """(param, message) pairs that make this call raise TypeError.

    Returns None when the call is not statically checkable (*args/**kwargs
    spread at the call), an empty list when it binds.
    """
    if star_args or star_kwargs:
        return None
    problems = []
    cap = len(spec.positional)
    if nargs > cap and not spec.vararg:
        problems.append((None, "takes %d positional argument(s) but %d given" % (cap, nargs)))
    filled = set(spec.positional[:nargs])
    named = set(spec.args) | set(spec.kwonly)
    for kw in keywords:
        if kw in named:
            if kw in filled:
                problems.append((kw, "got multiple values for argument '%s'" % kw))
        elif kw in spec.posonly:
            if not spec.kwarg:
                problems.append((kw, "parameter '%s' is positional-only" % kw))
        elif not spec.kwarg:
            problems.append((kw, "unexpected keyword argument '%s'" % kw))
    required = list(spec.positional[:cap - spec.defaults]) + list(spec.kwonly_required)
    provided = filled | (set(keywords) & named)
    for name in required:
        if name not in provided:
            problems.append((name, "missing required argument '%s'" % name))
    return problems


def replacement_drops(upstream, replacement):
    """Upstream parameter names the replacement cannot accept.

    None when the comparison is moot: either side unknown, or *args/**kwargs
    anywhere, because then whether the call path breaks is not statically
    decidable.
    """
    if upstream is None or replacement is None:
        return None
    if upstream.vararg or upstream.kwarg or replacement.vararg or replacement.kwarg:
        return None
    have = set(replacement.names)
    return [n for n in upstream.names if n not in have]
