"""Signature checks: call binding, monkeypatch drift, and their attribution.

The live fixtures reconstruct the two 2026 issue shapes:
  Comfy-Org/ComfyUI#5355  - calculate_weight() got an unexpected keyword
                            argument 'intermediate_dtype': a pack shipping an
                            outdated replacement of a core function
  Comfy-Org/ComfyUI#13136 / #12134 - the same class of TypeError on
                            WanAttentionBlock.forward / context_img_len
"""

import ast

import pytest

from comfy_import_guard.blame import blame_signature
from comfy_import_guard.extract import extract_calls, scan_pack
from comfy_import_guard.ledger import Ledger
from comfy_import_guard.report import SAFE, WARN, WILL_BREAK, check
from comfy_import_guard.signature import (
    FOUND,
    PARSE_FAILED,
    UNRESOLVED,
    ParamSpec,
    bind_call,
    replacement_drops,
    spec_from_source,
)

CALC_WEIGHT_MOVED_IN = "c26ca272076262c8b21a8f2e094cf538d88b9e46"   # comfy.lora created
CONTEXT_IMG_LEN_ADDED_IN = "0d720e4367c1c149dbfa0a98ebd81c7776914545"


def spec(source, qualname="f"):
    s = spec_from_source(source, qualname)
    assert s is not None, "expected a spec for %s" % qualname
    return s


# ------------------------------------------------------------ ParamSpec


def test_spec_of_a_plain_function():
    s = spec("def f(a, b, c=1, *args, d, e=2, **kw): pass")
    assert s.args == ("a", "b", "c")
    assert s.defaults == 1
    assert s.kwonly == ("d", "e")
    assert s.kwonly_required == ("d",)
    assert s.vararg and s.kwarg


def test_spec_of_positional_only():
    s = spec("def f(a, b, /, c): pass")
    assert s.posonly == ("a", "b")
    assert s.args == ("c",)


def test_spec_of_a_class_binds_init_without_self():
    s = spec("class C:\n    def __init__(self, total, bar=None): pass", "C")
    assert s.args == ("total", "bar")
    assert s.defaults == 1


def test_spec_of_a_method_keeps_self():
    s = spec("class C:\n    def forward(self, x, y=1): pass", "C.forward")
    assert s.args == ("self", "x", "y")


def test_spec_of_a_staticmethod_keeps_all_params():
    s = spec("class C:\n    @staticmethod\n    def f(x, y): pass", "C.f")
    assert s.args == ("x", "y")


def test_spec_of_a_classmethod_drops_cls():
    s = spec("class C:\n    @classmethod\n    def f(cls, x): pass", "C.f")
    assert s.args == ("x",)


def test_decorated_function_is_unresolved():
    assert spec_from_source("import functools\n@functools.cache\ndef f(a): pass", "f") is None


def test_class_without_own_init_is_unresolved():
    assert spec_from_source("class C:\n    pass", "C") is None


def test_render_marks_defaults_and_stars():
    s = spec("def f(a, b=1, *args, c, **kw): pass")
    assert s.render() == "(a, b=..., *args, c, **kwargs)"


# ------------------------------------------------------------- binding


CALC = spec("def calculate_weight(patches, weight, key, intermediate_dtype=1, original_weights=None): pass",
            "calculate_weight")


def test_matching_call_binds():
    assert bind_call(CALC, 3, ()) == []
    assert bind_call(CALC, 3, ("intermediate_dtype",)) == []
    assert bind_call(CALC, 5, ()) == []


def test_unknown_keyword_is_a_problem():
    problems = bind_call(CALC, 3, ("wrong_kw",))
    assert problems == [("wrong_kw", "unexpected keyword argument 'wrong_kw'")]


def test_too_many_positionals_is_a_problem():
    problems = bind_call(CALC, 6, ())
    assert problems == [(None, "takes 5 positional argument(s) but 6 given")]


