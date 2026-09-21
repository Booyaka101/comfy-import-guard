"""crawl against a local fake registry. Nothing here touches the network.

The ComfyUI clone is still real, because the whole point of a board is what the
derived range says about real release history. The registry, the CDN and every
failure they can produce come from tests/fakeregistry.py.
"""

import json
import os
import zipfile

import pytest

from comfy_import_guard import crawl as crawl_mod
from comfy_import_guard.crawl import (
    AGREE,
    DISAGREE,
    NOT_COMPARABLE,
    NOT_DERIVED,
    REGISTRY_SILENT,
    SKIPPED,
    _cache_path,
    _compare,
    _safe,
    board_order,
    crawl,
    render_markdown,
)
from comfy_import_guard.errors import NetworkError
from comfy_import_guard.extract import DirectorySource, ZipSource, is_pack_python, scan_pack
from comfy_import_guard.registry import RegistryClient
from comfy_import_guard.version import parse_range, render_bound

from fakeregistry import FakeRegistry, make_zip

USES_COMFY = {"__init__.py": "from .nodes import NODE_CLASS_MAPPINGS\n",
              "nodes.py": "import comfy.utils\nfrom comfy.utils import ProgressBar\n"}

NO_COMFY = {"nodes.py": "import os\n\nNODE_CLASS_MAPPINGS = {}\n"}

# Deleted upstream in ComfyUI v0.8.0, which is what makes a pack using it break.
REMOVED = {"nodes.py": "from comfy.ldm.lightricks.model import precompute_freqs_cis\n"}


@pytest.fixture
def out_dir(tmp_path):
    return tmp_path / "board"


@pytest.fixture
def crawl_repo(repo, tmp_path, monkeypatch):
    """The real clone, with pack downloads redirected into the test's tmp dir."""
    monkeypatch.setattr(repo, "cache_dir", tmp_path / "cache")
    return repo


def run(registry, repo, out, retries=2, **kw):
    client = RegistryClient(api_root=registry.url, retries=retries, backoff=0,
                            sleep=kw.pop("sleep", lambda seconds: None))
    kw.setdefault("limit", 0)
    return crawl(repo, out, client=client, **kw)


def by_id(board):
    return {p["id"]: p for p in board["packs"]}


# --------------------------------------------------------------- the happy path

