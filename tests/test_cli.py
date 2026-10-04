"""CLI-level behaviour: pack filtering, progress and JSON output.

These run against the same session clone the other suites use, offline, so
they exercise argument handling and rendering rather than resolution itself.
"""

import json
import os
import shutil

import pytest

from comfy_import_guard.cli import main

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture()
def install(tmp_path, repo):
    """A ComfyUI root holding the two fixture packs, once a clone exists."""
    custom_nodes = tmp_path / "custom_nodes"
    custom_nodes.mkdir()
    for name in ("ancient_pack", "recent_pack"):
        shutil.copytree(os.path.join(HERE, "packs", name), custom_nodes / name)
    return str(tmp_path)


OFFLINE = ["--offline", "--no-update"]


def test_pack_filter_matching_nothing_is_an_error(install, capsys):
    code = main(["check", "--comfy-dir", install, "--pack", "nope"] + OFFLINE)
    assert code == 2
    err = capsys.readouterr().err
    assert "--pack matched no installed pack: nope" in err
    assert "ancient_pack" in err          # the message names what is installed


def test_pack_filter_typo_among_matches_warns_but_carries_on(install, capsys):
    code = main(["check", "--comfy-dir", install,
                 "--pack", "recent_pack", "--pack", "nope"] + OFFLINE)
    assert code == 0
    caught = capsys.readouterr()
    assert "warning: --pack matched no installed pack: nope" in caught.err
    assert "recent_pack" in caught.out
    assert "ancient_pack" not in caught.out


def test_progress_lines_go_to_stderr_only(install, capsys):
    code = main(["check", "--comfy-dir", install] + OFFLINE)
    assert code == 0
    caught = capsys.readouterr()
    assert "[1/2] ancient_pack" in caught.err
    assert "[2/2] recent_pack" in caught.err
    assert "[1/2]" not in caught.out


def test_quiet_suppresses_progress(install, capsys):
    code = main(["check", "--comfy-dir", install, "--quiet"] + OFFLINE)
    assert code == 0
    assert "[1/2]" not in capsys.readouterr().err


def test_json_report_stays_parseable(install, capsys):
    code = main(["check", "--comfy-dir", install, "--json"] + OFFLINE)
    assert code == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["totals"]["packs"] == 2
    assert rep["filter_misses"] == []
    assert [p["pack"] for p in rep["packs"]] == ["ancient_pack", "recent_pack"]