def test_missing_required_is_a_problem():
    problems = bind_call(CALC, 2, ())
    assert problems == [("key", "missing required argument 'key'")]


def test_duplicate_positional_and_keyword():
    problems = bind_call(CALC, 3, ("key",))
    assert problems == [("key", "got multiple values for argument 'key'")]


def test_star_spread_silences_the_call():
    assert bind_call(CALC, 0, (), star_args=True) is None
    assert bind_call(CALC, 0, (), star_kwargs=True) is None


def test_kwargs_upstream_accepts_any_keyword():
    s = spec("def f(a, **kw): pass")
    assert bind_call(s, 1, ("anything",)) == []


def test_vararg_upstream_accepts_extra_positionals():
    s = spec("def f(a, *args): pass")
    assert bind_call(s, 9, ()) == []


def test_keyword_for_positional_only_param():
    s = spec("def f(a, /, b): pass")
    assert bind_call(s, 0, ("a", "b")) == [
        ("a", "parameter 'a' is positional-only"),
        ("a", "missing required argument 'a'"),
    ]


# ------------------------------------------------- replacement comparison


def test_replacement_dropping_new_params_is_drift():
    repl = spec("def my_calculate_weight(patches, weight, key): pass", "my_calculate_weight")
    assert replacement_drops(CALC, repl) == ["intermediate_dtype", "original_weights"]


def test_replacement_matching_upstream_is_clean():
    repl = spec("def mine(patches, weight, key, intermediate_dtype=2, original_weights=None): pass", "mine")
    assert replacement_drops(CALC, repl) == []


def test_replacement_with_kwargs_is_silent():
    repl = spec("def mine(patches, weight, key, **kw): pass", "mine")
    assert replacement_drops(CALC, repl) is None


def test_upstream_with_kwargs_is_silent():
    up = spec("def f(a, **kw): pass")
    repl = spec("def g(a): pass", "g")
    assert replacement_drops(up, repl) is None


# ------------------------------------------------------------ extraction


def calls_of(source):
    return extract_calls(ast.parse(source), "x.py")


def test_attribute_call_site_is_collected():
    calls, _ = calls_of("import comfy.lora\ncomfy.lora.calculate_weight(1, 2, k=3)\n")
    assert len(calls) == 1
    c = calls[0]
    assert c.dotted == "comfy.lora.calculate_weight"
    assert c.nargs == 2 and c.keywords == ("k",)
    assert not c.star_args and not c.star_kwargs


def test_from_import_call_site_is_collected():
    calls, _ = calls_of("from comfy.lora import calculate_weight\ncalculate_weight(1)\n")
    assert calls[0].dotted == "comfy.lora.calculate_weight"


def test_local_alias_call_site_is_collected():
    calls, _ = calls_of(
        "import comfy.lora\n"
        "orig = comfy.lora.calculate_weight\n"
        "orig(1, 2, 3)\n"
    )
    assert any(c.dotted == "comfy.lora.calculate_weight" and c.nargs == 3 for c in calls)


def test_star_spread_is_recorded():
    calls, _ = calls_of("import comfy.lora\ncomfy.lora.calculate_weight(*a, **kw)\n")
    assert calls[0].star_args and calls[0].star_kwargs and calls[0].nargs == 0


def test_call_in_try_except_is_soft():
    calls, _ = calls_of(
        "import comfy.lora\n"
        "try:\n"
        "    comfy.lora.calculate_weight(1)\n"
        "except Exception:\n"
        "    pass\n"
    )
    assert calls[0].soft is True


def test_non_comfy_calls_are_ignored():
    calls, patches = calls_of("import torch\ntorch.zeros(3)\nlocal_fn(1)\n")
    assert calls == [] and patches == []


def test_monkeypatch_assign_records_replacement_params():
    _, patches = calls_of(
        "import comfy.lora\n"
        "def mine(patches, weight, key): pass\n"
        "comfy.lora.calculate_weight = mine\n"
    )
    assert len(patches) == 1
    p = patches[0]
    assert p.dotted == "comfy.lora.calculate_weight"
    assert p.replacement.args == ("patches", "weight", "key")


