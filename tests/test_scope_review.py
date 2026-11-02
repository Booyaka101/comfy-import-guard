"""Adversarial controls for lexical binding and annotation evaluation."""

import ast
import sys

import pytest

from comfy_import_guard.extract import ATTR, GETATTR, extract_calls, extract_references


def analyse(source):
    tree = ast.parse(source)
    refs = [r.dotted for r in extract_references(tree, "nodes.py") if r.kind in (ATTR, GETATTR)]
    calls, patches = extract_calls(tree, "nodes.py")
    return refs, [c.dotted for c in calls], patches


@pytest.mark.parametrize("module", ["other", "comfy.utils"])
def test_wildcard_import_cannot_leave_known_aliases_or_builtins_trusted(module):
    refs, calls, patches = analyse(
        "import comfy.lora as api\n"
        "from " + module + " import *\n"
        "def replacement(a): pass\n"
        "api.fake(1)\n"
        "getattr(api, 'fake')\n"
        "setattr(api, 'fake', replacement)\n"
        "api.fake = replacement\n"
    )
    assert refs == calls == patches == []


def test_local_bindings_in_function_still_resolve_above_wildcard_module():
    refs, calls, _ = analyse(
        "from other import *\n"
        "def run():\n    import comfy.lora as api\n    api.calculate_weight(1)\n"
    )
    assert refs == calls == ["comfy.lora.calculate_weight"]


@pytest.mark.parametrize("declaration", ["global api", "nonlocal api"])
def test_read_only_external_declarations_resolve_without_hiding_unrelated_calls(declaration):
    refs, calls, _ = analyse(
        "import comfy.lora as api\n"
        "def outer():\n    import comfy.utils as api\n"
        "    def run():\n        " + declaration + "\n        api.real(1)\n"
        "api.real(2)\n"
    )
    expected = "comfy.lora.real" if declaration.startswith("global") else "comfy.utils.real"
    assert sorted(refs) == sorted(calls) == sorted([expected, "comfy.lora.real"])


def test_nonlocal_write_invalidates_nearest_function_not_unrelated_module_alias():
    refs, calls, _ = analyse(
        "import comfy.lora as api\n"
        "def outer():\n    import comfy.utils as api\n"
        "    def change():\n        nonlocal api\n        api = object()\n"
        "    api.fake()\n"
        "api.calculate_weight(1)\n"
    )
    assert refs == calls == ["comfy.lora.calculate_weight"]


def test_global_write_does_not_invalidate_unrelated_enclosing_local():
    refs, calls, _ = analyse(
        "import comfy.lora as api\n"
        "def outer():\n    import comfy.utils as api\n"
        "    def change():\n        global api\n        api = object()\n"
        "    api.ProgressBar(1)\n"
        "api.fake()\n"
    )
    assert refs == calls == ["comfy.utils.ProgressBar"]


@pytest.mark.parametrize("scope", ["module", "class"])
def test_annotation_without_value_does_not_rebind_module_or_class_name(scope):
    source = "import comfy.lora as api\n"
    body = "api: object\napi.calculate_weight(1)\n"
    if scope == "class":
        body = "class Node:\n" + "".join("    " + line + "\n" for line in body.splitlines())
    refs, calls, _ = analyse(source + body)
    assert refs == calls == ["comfy.lora.calculate_weight"]


def test_annotation_without_value_still_shadows_enclosing_function_alias():
    refs, calls, _ = analyse(
        "import comfy.lora as api\n"
        "def run():\n    api: object\n    api.fake()\n"
    )
    assert refs == calls == []


def test_annotated_saved_alias_is_callable_and_scoped():
    _, calls, _ = analyse(
        "import comfy.lora as api\n"
        "def run():\n    saved: object = api.calculate_weight\n    saved(1)\n"
        "saved(2)\n"
    )
    assert calls == ["comfy.lora.calculate_weight"]


def test_future_annotations_are_not_eager_calls_but_defaults_still_are():
    refs, calls, _ = analyse(
        "from __future__ import annotations\n"
        "import comfy.lora as api\n"
        "def run(x: api.fake(1) = api.calculate_weight(1)) -> api.fake(2): pass\n"
        "value: api.fake(3)\n"
    )
    assert calls == ["comfy.lora.calculate_weight"]
    assert "comfy.lora.fake" not in refs


def test_eager_annotations_are_still_checked():
    _, calls, _ = analyse(
        "import comfy.lora as api\n"
        "def run(x: api.annotation(1)) -> api.annotation(2): pass\n"
    )
    assert calls == ["comfy.lora.annotation", "comfy.lora.annotation"]


def test_function_local_variable_annotations_are_not_evaluated():
    refs, calls, _ = analyse(
        "import comfy.lora as api\n"
        "def run():\n    value: api.fake(1)\n    saved: api.fake(2) = api.calculate_weight\n    saved(1)\n"
    )
    assert calls == ["comfy.lora.calculate_weight"]
    assert "comfy.lora.fake" not in refs


@pytest.mark.skipif(sys.version_info < (3, 12), reason="generic bounds require Python 3.12+")
@pytest.mark.parametrize("definition", ["def run[T: api.fake(1)](): pass", "class Node[T: api.fake(1)]: pass"])
def test_generic_bounds_are_lazy_not_eager_calls(definition):
    refs, calls, _ = analyse(
        "import comfy.lora as api\n" + definition + "\napi.calculate_weight(1)\n"
    )
    assert refs == calls == ["comfy.lora.calculate_weight"]


def test_async_function_bindings_do_not_leak():
    refs, calls, _ = analyse(
        "import comfy.lora as api\n"
        "async def shadowed(api):\n    api.fake()\n"
        "async def real():\n    api.calculate_weight(1)\n"
    )
    assert refs == calls == ["comfy.lora.calculate_weight"]
