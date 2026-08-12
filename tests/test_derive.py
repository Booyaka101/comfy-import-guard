"""derive-requires against real ComfyUI release history."""

import os

import pytest

from comfy_import_guard.derive import derive_requires, hard_references
from comfy_import_guard.extract import scan_pack
from comfy_import_guard.version import parse_tag, sort_tags, tag_to_version


def test_ancient_pack_gets_an_old_floor(repo, packs_dir):
    rep = derive_requires(repo, os.path.join(packs_dir, "ancient_pack"))
    assert rep["line"] is not None
    assert rep["ceiling_tag"] is None
    assert parse_tag(rep["floor_tag"]) < (0, 4, 0), rep["floor_tag"]
    assert rep["line"] == 'requires-comfyui = ">=%s"' % tag_to_version(rep["floor_tag"])


def test_recent_pack_gets_a_recent_floor(repo, packs_dir):
    rep = derive_requires(repo, os.path.join(packs_dir, "recent_pack"))
    # comfy.ldm.minimax landed with MiniMax-H3 support, released in v0.30.0.
    assert rep["floor_tag"] == "v0.30.0"
    assert rep["line"] == 'requires-comfyui = ">=0.30.0"'
    assert "comfy.ldm.minimax.model" in " ".join(rep["determined_by"])


def test_recent_floor_is_strictly_newer_than_ancient(repo, packs_dir):
    old = derive_requires(repo, os.path.join(packs_dir, "ancient_pack"))
    new = derive_requires(repo, os.path.join(packs_dir, "recent_pack"))
    assert parse_tag(new["floor_tag"]) > parse_tag(old["floor_tag"])


def test_removed_symbol_produces_an_upper_bound(repo, tmp_path):
    pack = tmp_path / "teacache_like"
    pack.mkdir()
    (pack / "nodes.py").write_text(
        "from comfy.ldm.lightricks.model import precompute_freqs_cis\n"
        "from comfy.utils import ProgressBar\n",
        encoding="utf-8",
    )
    rep = derive_requires(repo, str(pack))
    assert rep["ceiling_tag"] == "v0.8.0"
    assert rep["line"].endswith(',<0.8.0"')
    assert rep["broken_at_head"][0]["dotted"] == (
        "comfy.ldm.lightricks.model.precompute_freqs_cis")


def test_floor_tag_actually_satisfies_the_pack(repo, packs_dir):
    """The emitted floor must resolve; the tag one below it must not."""
    from comfy_import_guard.resolve import Resolver

    rep = derive_requires(repo, os.path.join(packs_dir, "recent_pack"))
    refs = hard_references(scan_pack(os.path.join(packs_dir, "recent_pack")))
    tags = sort_tags(repo.all_tags())
    idx = tags.index(rep["floor_tag"])

    at_floor = Resolver(repo, tags[idx])
    assert not [r for r in refs if at_floor.resolve(r).breaking]
    below = Resolver(repo, tags[idx - 1])
    assert [r for r in refs if below.resolve(r).breaking]


def test_pack_with_no_comfy_references(repo, tmp_path):
    pack = tmp_path / "plain"
    pack.mkdir()
    (pack / "nodes.py").write_text("import torch\nNODE_CLASS_MAPPINGS = {}\n", encoding="utf-8")
    rep = derive_requires(repo, str(pack))
    assert rep["line"] is None
    assert "does not need" in rep["note"]


def test_pack_with_no_python_files(repo, tmp_path):
    pack = tmp_path / "docs_only"
    pack.mkdir()
    (pack / "README.md").write_text("nothing here", encoding="utf-8")
    rep = derive_requires(repo, str(pack))
    assert rep["line"] is None and rep["python_files"] == 0


def test_vendored_pack_gets_no_floor(repo, tmp_path):
    pack = tmp_path / "vendored"
    (pack / "comfy").mkdir(parents=True)
    (pack / "comfy" / "__init__.py").write_text("", encoding="utf-8")
    (pack / "nodes.py").write_text("from comfy.utils import X\n", encoding="utf-8")
    rep = derive_requires(repo, str(pack))
    assert rep["vendored_comfy"] is True and rep["line"] is None


def test_guarded_imports_do_not_raise_the_floor(repo, tmp_path):
    pack = tmp_path / "guarded"
    pack.mkdir()
    (pack / "nodes.py").write_text(
        "from comfy.utils import ProgressBar\n"
        "try:\n"
        "    import comfy.ldm.minimax.model as mm\n"
        "except ImportError:\n"
        "    mm = None\n",
        encoding="utf-8",
    )
    rep = derive_requires(repo, str(pack))
    assert parse_tag(rep["floor_tag"]) < (0, 30, 0), rep["floor_tag"]


def test_missing_pack_directory_is_a_clean_error(repo, tmp_path):
    from comfy_import_guard.errors import BadInputError
    with pytest.raises((BadInputError, OSError)):
        derive_requires(repo, str(tmp_path / "does-not-exist"))


# --------------------------------------------------------------- tag sorting

def test_tag_sorting_is_numeric_not_lexical():
    tags = ["v0.9.0", "v0.31.0", "latest", "v0.8.0", "v0.10.0", "not-a-tag"]
    assert sort_tags(tags) == ["v0.8.0", "v0.9.0", "v0.10.0", "v0.31.0"]


def test_tag_to_version_strips_v():
    assert tag_to_version("v0.19.3") == "0.19.3"