def test_monkeypatch_lambda_records_replacement_params():
    _, patches = calls_of("import comfy.lora\ncomfy.lora.calculate_weight = lambda p, w, k: None\n")
    assert patches[0].replacement.args == ("p", "w", "k")


def test_monkeypatch_setattr_literal_name():
    _, patches = calls_of(
        "import comfy.lora\n"
        "def mine(p, w, k): pass\n"
        "setattr(comfy.lora, 'calculate_weight', mine)\n"
    )
    assert patches[0].dotted == "comfy.lora.calculate_weight"


def test_monkeypatch_setattr_variable_name_is_silent():
    _, patches = calls_of(
        "import comfy.lora\n"
        "def mine(p): pass\n"
        "name = 'calculate_weight'\n"
        "setattr(comfy.lora, name, mine)\n"
    )
    assert patches == []


def test_monkeypatch_with_decorated_replacement_is_silent():
    _, patches = calls_of(
        "import functools\nimport comfy.lora\n"
        "@functools.wraps(object)\n"
        "def mine(p): pass\n"
        "comfy.lora.calculate_weight = mine\n"
    )
    assert patches == []


def test_monkeypatch_with_partial_is_silent():
    _, patches = calls_of(
        "import functools\nimport comfy.lora\n"
        "def mine(p, w, k, dtype): pass\n"
        "comfy.lora.calculate_weight = functools.partial(mine, dtype=1)\n"
    )
    assert patches == []


def test_monkeypatch_in_try_except_is_soft():
    _, patches = calls_of(
        "import comfy.lora\n"
        "def mine(p, w, k): pass\n"
        "try:\n"
        "    comfy.lora.calculate_weight = mine\n"
        "except AttributeError:\n"
        "    pass\n"
    )
    assert patches[0].soft is True


def test_shadowed_name_is_not_a_call_site():
    """A rebound name is not that callable any more.

    Presence checks can be loose here and grade the result WARN. A call site
    cannot: a bad bind is a hard WILL BREAK, so shadowing must silence it.
    """
    shadowing = {
        "rebound": "calculate_weight = my_impl\ncalculate_weight(1)\n",
        "local def": "def calculate_weight(x):\n    return x\ncalculate_weight(1)\n",
        "parameter": "def outer(calculate_weight):\n    return calculate_weight(1)\n",
        "for target": "for calculate_weight in xs:\n    calculate_weight(1)\n",
        "with as": "with open(p) as calculate_weight:\n    calculate_weight(1)\n",
        "except as": "try:\n    pass\nexcept E as calculate_weight:\n    calculate_weight(1)\n",
        "walrus": "if (calculate_weight := f()):\n    calculate_weight(1)\n",
        "later import": "from mypack import calculate_weight\ncalculate_weight(1)\n",
    }
    for label, tail in shadowing.items():
        calls, _ = calls_of("from comfy.lora import calculate_weight\n" + tail)
        assert calls == [], label


def test_shadowed_root_alias_is_not_a_call_site():
    calls, _ = calls_of(
        "import comfy.lora\n"
        "def f(comfy):\n"
        "    return comfy.lora.calculate_weight(1, 2, 3)\n"
    )
    assert calls == []


def test_saving_the_original_before_patching_still_resolves():
    """The save-then-patch idiom must survive the shadowing guard."""
    calls, patches = calls_of(
        "import comfy.samplers\n"
        "def mine(a):\n    return a\n"
        "orig = comfy.samplers.sample\n"
        "comfy.samplers.sample = mine\n"
        "orig(1)\n"
    )
    assert [c.dotted for c in calls] == ["comfy.samplers.sample"]
    assert len(patches) == 1


