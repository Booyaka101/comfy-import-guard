"""Conservative lexical bindings, without importing or executing pack code.

Conflicting writes invalidate a name within its scope, regardless of order.
This is not control-flow analysis: uncertainty must not become a hard verdict.
"""

import ast

from .signature import ParamSpec


class _Scope:
    def __init__(self, parent=None, kind="module"):
        self.parent = parent
        self.kind = kind
        self.bindings = {}
        self.external = {}
        self.wildcard = False

    def bind(self, name, value=None):
        if name in self.bindings and self.bindings[name] != value:
            value = None
        self.bindings[name] = value


class ScopeIndex(ast.NodeVisitor):
    def __init__(self, tree):
        self.scope = _Scope()
        self.scopes = [self.scope]
        self.by_node = {}
        self._maps = {}
        self.deferred_annotations = any(
            isinstance(node, ast.ImportFrom) and node.module == "__future__" and
            any(alias.name == "annotations" for alias in node.names)
            for node in getattr(tree, "body", ())
        )
        self.visit(tree)
        # Writes through global/nonlocal can affect callers in other scopes.
        # Do not guess when those functions run, or which write wins.
        for scope in self.scopes:
            for name in scope.external:
                if name in scope.bindings:
                    owner = self._external_owner(scope, name)
                    if owner is not None:
                        owner.bindings[name] = None
                    del scope.bindings[name]

    def visit(self, node):
        self.by_node[node] = self.scope
        return super().visit(node)

    def _external_owner(self, scope, name):
        if scope.external[name] == "global":
            return self.scopes[0]
        parent = scope.parent
        while parent is not None and parent.kind != "module":
            if parent.kind != "class" and name in parent.bindings and name not in parent.external:
                return parent
            parent = parent.parent
        return None

    def _lookup(self, scope, name):
        while scope is not None:
            if name in scope.external:
                owner = self._external_owner(scope, name)
                if owner is None:
                    return scope, None
                scope = owner
            # A wildcard may replace any name, including getattr/setattr. Keep
            # explicit import references, but do not guess its unknown exports.
            if scope.wildcard:
                return scope, None
            if name in scope.bindings:
                return scope, scope.bindings[name]
            scope = scope.parent
        return None, None

    def _resolve(self, scope, name, active):
        owner, binding = self._lookup(scope, name)
        key = (owner, name)
        # Bound alias expansion even for a hostile pack with a very long chain.
        if binding is None or key in active or len(active) >= 64:
            return None
        kind, value = binding
        if kind != "alias":
            return binding
        return self._expression(owner, value, active | {key})

    def _expression(self, scope, node, active):
        if isinstance(node, ast.Name):
            return self._resolve(scope, node.id, active)
        if isinstance(node, ast.Attribute):
            base = self._expression(scope, node.value, active)
            if base and base[0] == "path":
                return "path", "%s.%s" % (base[1], node.attr)
        return None

    def maps(self, node):
        scope = self.by_node[node]
        if scope not in self._maps:
            names = set()
            parent = scope
            while parent is not None:
                names.update(parent.bindings)
                parent = parent.parent
            aliases, definitions = {}, {}
            for name in names:
                binding = self._resolve(scope, name, set())
                if binding:
                    (aliases if binding[0] == "path" else definitions)[name] = binding[1]
            self._maps[scope] = aliases, definitions
        return self._maps[scope]

    def is_builtin(self, node, name):
        owner, _ = self._lookup(self.by_node[node], name)
        return owner is None

    def _target(self, node, binding=None):
        if isinstance(node, ast.Name):
            self.by_node[node] = self.scope
            self.scope.bind(node.id, binding)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for item in node.elts:
                self._target(item)
        elif isinstance(node, ast.Starred):
            self._target(node.value)
        else:
            self.visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.scope.bind(node.id)

    def visit_Import(self, node):
        for alias in node.names:
            path = alias.name if alias.asname else alias.name.split(".")[0]
            binding = ("path", path) if _is_comfy(alias.name) else None
            self.scope.bind(alias.asname or path, binding)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.name == "*":
                self.scope.wildcard = True
                continue
            path = "%s.%s" % (node.module, alias.name)
            binding = ("path", path) if not node.level and node.module and _is_comfy(node.module) else None
            self.scope.bind(alias.asname or alias.name, binding)

    def visit_Assign(self, node):
        self.visit(node.value)
        binding = ("alias", node.value) if isinstance(node.value, (ast.Name, ast.Attribute)) else None
        for target in node.targets:
            self._target(target, binding)

    def visit_AnnAssign(self, node):
        if node.value is not None:
            self.visit(node.value)
            binding = ("alias", node.value) if isinstance(node.value, (ast.Name, ast.Attribute)) else None
            self._target(node.target, binding)
        elif isinstance(node.target, ast.Name) and self.scope.kind != "function":
            # At module/class scope this records an annotation, not a value.
            self.by_node[node.target] = self.scope
        else:
            self._target(node.target)
        if not self.deferred_annotations and self.scope.kind != "function":
            self.visit(node.annotation)

    def visit_NamedExpr(self, node):
        self.visit(node.value)
        scope = self.scope
        while scope.kind == "comprehension":
            scope = scope.parent
        if isinstance(node.target, ast.Name):
            scope.bind(node.target.id)
        self.by_node[node.target] = scope

    def visit_ExceptHandler(self, node):
        if node.name:
            self.scope.bind(node.name)
        self.generic_visit(node)

    def visit_Global(self, node):
        if self.scope.kind != "module":
            self.scope.external.update((name, "global") for name in node.names)

    def visit_Nonlocal(self, node):
        self.scope.external.update((name, "nonlocal") for name in node.names)

    def visit_MatchAs(self, node):
        if node.name:
            self.scope.bind(node.name)
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node):
        if node.rest:
            self.scope.bind(node.rest)
        self.generic_visit(node)

    def _enter(self, kind):
        parent = self.scope
        while parent.kind == "class":
            parent = parent.parent
        self.scope = _Scope(parent, kind)
        self.scopes.append(self.scope)

    def _arguments(self, args):
        for arg in args.posonlyargs + args.args + args.kwonlyargs:
            if arg.annotation and not self.deferred_annotations:
                self.visit(arg.annotation)
        for arg in (args.vararg, args.kwarg):
            if arg is not None and arg.annotation and not self.deferred_annotations:
                self.visit(arg.annotation)
        for default in args.defaults + args.kw_defaults:
            if default is not None:
                self.visit(default)

    def _parameters(self, args):
        for arg in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
            if arg is not None:
                self.scope.bind(arg.arg)

    def _type_parameters(self, node):
        for parameter in getattr(node, "type_params", ()):
            self.scope.bind(parameter.name)

    def _header(self, node):
        outer = self.scope
        if getattr(node, "type_params", ()):
            self.scope = _Scope(outer, "annotation")
            self.scopes.append(self.scope)
            self._type_parameters(node)
            # Bounds, constraints and defaults of type parameters are lazy.
        return outer

    def visit_FunctionDef(self, node):
        spec = None if node.decorator_list else ("def", ParamSpec.from_arguments(node.args))
        self.scope.bind(node.name, spec)
        for decorator in node.decorator_list:
            self.visit(decorator)
        outer = self._header(node)
        self._arguments(node.args)
        if node.returns and not self.deferred_annotations:
            self.visit(node.returns)
        self.scope = outer
        self._enter("function")
        self._type_parameters(node)
        self._parameters(node.args)
        for statement in node.body:
            self.visit(statement)
        self.scope = outer

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self._arguments(node.args)
        outer = self.scope
        self._enter("function")
        self._parameters(node.args)
        self.visit(node.body)
        self.scope = outer

    def visit_ClassDef(self, node):
        self.scope.bind(node.name)
        for expression in node.decorator_list:
            self.visit(expression)
        outer = self._header(node)
        for expression in node.bases + node.keywords:
            self.visit(expression)
        self.scope = outer
        self._enter("class")
        if getattr(node, "type_params", ()):
            # Methods skip class locals but still close over class type parameters.
            parameters = _Scope(self.scope.parent, "annotation")
            self.scopes.append(parameters)
            for parameter in node.type_params:
                parameters.bind(parameter.name)
            self.scope.parent = parameters
        for statement in node.body:
            self.visit(statement)
        self.scope = outer

    def visit_TypeAlias(self, node):
        # PEP 695 alias values are lazy annotation expressions, not call sites.
        self._target(node.name)

    def visit_ListComp(self, node):
        first, *rest = node.generators
        self.visit(first.iter)
        outer = self.scope
        self._enter("comprehension")
        self._target(first.target)
        for condition in first.ifs:
            self.visit(condition)
        for generator in rest:
            self.visit(generator.iter)
            self._target(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        for expression in ([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]):
            self.visit(expression)
        self.scope = outer

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp


def _is_comfy(dotted):
    return dotted == "comfy" or dotted.startswith("comfy.")
