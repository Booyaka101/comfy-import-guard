"""Assertions against real, dated ComfyUI history.

Sources:
  Comfy-Org/ComfyUI#11660  - precompute_freqs_cis, broke TeaCache and MagCache
  T8mars/comfyui-minimax-h3-blockcache-T8#1 - time_shift_slope, PR #15243
"""

import pytest

from comfy_import_guard.blame import blame_symbol
from comfy_import_guard.errors import BadInputError, GitError
from comfy_import_guard.extract import scan_pack
from comfy_import_guard.ledger import Ledger
from comfy_import_guard.report import SAFE, WILL_BREAK, check
from comfy_import_guard.resolve import MISSING, PRESENT, Resolver, exported_names

LIGHTRICKS = "comfy/ldm/lightricks/model.py"
LTXV2_COMMIT = "f2b002372b71cf0671a4cf1fa539e1c386d727e4"   # PR #11632
MINIMAX_FIX = "bdcb886a4705a03cf40f4a7226de9fc7c059fc90"    # PR #15243


# --------------------------------------------------------------- resolution

def test_precompute_freqs_cis_present_before_ltxv2(repo):
    r = Resolver(repo, LTXV2_COMMIT + "^")
    assert "precompute_freqs_cis" in r.exports("comfy.ldm.lightricks.model")


def test_precompute_freqs_cis_gone_after_ltxv2(repo):
    r = Resolver(repo, LTXV2_COMMIT)
    assert "precompute_freqs_cis" not in r.exports("comfy.ldm.lightricks.model")


def test_the_substring_trap(repo):
    """A text search still finds the name at master. AST resolution must not."""
    src = repo.read_file("origin/master", LIGHTRICKS)
    assert src is not None
    assert src.count("precompute_freqs_cis") >= 2, "upstream still has the private method"
    assert "precompute_freqs_cis" not in exported_names(src)
    assert "_precompute_freqs_cis" not in exported_names(src), "it is a class method, not top level"


def test_time_shift_slope_lifecycle(repo):
    assert "time_shift_slope" in Resolver(repo, MINIMAX_FIX + "^").exports(
        "comfy.ldm.minimax.model")
    assert "time_shift_slope" not in Resolver(repo, MINIMAX_FIX).exports(
        "comfy.ldm.minimax.model")


def test_a_symbol_that_survived_is_present(repo):
    r = Resolver(repo, "origin/master")
    assert "ProgressBar" in r.exports("comfy.utils")


def test_comfy_namespace_package_resolves(repo):
    """comfy/ has no __init__.py; submodule references must still resolve."""
    r = Resolver(repo, "origin/master")
    assert r.module_exists("comfy")
    assert r.module_exists("comfy.model_sampling")


# -------------------------------------------------------------------- blame

def test_blame_time_shift_slope(repo):
    rep = blame_symbol(repo, "comfy.ldm.minimax.model.time_shift_slope", use_ledger=False)
    assert rep["removed_in_commit"] == MINIMAX_FIX
    assert rep["pr"] == 15243
    assert rep["subject"].startswith("Fix sampler issues for audio with minimax")
    assert rep["removed_on"].startswith("2026-08-06")
    assert rep["last_good_tag"] == "v0.30.2"
    assert rep["first_bad_tag"] == "v0.31.0"
    assert rep["present_at_head"] is False


def test_blame_precompute_freqs_cis(repo):
    rep = blame_symbol(repo, "comfy.ldm.lightricks.model.precompute_freqs_cis",
                       use_ledger=False)
    assert rep["removed_in_commit"] == LTXV2_COMMIT
    assert rep["pr"] == 11632
    assert rep["subject"] == "Support the LTXV 2 model. (#11632)"
    assert rep["last_good_tag"] == "v0.7.0"
    assert rep["first_bad_tag"] == "v0.8.0"


def test_blame_reports_a_living_symbol_as_present(repo):
    rep = blame_symbol(repo, "comfy.utils.ProgressBar", use_ledger=False)
    assert rep["present_at_head"] is True
    assert rep["removed_in_commit"] is None