def test_monkeypatch_of_imported_function_is_silent():
    _, patches = calls_of(
        "import comfy.lora\n"
        "from elsewhere import mine\n"
        "comfy.lora.calculate_weight = mine\n"
    )
    assert patches == []


# ------------------------------------------------- target module parse failure


class _StubRepo:
    """Just enough repo for a check() run over canned file contents."""

    def __init__(self, files):
        self.files = files

    def resolve_ref(self, ref):
        return "f" * 40

    def read_file(self, ref, path):
        return self.files.get(path)

    def is_dir(self, ref, path):
        return any(k.startswith(path + "/") for k in self.files)


def test_unparseable_target_module_is_counted_not_skipped(tmp_path):
    pack = tmp_path / "custom_nodes" / "p"
    pack.mkdir(parents=True)
    (pack / "nodes.py").write_text(
        "import comfy.lora\ncomfy.lora.calculate_weight(1, 2, 3)\n", encoding="utf-8")
    repo = _StubRepo({"comfy/lora.py": "def calculate_weight(a, b broken syntax"})
    rep = check(repo, str(tmp_path), "whatever", Ledger(tmp_path / "none.json"))
    rows = [r for r in rep["packs"][0]["unresolvable"] if r["status"] == "TARGET_UNPARSED"]
    assert len(rows) == 1
    assert rows[0]["module"] == "comfy.lora"


# ------------------------------------------------------------- live fixtures


TEACACHE_5355 = (
    "import comfy.lora\n"
    "\n"
    "def my_calculate_weight(patches, weight, key):\n"
    "    return None\n"
    "\n"
    "comfy.lora.calculate_weight = my_calculate_weight\n"
)

KJNODES_12134 = (
    "import comfy.ldm.wan.model\n"
    "\n"
    "def patched_forward_orig(self, x, e, freqs, context):\n"
    "    return None\n"
    "\n"
    "comfy.ldm.wan.model.WanAttentionBlock.forward = patched_forward_orig\n"
)


def _install(tmp_path, name, source):
    pack = tmp_path / "custom_nodes" / name
    pack.mkdir(parents=True)
    (pack / "nodes.py").write_text(source, encoding="utf-8")
    return tmp_path


def test_issue_5355_monkeypatch_is_signature_drift(repo, tmp_path):
    """The calculate_weight/intermediate_dtype shape from ComfyUI#5355."""
    root = _install(tmp_path, "outdated-lora-pack", TEACACHE_5355)
    pack = check(repo, str(root), "origin/master", Ledger())["packs"][0]
    assert pack["verdict"] == WARN
    assert pack["breaking"] == []
    assert len(pack["signature_drift"]) == 1
    row = pack["signature_drift"][0]
    assert row["dotted"] == "comfy.lora.calculate_weight"
    assert row["file"] == "nodes.py" and row["line"] == 6
    assert row["missing"] == ["intermediate_dtype", "original_weights"]
    assert row["attribution"]["changed_in_commit"] == CALC_WEIGHT_MOVED_IN
    assert row["attribution"]["direction"] == "added"


def test_issue_12134_monkeypatch_is_signature_drift(repo, tmp_path):
    """The WanAttentionBlock.forward/context_img_len shape from ComfyUI#12134/#13136."""
    root = _install(tmp_path, "kjnodes-like", KJNODES_12134)
    pack = check(repo, str(root), "origin/master", Ledger())["packs"][0]
    assert pack["verdict"] == WARN
    assert len(pack["signature_drift"]) == 1
    row = pack["signature_drift"][0]
    assert row["dotted"] == "comfy.ldm.wan.model.WanAttentionBlock.forward"
    assert row["file"] == "nodes.py" and row["line"] == 6
    assert "context_img_len" in row["missing"]
    att = row["attribution"]
    assert att["changed_in_commit"] == CONTEXT_IMG_LEN_ADDED_IN
    assert att["last_good_tag"] == "v0.3.28"
    assert att["first_bad_tag"] == "v0.3.29"


