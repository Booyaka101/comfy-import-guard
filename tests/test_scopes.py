"""Lexical isolation for references, call sites and replacement signatures."""

import ast
import sys

import pytest

from comfy_import_guard.extract import ATTR, GETATTR, extract_calls, extract_references, scan_pack


def analyse(source):
    tree = ast.parse(source)
    refs = [r for r in extract_references(tree, "nodes.py") if r.kind in (ATTR, GETATTR)]
    calls, patches = extract_calls(tree, "nodes.py")
    return refs, calls, patches


def test_parameter_does_not_hide_calls_in_sibling_or_module_scope():
    refs, calls, _ = analyse(
        "import comfy.lora as api\n"
        "def unrelated(api):\n    api.fake()\n"
        "def real():\n    api.calculate_weight(1)\n"
        "api.calculate_weight(2)\n"
    )
    assert [(r.dotted, r.lineno) for r in refs] == [
        ("comfy.lora.calculate_weight", 6), ("comfy.lora.calculate_weight", 5)]
    assert {c.lineno for c in calls} == {5, 6}


def test_local_imports_never_leak_to_siblings_or_module():
    refs, calls, _ = analyse(
        "def one():\n    import comfy.lora as api\n    api.calculate_weight(1)\n"
        "def two():\n    import comfy.utils as api\n    api.ProgressBar(2)\n"
        "def unrelated():\n    api.fake()\n"
        "api.fake()\n"
    )
    assert {r.dotted for r in refs} == {"comfy.lora.calculate_weight", "comfy.utils.ProgressBar"}
    assert {c.dotted for c in calls} == {"comfy.lora.calculate_weight", "comfy.utils.ProgressBar"}


@pytest.mark.parametrize("statement", [
    "api = object()",
    "api: object = object()",
    "api += other",
    "api, other = items",
    "*api, other = items",
    "for api in items:\n        pass",
    "with context() as api:\n        pass",
    "try:\n        pass\n    except Exception as api:\n        pass",
    "if (api := object()):\n        pass",
    "import unrelated as api",
    "from unrelated import api",
    "def api():\n        pass",
    "class api:\n        pass",
    "del api",
    "match subject:\n        case {'value': api}:\n            pass",
    "match subject:\n        case [*api]:\n            pass",
    "match subject:\n        case {**api}:\n            pass",
])
def test_local_writes_shadow_only_their_own_function(statement):
    refs, calls, _ = analyse(
        "import comfy.lora as api\n"
        "def shadowed():\n    " + statement + "\n    api.fake()\n"
        "def real():\n    api.calculate_weight(1)\n"
    )
    assert [r.dotted for r in refs] == ["comfy.lora.calculate_weight"]
    assert [c.dotted for c in calls] == ["comfy.lora.calculate_weight"]


def test_from_import_parameter_is_scoped():
    _, calls, _ = analyse(
        "from comfy.lora import calculate_weight\n"
        "def shadowed(calculate_weight):\n    calculate_weight(0)\n"
        "def real():\n    calculate_weight(1)\n"
    )
    assert [(c.dotted, c.lineno) for c in calls] == [("comfy.lora.calculate_weight", 5)]


def test_local_aliases_are_isolated_and_can_chain():
    _, calls, _ = analyse(
        "import comfy.lora as api\n"
        "def one():\n    original = api.calculate_weight\n    saved = original\n    saved(1)\n"
        "def two():\n    original = object()\n    original(2)\n"
        "original(3)\n"
    )
    assert [(c.dotted, c.lineno) for c in calls] == [("comfy.lora.calculate_weight", 5)]


@pytest.mark.parametrize("aliases", [
    "first = second\nsecond = first\nsecond()\n",
    "first = api.calculate_weight\n" + "".join(
        "alias%d = %s\n" % (i, "first" if i == 0 else "alias%d" % (i - 1))
        for i in range(100)) + "alias99()\n",
])
def test_cyclic_and_deep_aliases_stay_bounded(aliases):
    _, calls, _ = analyse("import comfy.lora as api\n" + aliases)
    assert calls == []


def test_nested_function_captures_import_but_respects_enclosing_parameter():
    refs, calls, _ = analyse(
        "import comfy.lora as api\n"
        "def outer():\n    def inner():\n        api.calculate_weight(1)\n"
        "def shadowed(api):\n    def inner():\n        api.fake()\n"
    )
    assert [r.dotted for r in refs] == ["comfy.lora.calculate_weight"]
    assert [c.dotted for c in calls] == ["comfy.lora.calculate_weight"]


def test_function_defaults_and_decorators_use_enclosing_scope():
    _, calls, _ = analyse(
        "import comfy.lora as api\n"
        "@api.decorate()\n"
        "def run(api=api.calculate_weight(1)):\n    api.fake()\n"
        "other = lambda api=api.calculate_weight(2): api.fake()\n"
    )
    assert [(c.dotted, c.lineno) for c in calls] == [
        ("comfy.lora.decorate", 2),
        ("comfy.lora.calculate_weight", 3),
        ("comfy.lora.calculate_weight", 5),
    ]


