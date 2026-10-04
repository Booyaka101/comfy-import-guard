"""One ``ls-tree`` per ref answers every later existence probe for that ref.

Resolution probes module prefixes in bursts: each dotted reference walks the
longest-prefix-first list and asks "is there a file here" once per step. These
tests pin the mechanism that keeps those probes from costing one git process
each, and the tree answers staying identical to what a per-file query returns.
"""

import types

import pytest

from comfy_import_guard.repo import Repo


@pytest.fixture()
def counting_repo(repo):
    """The session clone with empty in-memory caches and every spawn counted."""
    r = Repo(cache_dir=str(repo.cache_dir), offline=True, quiet=True)
    r.ensure(deep=False)
    calls = []
    real = r._git

    def counting(args, **kw):
        calls.append(list(args))
        return real(args, **kw)

    r._git = counting
    return r, calls


def _head(repo):
    return repo.resolve_ref("origin/master")


def test_listing_is_cached_per_ref(counting_repo):
    r, calls = counting_repo
    head = _head(r)
    files = r.tree_files(head)
    assert "comfy/lora.py" in files
    assert "comfy/definitely_not_a_file.py" not in files
    r.tree_files(head)
    assert sum(1 for c in calls if c[0] == "ls-tree") == 1


def test_misses_after_the_first_cost_no_spawn(counting_repo):
    r, calls = counting_repo
    head = _head(r)
    assert r.read_file(head, "comfy/nope1.py") is None   # the miss loads the index
    for i in range(2, 6):
        assert r.read_file(head, "comfy/nope%d.py" % i) is None
    assert sum(1 for c in calls if c[0] == "show") == 1
    assert sum(1 for c in calls if c[0] == "ls-tree") == 1


def test_hits_still_read_content_and_never_load_the_index(counting_repo):
    r, calls = counting_repo
    head = _head(r)
    src = r.read_file(head, "comfy/lora.py")
    assert src and "def calculate_weight" in src
    assert r.read_file(head, "comfy/lora.py") is src
    assert sum(1 for c in calls if c[0] == "show") == 1
    assert sum(1 for c in calls if c[0] == "ls-tree") == 0


def test_is_dir_agrees_with_the_tree(counting_repo):
    r, _calls = counting_repo
    head = _head(r)
    assert r.is_dir(head, "comfy/ldm") is True          # a real package dir
    assert r.is_dir(head, "comfy/lora.py") is False     # a file, not a dir
    assert r.is_dir(head, "comfy/not_a_dir") is False


def test_bogus_ref_yields_an_empty_tree(counting_repo):
    r, _calls = counting_repo
    assert r.tree_files("no-such-ref-anywhere") == frozenset(set())
    assert r.is_dir("no-such-ref-anywhere", "comfy") is False


# ------------------------------------------------------------------- fetch

def _failed_fetch(args, **kw):
    return types.SimpleNamespace(
        returncode=1, stdout="",
        stderr="fatal: unable to access 'https://github.com/': could not resolve host")


def test_update_failure_warns_instead_of_going_silent(repo, capsys, monkeypatch):
    r = Repo(cache_dir=str(repo.cache_dir), offline=False, quiet=False)
    monkeypatch.setattr(r, "_git", _failed_fetch)
    r.update()
    err = capsys.readouterr().err
    assert "could not refresh" in err
    assert "could not resolve host" in err


def test_update_failure_respects_quiet(repo, capsys, monkeypatch):
    r = Repo(cache_dir=str(repo.cache_dir), offline=False, quiet=True)
    monkeypatch.setattr(r, "_git", _failed_fetch)
    r.update()
    assert capsys.readouterr().err == ""