def test_unknown_keyword_call_is_a_hard_break(repo, tmp_path):
    root = _install(tmp_path, "bad-caller",
                    "import comfy.lora\n"
                    "comfy.lora.calculate_weight(1, 2, 3, wrong_kw=True)\n")
    pack = check(repo, str(root), "origin/master", Ledger())["packs"][0]
    assert pack["verdict"] == WILL_BREAK
    row = pack["breaking"][0]
    assert row["status"] == "SIGNATURE"
    assert row["file"] == "nodes.py" and row["line"] == 2
    assert "wrong_kw" in row["detail"]
    assert "intermediate_dtype" in row["upstream_params"]


def test_too_many_positionals_is_a_hard_break(repo, tmp_path):
    root = _install(tmp_path, "over-caller",
                    "import comfy.lora\n"
                    "comfy.lora.calculate_weight(1, 2, 3, 4, 5, 6)\n")
    pack = check(repo, str(root), "origin/master", Ledger())["packs"][0]
    assert pack["verdict"] == WILL_BREAK
    assert "positional" in pack["breaking"][0]["detail"]


def test_valid_call_stays_safe(repo, tmp_path):
    root = _install(tmp_path, "good-caller",
                    "import comfy.lora\n"
                    "comfy.lora.calculate_weight(1, 2, 3)\n"
                    "comfy.lora.calculate_weight(1, 2, 3, intermediate_dtype=None)\n")
    pack = check(repo, str(root), "origin/master", Ledger())["packs"][0]
    assert pack["verdict"] == SAFE


def test_guarded_bad_call_is_soft_not_breaking(repo, tmp_path):
    root = _install(tmp_path, "guarded-caller",
                    "import comfy.lora\n"
                    "try:\n"
                    "    comfy.lora.calculate_weight(1, 2, 3, wrong_kw=True)\n"
                    "except Exception:\n"
                    "    pass\n")
    pack = check(repo, str(root), "origin/master", Ledger())["packs"][0]
    assert pack["verdict"] == WARN
    assert pack["breaking"] == []
    assert any(r.get("status") == "SIGNATURE" for r in pack["soft"])


def test_version_shim_is_not_a_hard_break(repo, tmp_path):
    """A hasattr-guarded arity shim must not fail the pack's CI.

    Reconstructed from comfyui-minimax-h3-blockcache-T8, which probes
    ComfyUI for the newer signature and calls the right arity per branch.
    Exactly one branch binds at any ref; the other is dead code there.
    """
    root = _install(tmp_path, "shim-pack",
                    "import comfy.lora\n"
                    "NEW = hasattr(comfy.lora, 'calculate_shape')\n"
                    "def go(p, w, k, dt):\n"
                    "    if NEW:\n"
                    "        return comfy.lora.calculate_weight(p, w, k, dt)\n"
                    "    return comfy.lora.calculate_weight(p, w, k, dt, None, 'extra')\n")
    pack = check(repo, str(root), "origin/master", Ledger())["packs"][0]
    assert pack["verdict"] == WARN
    assert pack["breaking"] == []
    shims = [r for r in pack["soft"] if r.get("status") == "SIGNATURE_SHIM"]
    assert len(shims) == 1
    assert "version shim" in shims[0]["detail"]


def test_lone_bad_call_is_still_a_hard_break(repo, tmp_path):
    """The shim rule must not swallow a target called only one way."""
    root = _install(tmp_path, "lone-caller",
                    "import comfy.lora\n"
                    "comfy.lora.calculate_weight(1, 2, 3, nope=1)\n")
    pack = check(repo, str(root), "origin/master", Ledger())["packs"][0]
    assert pack["verdict"] == WILL_BREAK


