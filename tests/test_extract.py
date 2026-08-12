"""Extraction is pure AST work; nothing here touches git."""

import ast
import os

from comfy_import_guard.extract import (
    ATTR,
    FROM,
    GETATTR,
    MODULE,
    STAR,
    extract_references,
    scan_pack,
)


def refs(source, filename="x.py"):
    return extract_references(ast.parse(source), filename)


def dotted(rs):
    return {r.dotted for r in rs}


def test_import_from_is_found():
    rs = refs("from comfy.ldm.lightricks.model import precompute_freqs_cis, LTXVModel\n")
    assert dotted(rs) == {
        "comfy.ldm.lightricks.model.precompute_freqs_cis",
        "comfy.ldm.lightricks.model.LTXVModel",
    }
    assert all(r.kind == FROM and r.lineno == 1 for r in rs)


def test_aliased_attribute_chain_is_found():
    rs = refs(
        "import comfy.ldm.minimax.model as mm\n"
        "\n"
        "def go():\n"
        "    return mm.time_shift_slope(1, 2, 3)\n"
    )
    attrs = [r for r in rs if r.kind == ATTR]
    assert len(attrs) == 1
    assert attrs[0].module == "comfy.ldm.minimax.model"
    assert attrs[0].symbol == "time_shift_slope"
    assert attrs[0].lineno == 4
    assert any(r.kind == MODULE and r.module == "comfy.ldm.minimax.model" for r in rs)


def test_bare_import_then_attribute_chain():
    rs = refs("import comfy\nx = comfy.model_management.get_torch_device()\n")
    attrs = [r for r in rs if r.kind == ATTR]
    assert attrs[0].module == "comfy.model_management"
    assert attrs[0].symbol == "get_torch_device"


def test_from_comfy_import_submodule_then_attribute():
    rs = refs("from comfy import model_management\nd = model_management.get_torch_device()\n")
    assert "comfy.model_management" in dotted(rs)
    attrs = [r for r in rs if r.kind == ATTR]
    assert attrs[0].module == "comfy.model_management"
    assert attrs[0].symbol == "get_torch_device"


def test_only_outermost_chain_is_reported():
    rs = refs("import comfy\nx = comfy.ldm.modules.attention.optimized_attention\n")
    attrs = [r for r in rs if r.kind == ATTR]
    assert len(attrs) == 1
    assert attrs[0].dotted == "comfy.ldm.modules.attention.optimized_attention"


def test_non_comfy_imports_ignored():
    rs = refs("import torch\nfrom torch import nn\nimport comfyui_frontend as f\nx = nn.Linear\n")
    assert rs == []


def test_relative_import_ignored():
    rs = refs("from .comfy import thing\n")
    assert rs == []


def test_star_import_marked_unresolvable():
    rs = refs("from comfy.utils import *\n")
    assert len(rs) == 1
    assert rs[0].kind == STAR and rs[0].symbol == "*"


def test_try_except_importerror_marks_soft():
    rs = refs(
        "try:\n"
        "    from comfy.ldm.minimax.model import time_shift_slope\n"
        "except ImportError:\n"
        "    time_shift_slope = None\n"
    )
    assert len(rs) == 1
    assert rs[0].soft is True


def test_unguarded_import_is_not_soft():
    rs = refs("from comfy.ldm.minimax.model import time_shift_slope\n")
    assert rs[0].soft is False


def test_try_except_valueerror_is_not_soft():
    rs = refs(
        "try:\n"
        "    from comfy.utils import ProgressBar\n"
        "except ValueError:\n"
        "    pass\n"
    )
    assert rs[0].soft is False


def test_getattr_string_literal():
    rs = refs("import comfy\nf = getattr(comfy.utils, 'ProgressBar', None)\n")
    g = [r for r in rs if r.kind == GETATTR]
    assert len(g) == 1
    assert g[0].module == "comfy.utils" and g[0].symbol == "ProgressBar"


def test_getattr_with_variable_name_is_skipped():
    rs = refs("import comfy\nname = 'x'\nf = getattr(comfy.utils, name)\n")
    assert not [r for r in rs if r.kind == GETATTR]


def test_vendored_comfy_pack_is_skipped(tmp_path):
    pack = tmp_path / "vendored"
    (pack / "comfy").mkdir(parents=True)
    (pack / "comfy" / "__init__.py").write_text("")
    (pack / "nodes.py").write_text("from comfy.utils import Nope\n")
    scan = scan_pack(str(pack))
    assert scan.vendored_comfy is True
    assert scan.references == []


def test_pack_with_no_python_files(tmp_path):
    pack = tmp_path / "empty"
    pack.mkdir()
    (pack / "README.md").write_text("hi")
    scan = scan_pack(str(pack))
    assert scan.python_files == 0 and scan.references == []


def test_pycache_is_skipped(tmp_path):
    pack = tmp_path / "p"
    (pack / "__pycache__").mkdir(parents=True)
    (pack / "__pycache__" / "cached.py").write_text("from comfy.utils import Gone\n")
    (pack / "nodes.py").write_text("from comfy.utils import ProgressBar\n")
    scan = scan_pack(str(pack))
    assert scan.python_files == 1
    assert dotted(scan.references) == {"comfy.utils.ProgressBar"}


def test_unparseable_file_is_reported_not_swallowed(tmp_path):
    pack = tmp_path / "p"
    pack.mkdir()
    (pack / "broken.py").write_text("def (:\n")
    (pack / "ok.py").write_text("from comfy.utils import ProgressBar\n")
    scan = scan_pack(str(pack))
    assert len(scan.unparseable) == 1
    assert scan.unparseable[0][0] == "broken.py"
    assert dotted(scan.references) == {"comfy.utils.ProgressBar"}


def test_references_are_deduped_per_line(tmp_path):
    pack = tmp_path / "p"
    pack.mkdir()
    (pack / "a.py").write_text(
        "import comfy\nx = comfy.utils.ProgressBar\ny = comfy.utils.ProgressBar\n"
    )
    scan = scan_pack(str(pack))
    attrs = [r for r in scan.references if r.kind == ATTR]
    assert len(attrs) == 2  # different lines are distinct call sites
    assert {r.lineno for r in attrs} == {2, 3}


def test_scan_pack_uses_posix_relative_paths(tmp_path):
    pack = tmp_path / "p"
    (pack / "sub").mkdir(parents=True)
    (pack / "sub" / "n.py").write_text("from comfy.utils import ProgressBar\n")
    scan = scan_pack(str(pack))
    assert scan.references[0].file == "sub/n.py"
    assert os.sep not in scan.references[0].file or os.sep == "/"