def test_class_body_import_does_not_leak_into_method_globals():
    refs, calls, _ = analyse(
        "class Node:\n"
        "    import comfy.lora as api\n"
        "    value = api.calculate_weight(1)\n"
        "    def run(self):\n        api.fake()\n"
        "api.fake()\n"
    )
    assert [r.dotted for r in refs] == ["comfy.lora.calculate_weight"]
    assert [c.lineno for c in calls] == [3]


def test_method_closure_skips_class_bindings():
    refs, calls, _ = analyse(
        "import comfy.lora as api\n"
        "class Node:\n    api = object()\n"
        "    def run(self):\n        api.calculate_weight(1)\n"
    )
    assert [r.dotted for r in refs] == ["comfy.lora.calculate_weight"]
    assert [c.lineno for c in calls] == [5]


@pytest.mark.parametrize("expression", [
    "[api.fake() for api in items]",
    "{api.fake() for api in items}",
    "{api.fake(): 1 for api in items}",
    "(api.fake() for api in items)",
    "[other.fake() for api in items for other in api.things]",
])
def test_comprehension_targets_do_not_leak(expression):
    refs, calls, _ = analyse(
        "import comfy.lora as api\n"
        "result = " + expression + "\n"
        "api.calculate_weight(1)\n"
    )
    assert [r.dotted for r in refs] == ["comfy.lora.calculate_weight"]
    assert [c.lineno for c in calls] == [3]


def test_comprehension_first_iterable_is_evaluated_in_enclosing_scope():
    _, calls, _ = analyse(
        "import comfy.lora as api\n"
        "result = [api.fake() for api in api.calculate_weight(1)]\n"
    )
    assert [c.dotted for c in calls] == ["comfy.lora.calculate_weight"]


def test_walrus_in_comprehension_invalidates_enclosing_binding():
    refs, calls, _ = analyse(
        "import comfy.lora as api\n"
        "values = [(api := value) for value in items]\n"
        "api.fake()\n"
    )
    assert refs == calls == []


@pytest.mark.parametrize("declaration", ["global api", "nonlocal api"])
def test_external_rebinding_stays_conservative(declaration):
    refs, calls, _ = analyse(
        "import comfy.lora as api\n"
        "def outer():\n    import comfy.lora as api\n"
        "    def change():\n        " + declaration + "\n        api = object()\n"
        "    api.fake()\n"
    )
    expected = ["comfy.lora.fake"] if declaration.startswith("global") else []
    assert [r.dotted for r in refs] == [c.dotted for c in calls] == expected


def test_same_named_replacement_functions_are_scoped():
    _, _, patches = analyse(
        "import comfy.lora as api\n"
        "def one():\n    def mine(a): pass\n    api.calculate_weight = mine\n"
        "def two():\n    def mine(a, b): pass\n    setattr(api, 'calculate_weight', mine)\n"
    )
    assert {p.replacement.args for p in patches} == {("a",), ("a", "b")}


def test_shadowed_target_and_replacement_do_not_create_monkeypatches():
    _, _, patches = analyse(
        "import comfy.lora as api\n"
        "def mine(a): pass\n"
        "def target(api):\n    api.calculate_weight = mine\n"
        "def replacement(mine):\n    api.calculate_weight = mine\n"
    )
    assert patches == []


def test_shadowed_reflection_builtins_do_not_create_fake_targets():
    refs, _, patches = analyse(
        "import comfy.lora as api\n"
        "def mine(a): pass\n"
        "def reflection(getattr, setattr):\n"
        "    getattr(api, 'fake')\n    setattr(api, 'fake', mine)\n"
    )
    assert refs == patches == []


def test_pack_scan_preserves_softness_paths_and_scope_isolation(tmp_path):
    source = tmp_path / "sub" / "nodes.py"
    source.parent.mkdir()
    source.write_text(
        "import comfy.lora as api\n"
        "def unrelated(api):\n    api.fake()\n"
        "def real():\n    try:\n        api.calculate_weight(1)\n"
        "    except TypeError:\n        pass\n",
        encoding="utf-8",
    )
    result = scan_pack(str(tmp_path))
    assert result.python_files == 1
    assert [c.file for c in result.call_sites] == ["sub/nodes.py"]
    assert result.call_sites[0].soft
    assert not any(r.symbol == "fake" for r in result.references)


@pytest.mark.skipif(sys.version_info < (3, 12), reason="type parameters require Python 3.12+")
def test_type_parameters_shadow_imports_only_in_their_generic_scope():
    refs, calls, _ = analyse(
        "import comfy.lora as api\n"
        "def generic[api]():\n    api.fake()\n"
        "class Generic[api]:\n    value = api.fake()\n"
        "    def run(self):\n        api.fake()\n"
        "api.calculate_weight(1)\n"
    )
    assert [r.dotted for r in refs] == ["comfy.lora.calculate_weight"]
    assert [c.dotted for c in calls] == ["comfy.lora.calculate_weight"]


@pytest.mark.skipif(sys.version_info < (3, 12), reason="type aliases require Python 3.12+")
def test_lazy_type_alias_values_are_not_call_sites():
    _, calls, _ = analyse(
        "import comfy.lora as api\n"
        "type Alias[api] = api.fake()\n"
        "api.calculate_weight(1)\n"
    )
    assert [c.dotted for c in calls] == ["comfy.lora.calculate_weight"]