def test_except_typeerror_softens_a_call(repo, tmp_path):
    root = _install(tmp_path, "typeerror-pack",
                    "import comfy.lora\n"
                    "try:\n"
                    "    comfy.lora.calculate_weight(1, 2, 3, nope=1)\n"
                    "except TypeError:\n"
                    "    pass\n")
    pack = check(repo, str(root), "origin/master", Ledger())["packs"][0]
    assert pack["verdict"] == WARN
    assert pack["breaking"] == []


def test_except_typeerror_does_not_soften_an_import():
    """TypeError softens calls only; an import guarded by it is still hard."""
    from comfy_import_guard.extract import extract_references
    rs = extract_references(ast.parse("try:\n"
                                      "    from comfy.utils import Gone\n"
                                      "except TypeError:\n"
                                      "    pass\n"), "x.py")
    assert rs[0].soft is False


def test_no_signatures_flag_disables_the_pass(repo, tmp_path):
    root = _install(tmp_path, "outdated-lora-pack", TEACACHE_5355)
    rep = check(repo, str(root), "origin/master", Ledger(), signatures=False)
    pack = rep["packs"][0]
    assert pack["verdict"] == SAFE
    assert pack["signature_drift"] == []


def test_drift_vanishes_at_a_ref_before_the_parameter(repo, tmp_path):
    """At the commit before context_img_len existed there is nothing to drop."""
    root = _install(tmp_path, "kjnodes-like",
                    "import comfy.ldm.wan.model\n"
                    "def patched(self, x, e, seq_lens, grid_sizes, freqs, context, context_lens):\n"
                    "    return None\n"
                    "comfy.ldm.wan.model.WanAttentionBlock.forward = patched\n")
    before = check(repo, str(root), CONTEXT_IMG_LEN_ADDED_IN + "^", Ledger())["packs"][0]
    assert before["signature_drift"] == [] or "context_img_len" not in (
        before["signature_drift"][0].get("missing") or [])


def test_blame_signature_names_the_adding_commit(repo):
    rep = blame_signature(repo, "comfy.ldm.wan.model", "WanAttentionBlock.forward",
                          "context_img_len")
    assert rep["changed_in_commit"] == CONTEXT_IMG_LEN_ADDED_IN
    assert rep["direction"] == "added"
    assert rep["changed_on"].startswith("2025-04-17")
    assert rep["last_good_tag"] == "v0.3.28"
    assert rep["first_bad_tag"] == "v0.3.29"


def test_scan_pack_carries_calls_and_patches(tmp_path):
    pack = tmp_path / "p"
    pack.mkdir()
    (pack / "nodes.py").write_text(TEACACHE_5355, encoding="utf-8")
    scan = scan_pack(str(pack))
    assert len(scan.monkeypatches) == 1
    assert scan.monkeypatches[0].dotted == "comfy.lora.calculate_weight"


def test_cli_parses_no_signatures():
    from comfy_import_guard.cli import build_parser
    args = build_parser().parse_args(["check", "--comfy-dir", "x", "--no-signatures"])
    assert args.no_signatures is True


def test_cli_parses_blame_param():
    from comfy_import_guard.cli import build_parser
    args = build_parser().parse_args(
        ["blame", "comfy.lora.calculate_weight", "--param", "intermediate_dtype"])
    assert args.param == "intermediate_dtype"


def test_split_signature_target_keeps_the_class(repo):
    from comfy_import_guard.blame import split_signature_target
    module, qual = split_signature_target(
        repo, "comfy.ldm.wan.model.WanAttentionBlock.forward")
    assert module == "comfy.ldm.wan.model"
    assert qual == "WanAttentionBlock.forward"


def test_blame_param_end_to_end(repo, capsys):
    from comfy_import_guard.cli import main
    code = main(["blame", "comfy.ldm.wan.model.WanAttentionBlock.forward",
                 "--param", "context_img_len", "-q"])
    out = capsys.readouterr().out
    assert code == 0
    assert CONTEXT_IMG_LEN_ADDED_IN[:9] in out
    assert "v0.3.29" in out