def test_blame_rejects_a_non_comfy_path(repo):
    with pytest.raises(BadInputError):
        blame_symbol(repo, "torch.nn.Linear", use_ledger=False)


def test_blame_rejects_an_invented_module(repo):
    with pytest.raises(BadInputError):
        blame_symbol(repo, "comfy.not_a_real_module_xyz.thing", use_ledger=False)


# ------------------------------------------------------- ledger short circuit

class _NoGitRepo:
    """Any git call is a test failure."""

    path = "/nonexistent"

    def _git(self, *a, **k):
        raise AssertionError("ledger short-circuit still shelled out to git")

    def __getattr__(self, name):
        raise AssertionError("ledger short-circuit touched repo.%s" % name)


def test_ledger_answers_without_git():
    ledger = Ledger()
    rep = blame_symbol(_NoGitRepo(), "comfy.ldm.minimax.model.time_shift_slope", ledger)
    assert rep["source"] == "ledger"
    assert rep["removed_in_commit"] == MINIMAX_FIX
    assert rep["pr"] == 15243
    assert rep["last_good_tag"] == "v0.30.2"
    assert rep["first_bad_tag"] == "v0.31.0"


def test_ledger_and_git_agree(repo):
    ledger = Ledger()
    for dotted in ("comfy.ldm.minimax.model.time_shift_slope",
                   "comfy.ldm.lightricks.model.precompute_freqs_cis"):
        cached = blame_symbol(_NoGitRepo(), dotted, ledger)
        live = blame_symbol(repo, dotted, use_ledger=False)
        for key in ("removed_in_commit", "pr", "last_good_tag", "first_bad_tag"):
            assert cached[key] == live[key], (dotted, key)


def test_shipped_ledger_holds_the_two_real_removals():
    ledger = Ledger()
    assert len(ledger.entries) >= 2
    for e in ledger.entries:
        assert len(e["removed_in_commit"]) == 40, "must be a real full sha"
        assert e["first_bad_tag"].startswith("v")


# -------------------------------------------------------------------- check

TEACACHE_LIKE = (
    "from comfy.ldm.lightricks.model import precompute_freqs_cis\n"
    "from comfy.utils import ProgressBar\n"
)


def _install(tmp_path, name, source):
    pack = tmp_path / "custom_nodes" / name
    pack.mkdir(parents=True)
    (pack / "nodes.py").write_text(source, encoding="utf-8")
    return tmp_path


def test_check_predicts_the_lightricks_break_at_master(repo, tmp_path):
    root = _install(tmp_path, "ComfyUI-TeaCache", TEACACHE_LIKE)
    rep = check(repo, str(root), "origin/master", Ledger())
    pack = rep["packs"][0]
    assert pack["verdict"] == WILL_BREAK
    assert len(pack["breaking"]) == 1
    row = pack["breaking"][0]
    assert row["dotted"] == "comfy.ldm.lightricks.model.precompute_freqs_cis"
    assert row["file"] == "nodes.py" and row["line"] == 1
    assert row["attribution"]["pr"] == 11632
    assert rep["totals"]["will_break"] == 1


def test_check_is_safe_at_the_ref_before_the_removal(repo, tmp_path):
    root = _install(tmp_path, "ComfyUI-TeaCache", TEACACHE_LIKE)
    rep = check(repo, str(root), LTXV2_COMMIT + "^", Ledger())
    assert rep["packs"][0]["verdict"] == SAFE
    assert rep["packs"][0]["breaking"] == []


def test_check_flips_exactly_at_the_removal_commit(repo, tmp_path):
    root = _install(tmp_path, "ComfyUI-TeaCache", TEACACHE_LIKE)
    before = check(repo, str(root), LTXV2_COMMIT + "^", Ledger())
    after = check(repo, str(root), LTXV2_COMMIT, Ledger())
    assert before["packs"][0]["verdict"] == SAFE
    assert after["packs"][0]["verdict"] == WILL_BREAK


def test_check_release_tag_boundary(repo, tmp_path):
    root = _install(tmp_path, "ComfyUI-MagCache", TEACACHE_LIKE)
    assert check(repo, str(root), "v0.7.0", Ledger())["packs"][0]["verdict"] == SAFE
    assert check(repo, str(root), "v0.8.0", Ledger())["packs"][0]["verdict"] == WILL_BREAK


