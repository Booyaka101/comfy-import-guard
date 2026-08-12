import os
import shutil

import pytest

from comfy_import_guard.repo import Repo

HERE = os.path.dirname(os.path.abspath(__file__))
PACKS = os.path.join(HERE, "packs")


@pytest.fixture(scope="session")
def repo():
    """A real ComfyUI clone. These tests assert against live public history."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    r = Repo(cache_dir=os.environ.get("COMFY_IMPORT_GUARD_CACHE"), quiet=True)
    try:
        r.ensure(deep=True)
    except Exception as exc:  # network down / no clone
        pytest.skip("no ComfyUI clone available: %s" % exc)
    return r


@pytest.fixture(scope="session")
def packs_dir():
    return PACKS