def test_board_counts_add_up_and_both_files_are_written(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("uses-comfy", files=USES_COMFY)
        reg.add_pack("no-comfy", files=NO_COMFY)
        reg.add_pack("no-release", version=None)
        reg.add_pack("empty-zip", files={"README.md": "nothing to parse\n"})
        board = run(reg, crawl_repo, out_dir)

    t = board["totals"]
    assert t["packs"] == 4
    assert t["analysed"] + t["skipped"] == t["packs"]
    assert t["analysed"] == 2 and t["skipped"] == 2
    assert board["registryTotal"] == 4
    assert board["comfySha"] == crawl_repo.resolve_ref("origin/master")
    assert board["schemaVersion"] == crawl_mod.SCHEMA_VERSION
    assert (out_dir / "board.json").exists()
    assert (out_dir / "board.md").exists()

    packs = by_id(board)
    assert packs["uses-comfy"]["derived"]["range"].startswith(">=")
    assert packs["uses-comfy"]["hardReferences"] == 2


def test_board_json_holds_the_registry_string_verbatim(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("declared", files=USES_COMFY, declared="  >= 0.3.45 ")
        board = run(reg, crawl_repo, out_dir)
    record = by_id(board)["declared"]
    assert record["registry"]["supportedComfyuiVersion"] == "  >= 0.3.45 "
    assert record["registry"]["declaredIn"] == "version"


def test_packs_keep_registry_order_in_board_json(crawl_repo, out_dir):
    with FakeRegistry(page_size=2) as reg:
        for i in range(5):
            reg.add_pack("pack-%d" % i, files=NO_COMFY)
        board = run(reg, crawl_repo, out_dir)
    assert [p["id"] for p in board["packs"]] == ["pack-%d" % i for i in range(5)]


def test_limit_takes_the_first_n_across_pages(crawl_repo, out_dir):
    with FakeRegistry(page_size=2) as reg:
        for i in range(5):
            reg.add_pack("pack-%d" % i, files=NO_COMFY)
        board = run(reg, crawl_repo, out_dir, limit=3)
    assert [p["id"] for p in board["packs"]] == ["pack-0", "pack-1", "pack-2"]
    assert board["registryTotal"] == 5


def test_min_downloads_filters_the_selection(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("popular", files=NO_COMFY, downloads=500)
        reg.add_pack("quiet", files=NO_COMFY, downloads=2)
        board = run(reg, crawl_repo, out_dir, min_downloads=100)
    assert [p["id"] for p in board["packs"]] == ["popular"]


# ------------------------------------------------------------------ edge cases

def test_zip_with_no_python_files_is_skipped_and_counted(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("docs-only", files={"README.md": "# hi\n", "logo.png": "not python"})
        board = run(reg, crawl_repo, out_dir)
    record = by_id(board)["docs-only"]
    assert record["status"] == SKIPPED
    assert "no Python files" in record["skipReason"]
    assert board["totals"]["skipped"] == 1
    assert "docs-only" in render_markdown(board)


def test_pack_with_no_comfy_references_is_analysed_without_a_range(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("standalone", files=NO_COMFY)
        board = run(reg, crawl_repo, out_dir)
    record = by_id(board)["standalone"]
    assert record["status"] == "analysed"
    assert record["derived"] is None
    assert record["hardReferences"] == 0
    assert "no comfy.* references" in record["note"]
    assert record["agreement"] == REGISTRY_SILENT


def test_download_that_404s_is_recorded_as_a_skip(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("gone", files=USES_COMFY)
        reg.versions[("gone", "1.0.0")]["downloadUrl"] = "/cdn/not-there.zip"
        reg.add_pack("fine", files=USES_COMFY)
        board = run(reg, crawl_repo, out_dir)
    record = by_id(board)["gone"]
    assert record["status"] == SKIPPED
    assert "404" in record["skipReason"]
    assert by_id(board)["fine"]["status"] == "analysed"


def test_version_record_that_404s_is_recorded_as_a_skip(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("ghost", files=USES_COMFY)
        del reg.versions[("ghost", "1.0.0")]
        board = run(reg, crawl_repo, out_dir)
    assert "404" in by_id(board)["ghost"]["skipReason"]


def test_latest_version_with_no_download_url_is_skipped(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("urlless", files=USES_COMFY, download_url="")
        board = run(reg, crawl_repo, out_dir)
    assert "no downloadUrl" in by_id(board)["urlless"]["skipReason"]


def test_unpublished_latest_version_is_skipped(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("pending", files=USES_COMFY, status="NodeVersionStatusPending")
        board = run(reg, crawl_repo, out_dir)
    record = by_id(board)["pending"]
    assert record["status"] == SKIPPED
    assert "NodeVersionStatusPending" in record["skipReason"]


def test_deprecated_latest_version_is_skipped(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("old", files=USES_COMFY, deprecated=True)
        board = run(reg, crawl_repo, out_dir)
    assert "deprecated" in by_id(board)["old"]["skipReason"]


def test_pack_with_no_published_version_is_skipped_without_a_request(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("unreleased", version=None)
        board = run(reg, crawl_repo, out_dir)
        assert not reg.requests_for("/versions/")
    assert "no published version" in by_id(board)["unreleased"]["skipReason"]


def test_non_utf8_source_is_recorded_and_the_pack_still_analyses(crawl_repo, out_dir):
    files = {
        "nodes.py": "import comfy.utils\n".encode("utf-8"),
        # CPython tolerates undecodable bytes in a comment but not in a literal.
        "legacy.py": "AUTHOR = 'caf\xe9'\nimport os\n".encode("latin-1"),
        "commented.py": "# caf\xe9 pour l'auteur\nimport os\n".encode("latin-1"),
        "declared.py": ("# -*- coding: latin-1 -*-\nAUTHOR = 'caf\xe9'\n").encode("latin-1"),
    }
    with FakeRegistry() as reg:
        reg.add_pack("mixed-encodings", archive=make_zip(files))
        board = run(reg, crawl_repo, out_dir)
    record = by_id(board)["mixed-encodings"]
    assert record["status"] == "analysed"
    assert record["pythonFiles"] == 4
    bad = [u["file"] for u in record["unparseable"]]
    assert bad == ["legacy.py"], record["unparseable"]
    assert record["derived"]["range"].startswith(">=")


def test_oversize_archive_is_refused_rather_than_expanded(crawl_repo, out_dir):
    payload = make_zip({"nodes.py": "import comfy.utils\n" + "# pad\n" * 5000})
    with FakeRegistry() as reg:
        reg.add_pack("huge", archive=payload)
        board = run(reg, crawl_repo, out_dir, max_zip_bytes=200)
        cached = _cache_path(crawl_repo.cache_dir / "packs",
                             {"publisher": "pub", "id": "huge", "version": "1.0.0"})
    record = by_id(board)["huge"]
    assert record["status"] == SKIPPED
    assert "over the" in record["skipReason"]
    assert not cached.exists()
    assert list((crawl_repo.cache_dir).rglob("*.part")) == []


def test_oversize_archive_is_refused_without_a_content_length(crawl_repo, out_dir):
    payload = make_zip({"nodes.py": "import comfy.utils\n" + "# pad\n" * 5000})
    with FakeRegistry() as reg:
        reg.add_pack("huge", archive=payload)
        reg.headless.add("/cdn/huge-1.0.0.zip")
        board = run(reg, crawl_repo, out_dir, max_zip_bytes=200)
    assert "over the" in by_id(board)["huge"]["skipReason"]


def test_corrupt_archive_is_skipped_and_the_cached_copy_dropped(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("corrupt", archive=b"PK\x03\x04 this is not a zip")
        board = run(reg, crawl_repo, out_dir)
        cached = _cache_path(crawl_repo.cache_dir / "packs",
                             {"publisher": "pub", "id": "corrupt", "version": "1.0.0"})
    record = by_id(board)["corrupt"]
    assert record["status"] == SKIPPED
    assert "not a readable zip archive" in record["skipReason"]
    assert not cached.exists()


def test_one_bad_pack_does_not_stop_the_crawl(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("first", files=USES_COMFY)
        reg.add_pack("broken", archive=b"not a zip at all")
        reg.add_pack("last", files=USES_COMFY)
        board = run(reg, crawl_repo, out_dir)
    assert board["totals"]["packs"] == 3
    assert by_id(board)["last"]["status"] == "analysed"


# ------------------------------------------------------- rate limits and resume

def test_429_and_5xx_back_off_then_succeed(crawl_repo, out_dir):
    waits = []
    with FakeRegistry() as reg:
        reg.add_pack("slow", files=USES_COMFY)
        reg.fail("/nodes", [429, 503])
        reg.fail("/cdn/slow-1.0.0.zip", [500])
        board = run(reg, crawl_repo, out_dir, retries=4, sleep=waits.append)
    assert by_id(board)["slow"]["status"] == "analysed"
    assert len(waits) == 3, waits
    assert waits[0] == 0.0          # Retry-After: 0 on the 429, honoured verbatim


def test_retries_are_finite_and_a_dead_route_becomes_a_skip(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("flaky", files=USES_COMFY)
        reg.fail("/cdn/flaky-1.0.0.zip", [503] * 10)
        board = run(reg, crawl_repo, out_dir, retries=2)
    record = by_id(board)["flaky"]
    assert record["status"] == SKIPPED
    assert "HTTP 503" in record["skipReason"]


def test_a_dropped_connection_checkpoints_then_the_next_run_resumes(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("one", files=USES_COMFY)
        reg.add_pack("two", files=USES_COMFY)
        reg.add_pack("three", files=USES_COMFY)
        reg.drop.add("/nodes/two/versions/1.0.0")

        with pytest.raises(NetworkError) as excinfo:
            run(reg, crawl_repo, out_dir, retries=0)
        assert "resumes from its checkpoint" in str(excinfo.value)

        state = json.loads((out_dir / crawl_mod.CHECKPOINT).read_text(encoding="utf-8"))
        assert [p["id"] for p in state["packs"]] == ["one"]
        assert len(state["selection"]) == 3
        pinned = state["generatedAt"]

        reg.drop.clear()
        before = len(reg.requests)
        board = run(reg, crawl_repo, out_dir, retries=0)

    assert [p["id"] for p in board["packs"]] == ["one", "two", "three"]
    assert board["generatedAt"] == pinned, "a resumed run keeps the original timestamp"
    resumed = reg.requests[before:]
    assert not [r for r in resumed if r.startswith("/nodes?")], "selection was re-listed"
    assert not [r for r in resumed if "/nodes/one/" in r], "a finished pack was refetched"


def test_a_finished_crawl_reruns_without_touching_the_registry(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("one", files=USES_COMFY)
        first = (out_dir / "board.json").parent
        run(reg, crawl_repo, out_dir)
        before = len(reg.requests)
        run(reg, crawl_repo, out_dir)
        assert reg.requests[before:] == []
    assert first.exists()


def test_a_second_run_reproduces_board_json_byte_for_byte(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("uses-comfy", files=USES_COMFY, declared=">=0.3.45")
        reg.add_pack("no-comfy", files=NO_COMFY)
        reg.add_pack("no-release", version=None)
        run(reg, crawl_repo, out_dir)
        first = (out_dir / "board.json").read_bytes()
        first_md = (out_dir / "board.md").read_bytes()
        run(reg, crawl_repo, out_dir)
        assert (out_dir / "board.json").read_bytes() == first
        assert (out_dir / "board.md").read_bytes() == first_md


def test_a_fresh_run_differs_only_in_its_timestamp(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("uses-comfy", files=USES_COMFY, declared=">=0.3.45")
        reg.add_pack("no-comfy", files=NO_COMFY)
        first = run(reg, crawl_repo, out_dir)
        first_bytes = (out_dir / "board.json").read_bytes()
        second = run(reg, crawl_repo, out_dir, fresh=True)
        second_bytes = (out_dir / "board.json").read_bytes()

    assert second["generatedAt"] != "" and first["generatedAt"] != ""
    rewritten = second_bytes.replace(second["generatedAt"].encode(),
                                     first["generatedAt"].encode())
    assert rewritten == first_bytes


def test_a_checkpoint_pinned_to_another_ref_is_discarded(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("one", files=USES_COMFY)
        run(reg, crawl_repo, out_dir)
        path = out_dir / crawl_mod.CHECKPOINT
        state = json.loads(path.read_text(encoding="utf-8"))
        state["comfySha"] = "0" * 40
        path.write_text(json.dumps(state), encoding="utf-8")
        before = len(reg.requests)
        board = run(reg, crawl_repo, out_dir)
        assert [r for r in reg.requests[before:] if r.startswith("/nodes?")]
    assert board["comfySha"] == crawl_repo.resolve_ref("origin/master")


def test_an_unreadable_checkpoint_starts_the_crawl_over(crawl_repo, out_dir):
    out_dir.mkdir(parents=True)
    (out_dir / crawl_mod.CHECKPOINT).write_text("{not json", encoding="utf-8")
    notes = []
    with FakeRegistry() as reg:
        reg.add_pack("one", files=USES_COMFY)
        board = run(reg, crawl_repo, out_dir, log=notes.append)
    assert board["totals"]["packs"] == 1
    assert any("unreadable" in n for n in notes), notes


# ------------------------------------------------------------- the board itself

def test_a_pack_that_breaks_at_the_ref_is_reported_first(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("healthy", files=USES_COMFY)
        reg.add_pack("broken", files=REMOVED)
        board = run(reg, crawl_repo, out_dir)

    record = by_id(board)["broken"]
    assert record["breaksAtRef"] is True
    assert board["totals"]["breaksAtRef"] == 1
    gone = record["removedAtRef"][0]
    assert gone["dotted"] == "comfy.ldm.lightricks.model.precompute_freqs_cis"
    assert gone["file"] == "nodes.py" and gone["line"] == 1

    assert board_order(board["packs"])[0]["id"] == "broken"
    md = render_markdown(board)
    head = md.split("## Packs")[0]
    assert "precompute_freqs_cis" in head and "nodes.py:1" in head
    assert md.index("| broken ") < md.index("| healthy ")


def test_a_declared_range_we_disagree_with_is_recorded_not_resolved(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("optimistic", files=USES_COMFY, declared=">=9.9.9")
        board = run(reg, crawl_repo, out_dir)
    record = by_id(board)["optimistic"]
    assert record["agreement"] == DISAGREE
    assert record["registry"]["supportedComfyuiVersion"] == ">=9.9.9"
    assert record["derived"]["range"].startswith(">=")
    assert "registry says >=9.9.9" in record["disagreement"]
    assert "usage implies" in record["disagreement"]
    assert "Where the registry and this tool disagree" in render_markdown(board)


def test_a_declared_range_we_cannot_compare_says_so(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("tilde", files=USES_COMFY, declared="~=0.3.0")
        board = run(reg, crawl_repo, out_dir)
    record = by_id(board)["tilde"]
    assert record["agreement"] == NOT_COMPARABLE
    assert "~=0.3.0" in record["disagreement"]


def test_a_declared_range_with_nothing_to_compare_against(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("silent-usage", files=NO_COMFY, declared=">=0.3.45")
        board = run(reg, crawl_repo, out_dir)
    assert by_id(board)["silent-usage"]["agreement"] == NOT_DERIVED


def test_markdown_escapes_registry_supplied_pipes(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("pipe|name", files=NO_COMFY)
        board = run(reg, crawl_repo, out_dir)
    row = [line for line in render_markdown(board).splitlines()
           if line.startswith("| pipe")][0]
    assert r"pipe\|name" in row
    assert row.count("|") - row.count("\\|") == 7, row      # the six cell borders


def test_board_json_is_serialisable_as_written(crawl_repo, out_dir):
    with FakeRegistry() as reg:
        reg.add_pack("one", files=USES_COMFY)
        run(reg, crawl_repo, out_dir)
    reloaded = json.loads((out_dir / "board.json").read_text(encoding="utf-8"))
    assert reloaded["packs"][0]["id"] == "one"


# ------------------------------------------------------------------ unit checks

@pytest.mark.parametrize("text,lower,upper,excludes", [
    (">=0.3.45", ">=0.3.45", "none", ()),
    (">= 0.3.45", ">=0.3.45", "none", ()),
    ("v0.3.45", ">=0.3.45", "<=0.3.45", ()),
    (">=0.3,<0.5", ">=0.3", "<0.5", ()),
    (">=0.3,>=0.4", ">=0.4", "none", ()),
    ("<0.5,<=0.4", "none", "<=0.4", ()),
    ("==1.0", ">=1.0", "<=1.0", ()),
    (">=0.3,!=0.4", ">=0.3", "none", ("0.4",)),
])
def test_parse_range_reduces_to_endpoints(text, lower, upper, excludes):
    parsed = parse_range(text)
    assert render_bound(parsed.lower, lower=True) == lower
    assert render_bound(parsed.upper, lower=False) == upper
    assert parsed.excludes == excludes


@pytest.mark.parametrize("text", ["", "   ", "~=1.0.0", "!=0.4", "latest", ">=abc", ">"])
def test_parse_range_declines_what_it_cannot_place(text):
    assert parse_range(text) is None


def test_padded_versions_compare_equal():
    assert parse_range(">=0.3").same_bounds_as(parse_range(">=0.3.0.0"))


@pytest.mark.parametrize("declared,derived,expected", [
    ("", ">=0.3.0", REGISTRY_SILENT),
    (">=0.3.0", None, NOT_DERIVED),
    (">=0.3.0", ">=0.3.0", AGREE),
    (">=0.3.0", ">=0.3.0.0", AGREE),
    (">=0.3.0", ">=0.4.0", DISAGREE),
    ("~=0.3.0", ">=0.3.0", NOT_COMPARABLE),
])
def test_compare_reports_agreement_without_choosing(declared, derived, expected):
    assert _compare(declared, derived)["agreement"] == expected


def test_compare_names_both_sides_of_a_ceiling_disagreement():
    out = _compare(">=0.3.0,<0.9.0", ">=0.3.0,<0.5.0")
    assert out["agreement"] == DISAGREE
    assert out["disagreement"] == "ceiling: registry says <0.9.0, usage implies <0.5.0"


def test_compare_names_a_missing_endpoint_as_none():
    out = _compare(">=0.3.0", ">=0.3.0,<0.5.0")
    assert out["disagreement"] == "ceiling: registry says none, usage implies <0.5.0"


@pytest.mark.parametrize("member,wanted", [
    ("nodes.py", True),
    ("sub/nodes.py", True),
    ("README.md", False),
    ("__pycache__/nodes.py", False),
    (".venv/lib/x.py", False),
    ("thing.egg-info/x.py", False),
    ("../escape.py", False),
    ("/abs/path.py", False),
    ("C:/win/path.py", False),
    ("dir/../x.py", False),
])
def test_is_pack_python_matches_what_the_directory_walker_reads(member, wanted):
    assert is_pack_python(member) is wanted


def test_a_zip_and_a_directory_scan_identically(tmp_path):
    files = {
        "__init__.py": "from .nodes import NODE_CLASS_MAPPINGS\n",
        "nodes.py": "import comfy.utils\n"
                    "from comfy.samplers import KSampler\n"
                    "comfy.utils.ProgressBar(1)\n",
        "sub/helper.py": "from comfy.model_management import get_torch_device\n",
        "__pycache__/nodes.cpython-311.pyc": "junk",
        "README.md": "# docs\n",
    }
    unpacked = tmp_path / "pack"
    for name, body in files.items():
        path = unpacked / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    archive = tmp_path / "node.zip"
    archive.write_bytes(make_zip(files))

    from_dir = scan_pack(DirectorySource(str(unpacked), "pack"))
    with ZipSource(str(archive), "pack") as source:
        from_zip = scan_pack(source)

    assert from_zip.python_files == from_dir.python_files == 3
    assert [(r.dotted, r.file, r.lineno) for r in from_zip.references] == \
           [(r.dotted, r.file, r.lineno) for r in from_dir.references]
    assert [(c.dotted, c.file, c.lineno) for c in from_zip.call_sites] == \
           [(c.dotted, c.file, c.lineno) for c in from_dir.call_sites]
    assert from_zip.vendored_comfy == from_dir.vendored_comfy is False


def test_a_zip_that_vendors_comfy_is_detected(tmp_path):
    archive = tmp_path / "node.zip"
    archive.write_bytes(make_zip({"comfy/__init__.py": "", "nodes.py": "import comfy.utils\n"}))
    with ZipSource(str(archive), "vendored") as source:
        assert scan_pack(source).vendored_comfy is True


def test_an_oversized_member_is_reported_rather_than_expanded(tmp_path):
    archive = tmp_path / "node.zip"
    archive.write_bytes(make_zip({"generated.py": "X = 1\n" * 100_000,
                                  "nodes.py": "import comfy.utils\n"}))
    with ZipSource(str(archive), "bomb", max_member_bytes=1024) as source:
        scan = scan_pack(source)
    assert scan.python_files == 2
    assert scan.unparseable[0][0] == "generated.py"
    assert "per-file cap" in scan.unparseable[0][1]
    assert [r.dotted for r in scan.references] == ["comfy.utils"]


def test_zip_source_rejects_what_it_cannot_open(tmp_path):
    from comfy_import_guard.errors import BadInputError

    missing = tmp_path / "nope.zip"
    with pytest.raises(BadInputError):
        ZipSource(str(missing))
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")
    with pytest.raises(BadInputError):
        ZipSource(str(broken))


@pytest.mark.parametrize("raw,expected", [
    ("comfyui-impact-pack", "comfyui-impact-pack"),
    ("../../etc", ".._.._etc"),
    ("a/b\\c", "a_b_c"),
    ("..", "_"),
    ("", "_"),
])
def test_cache_components_are_sanitised(raw, expected):
    assert _safe(raw) == expected


def test_cache_path_is_laid_out_per_publisher_pack_and_version(tmp_path):
    path = _cache_path(tmp_path, {"publisher": "acme", "id": "pack", "version": "1.2.3"})
    assert path == tmp_path / "acme" / "pack" / "1.2.3" / "node.zip"


def test_a_pack_archive_written_by_zipfile_round_trips(tmp_path):
    archive = tmp_path / "node.zip"
    archive.write_bytes(make_zip({"nodes.py": "import comfy.utils\n"}))
    with zipfile.ZipFile(str(archive)) as zf:
        assert zf.namelist() == ["nodes.py"]
    assert os.path.getsize(str(archive)) > 0


# ----------------------------------------------------------------- bad flags

@pytest.mark.parametrize("kw,needle", [
    ({"limit": -1}, "--limit"),
    ({"max_zip_bytes": 0}, "--max-zip-mb"),
    ({"max_zip_bytes": -5}, "--max-zip-mb"),
    ({"min_downloads": -1}, "--min-downloads"),
])
def test_nonsense_flag_values_are_refused_before_anything_is_fetched(
        crawl_repo, out_dir, kw, needle):
    from comfy_import_guard.errors import BadInputError

    with FakeRegistry() as registry:
        registry.add_pack("pack-a")
        with pytest.raises(BadInputError) as exc:
            run(registry, crawl_repo, out_dir, **kw)
        assert needle in str(exc.value)
        assert registry.requests == []


def test_an_unwritable_output_directory_is_a_clear_error(crawl_repo, tmp_path):
    from comfy_import_guard.errors import BadInputError

    blocker = tmp_path / "board"
    blocker.write_text("not a directory", encoding="utf-8")
    with FakeRegistry() as registry:
        registry.add_pack("pack-a")
        with pytest.raises(BadInputError) as exc:
            run(registry, crawl_repo, blocker)
        assert "Cannot write the board" in str(exc.value)


# ------------------------------------------------- a listing that lies about itself

def test_listing_fields_of_the_wrong_type_do_not_crash_the_crawl(crawl_repo, out_dir):
    with FakeRegistry() as registry:
        node = registry.add_pack("pack-a", files=USES_COMFY)
        node["publisher"] = ["not", "a", "mapping"]
        node["downloads"] = "lots"
        registry.add_pack("pack-b", files=USES_COMFY, downloads=5)
        board = run(registry, crawl_repo, out_dir, min_downloads=1)

    # "lots" is not a count, so it reads as 0 and falls below the floor.
    assert [p["id"] for p in board["packs"]] == ["pack-b"]


def test_a_listing_with_no_total_still_renders_a_board(crawl_repo, out_dir):
    class NoTotals(FakeRegistry):
        def _page(self, query):
            payload = json.loads(super()._page(query))
            payload.pop("total")
            payload.pop("totalPages")
            return json.dumps(payload).encode("utf-8")

    with NoTotals() as registry:
        registry.add_pack("pack-a", files=USES_COMFY)
        board = run(registry, crawl_repo, out_dir)

    assert board["registryTotal"] is None
    assert board["totals"]["packs"] == 1
    markdown = render_markdown(board)
    assert "None" not in markdown
    assert "Source: %s." % board["registrySource"] in markdown


# ------------------------------------------------- a body that stops half way

def test_an_archive_cut_off_mid_transfer_skips_that_pack_and_leaves_no_cache(
        crawl_repo, out_dir, tmp_path):
    with FakeRegistry() as registry:
        registry.add_pack("truncated", files=USES_COMFY)
        registry.add_pack("intact", files=USES_COMFY)
        registry.truncate.add("/cdn/truncated-1.0.0.zip")
        board = run(registry, crawl_repo, out_dir)

    packs = by_id(board)
    assert packs["truncated"]["status"] == SKIPPED
    assert "ended early" in packs["truncated"]["skipReason"]
    assert packs["intact"]["status"] == "analysed"
    cached = _cache_path(tmp_path / "cache" / "packs",
                         {"publisher": "pub", "id": "truncated", "version": "1.0.0"})
    assert not cached.exists()
    assert not cached.with_suffix(".zip.part").exists()


def test_a_listing_cut_off_mid_transfer_is_a_registry_error_not_a_traceback(
        crawl_repo, out_dir):
    from comfy_import_guard.errors import RegistryError

    with FakeRegistry() as registry:
        registry.add_pack("pack-a", files=USES_COMFY)
        registry.truncate.add("/nodes")
        with pytest.raises(RegistryError) as exc:
            run(registry, crawl_repo, out_dir)
        assert "ended early" in str(exc.value)


# ------------------------------------------------------------ repository links

def test_a_pack_row_links_to_the_repository_the_registry_gave(crawl_repo, out_dir):
    with FakeRegistry() as registry:
        registry.add_pack("linked", files=USES_COMFY,
                          repository="https://github.com/someone/linked")
        registry.add_pack("bare", files=USES_COMFY)
        board = run(registry, crawl_repo, out_dir)

    markdown = render_markdown(board)
    assert "| [linked](https://github.com/someone/linked) |" in markdown
    assert "| bare |" in markdown


@pytest.mark.parametrize("repository", [
    "javascript:alert(1)",
    "https://example.com/a)b",
    "https://example.com/a b",
    "not a url at all",
])
def test_a_repository_that_could_break_the_markdown_is_not_linked(
        crawl_repo, out_dir, repository):
    with FakeRegistry() as registry:
        registry.add_pack("risky", files=USES_COMFY, repository=repository)
        board = run(registry, crawl_repo, out_dir)

    markdown = render_markdown(board)
    assert "| risky |" in markdown
    assert repository not in markdown