def test_check_marks_a_guarded_import_soft_not_breaking(repo, tmp_path):
    guarded = (
        "try:\n"
        "    from comfy.ldm.minimax.model import time_shift_slope\n"
        "except ImportError:\n"
        "    time_shift_slope = None\n"
    )
    root = _install(tmp_path, "guarded-pack", guarded)
    pack = check(repo, str(root), "origin/master", Ledger())["packs"][0]
    assert pack["verdict"] == "WARN"
    assert pack["breaking"] == []
    assert len(pack["soft"]) == 1


def test_check_ignores_loose_files_and_pycache(repo, tmp_path):
    root = _install(tmp_path, "real-pack", "from comfy.utils import ProgressBar\n")
    cn = root / "custom_nodes"
    (cn / "websocket_image_save.py").write_text("from comfy.utils import Gone\n")
    (cn / "example_node.py.example").write_text("whatever")
    (cn / "__pycache__").mkdir()
    (cn / "__pycache__" / "x.py").write_text("from comfy.utils import AlsoGone\n")
    rep = check(repo, str(root), "origin/master", Ledger())
    assert [p["pack"] for p in rep["packs"]] == ["real-pack"]


def test_check_on_a_missing_directory_is_a_clean_error(repo, tmp_path):
    with pytest.raises(BadInputError):
        check(repo, str(tmp_path / "nope"), "origin/master", Ledger())


def test_check_on_a_directory_without_custom_nodes(repo, tmp_path):
    with pytest.raises(BadInputError):
        check(repo, str(tmp_path), "origin/master", Ledger())


def test_check_on_an_empty_custom_nodes(repo, tmp_path):
    (tmp_path / "custom_nodes").mkdir()
    rep = check(repo, str(tmp_path), "origin/master", Ledger())
    assert rep["packs"] == [] and rep["totals"]["packs"] == 0


def test_check_module_removed_entirely(repo, tmp_path):
    """A target ref that predates a module means every symbol in it is missing."""
    root = _install(tmp_path, "minimax-pack",
                    "from comfy.ldm.minimax.model import MiniMaxH3Model\n")
    pack = check(repo, str(root), "v0.3.0", Ledger())["packs"][0]
    assert pack["verdict"] == WILL_BREAK
    assert pack["breaking"][0]["status"] == "MODULE_MISSING"


def test_check_star_import_is_unresolvable_not_breaking(repo, tmp_path):
    root = _install(tmp_path, "star-pack", "from comfy.utils import *\n")
    pack = check(repo, str(root), "origin/master", Ledger())["packs"][0]
    assert pack["verdict"] == "WARN"
    assert len(pack["unresolvable"]) == 1


def test_check_vendored_pack_is_skipped(repo, tmp_path):
    root = _install(tmp_path, "vendor-pack", "from comfy.utils import Whatever\n")
    vend = root / "custom_nodes" / "vendor-pack" / "comfy"
    vend.mkdir()
    (vend / "__init__.py").write_text("")
    pack = check(repo, str(root), "origin/master", Ledger())["packs"][0]
    assert pack["verdict"] == "SKIPPED"


def test_bad_ref_gives_a_clear_error(repo, tmp_path):
    root = _install(tmp_path, "p", "from comfy.utils import ProgressBar\n")
    with pytest.raises((GitError, Exception)) as exc:
        check(repo, str(root), "definitely-not-a-ref-zzz", Ledger())
    assert "definitely-not-a-ref-zzz" in str(exc.value)


def test_scan_of_a_real_pack_layout(tmp_path):
    """Nested modules and __init__.py are both walked."""
    pack = tmp_path / "p"
    (pack / "inner").mkdir(parents=True)
    (pack / "__init__.py").write_text("from .inner.n import NODE_CLASS_MAPPINGS\n")
    (pack / "inner" / "n.py").write_text(
        "from comfy.ldm.lightricks.model import precompute_freqs_cis\n")
    scan = scan_pack(str(pack))
    assert scan.python_files == 2
    assert scan.references[0].file == "inner/n.py"
