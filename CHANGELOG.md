# Changelog

## 1.2.0 - 2026-09-21

A `crawl` subcommand, for the question this tool could answer but could not
reach: not "will my install survive" but "which packs in the registry are
already broken, and do their declared ComfyUI ranges match what they actually
use". It walks the public Comfy Registry listing, pulls each pack's latest
`node.zip` from the CDN, and puts it through the same extract, resolve,
signature and derive path `check` and `derive-requires` already use, pinned to
one ComfyUI ref.

From the first real run, 20 packs at `origin/master` (`b0f4b7b29`, registry
total 5667 packs): 11 analysed, 9 skipped, 0 breaking at that ref. 2 declared a
`supported_comfyui_version`, 5 had a range derived here, and of the packs where
both exist, 0 agreed and 1 disagreed. The disagreement is
`contextanchoredtilerefine` 1.6.1, which declares `>=0.3.45` while its use of
`comfy.model_management.intermediate_dtype` implies `>=0.18.0`. 8 of the 9
skips are packs with no published version in the registry, and 1 is an archive
holding no Python files.

- `crawl --out DIR` writes `board.json` and `board.md`. The JSON carries a
  `schemaVersion`, the run timestamp, the exact ref and sha, and one record per
  pack: publisher, name, version, the registry's `supported_comfyui_version`
  verbatim, the derived range and what determined it, the hard `comfy.*`
  reference count, and every already-removed symbol with its file and line. The
  markdown puts packs that break at the ref first, and links each pack to the
  repository the registry has on file for it.
- Where the registry's declared range and the derived one disagree, the record
  says so and names both. It does not pick a winner. The declared range is what
  the publisher committed to and the derived one is what the pack's usage
  needs, and which is wrong is a fact about the pack.
- `--limit`, `--min-downloads`, `--max-zip-mb` and `--fresh` shape the run.
  `--limit 0` takes the whole registry.
- Archives are cached beside the ComfyUI clone, keyed by publisher, pack and
  version. A checkpoint in `--out` holds the pinned ref, the pack selection and
  every finished record, so an interrupted or rate-limited run resumes instead
  of restarting. 429 and 5xx back off, honouring `Retry-After`.
- Because the ref, the selection and the timestamp are pinned when a run
  starts, re-running a finished crawl into the same `--out` reproduces
  `board.json` byte for byte. That is one mechanism serving both the resume
  requirement and the reproducibility one.
- A pack that cannot be analysed is skipped and counted, never fatal: a 404
  download, a 404 version record, an empty `downloadUrl`, an unpublished or
  deprecated latest version, a corrupt archive, a transfer that stops half way,
  an archive over the size cap, or an archive with no Python files in it. A pack with zero `comfy.*`
  references is analysed and recorded as having no range derived.

**Where the seam line was drawn.** A zip enters through the same file walk the
directory case uses, not a parallel one. `extract.py` grew a `PackSource`
protocol with a `DirectorySource` and a `ZipSource`; `iter_python_files` now
walks a source's member list and `scan_pack` takes either a directory path or a
source. Everything downstream (`resolve`, `signature`, `derive`, `report`) was
left alone, because none of it ever knew where the bytes came from.
`report._check_pack` was renamed to `check_pack` and made source-aware so
`crawl` calls the same function `check` does. The one thing deliberately not
merged is the pack selection loop: `registry.nodes_page` stays an HTTP
primitive and the limit, the download floor and the stopping rule live in
`crawl`, which is why `registry.iter_nodes` and `registry.total_nodes` were
deleted rather than left as a second pagination loop next to it.

Still stdlib plus `git`. The registry listing and the CDN are both public, so
`crawl` needs no account, token or key. It does need the network, and refuses
`--offline` rather than producing a board from whatever happens to be cached.

This release also carries 1.1.1 out to PyPI, which still showed 1.1.0. The
route hardening below ships with it.

## 1.1.1 - 2026-09-09

Hardening for the HTTP route. The `target` query parameter is the only
attacker-controlled input on it, and it reaches git only as a positional argv
token in a list, never through a shell. It is now validated at the route
boundary as well: a ref must start with an alphanumeric, so it can never be
read as a `-` option, and may hold only ref-safe characters with no whitespace
or shell metacharacters. Anything else returns 400 before git is invoked.

## 1.1.0 - 2026-08-29

Import success is necessary but not sufficient. The loudest custom-node
failure on the 2026 ComfyUI tracker is a TypeError, not an ImportError:
[#5355](https://github.com/Comfy-Org/ComfyUI/issues/5355) (a pack shipping an
outdated `calculate_weight` while core added `intermediate_dtype`),
[#12134](https://github.com/Comfy-Org/ComfyUI/issues/12134)
(`WanAttentionBlock.forward() got an unexpected keyword argument
'context_img_len'`) and [#13136](https://github.com/Comfy-Org/ComfyUI/issues/13136)
(`patched_forward_orig()` and `timestep_zero_index`, same shape again). This
release checks for that class statically.

- `check` now binds every direct `comfy.*` call site against the real
  parameter list at the target ref. A call that cannot bind (unknown keyword,
  too many positionals, a now-required parameter missing) is reported as
  `BADCALL` and counts toward WILL BREAK, because it raises TypeError the
  moment it runs.
- `check` also compares monkeypatch replacements (`comfy.x.f = my_f`,
  `setattr(comfy.x, "f", my_f)`, lambdas included) against the upstream
  parameter list. A replacement that no longer accepts a parameter the
  upstream original has is reported as `SIGDRIFT` and grades WARN, because
  whether core passes that argument on your path is not statically decidable.
- Both verdicts run through the same blame machinery as removals: the report
  names the commit that moved the signature, its PR, and the release boundary.
- Calls or replacements involving `*args`/`**kwargs`, decorated targets or
  replacements, `functools.partial`, and anything the alias machinery cannot
  resolve stay silent by design. A target module that fails to parse at the
  ref is counted, never silently passed.
- A call whose sibling in the same file binds is treated as a version shim and
  reported as `SHIM` under WARN, not as a break. Packs that support several
  ComfyUI versions probe with `hasattr` and call one arity per branch, so the
  branch that does not bind is dead code at that ref. Failing their build for
  it would punish exactly the packs handling compatibility properly.
- `except TypeError` around a call softens it, the same way `except
  ImportError` already softens an import. It does not soften imports.
- Names the file rebinds (a local `def`, a parameter, a loop target, a later
  import of the same name) are dropped from call checking. Presence checks can
  afford to be loose about shadowing and grade the result WARN; a call site
  cannot, because a bad bind is a hard failure.
- `derive-requires` now derives its range from signatures too. A release
  satisfies a pack only when every symbol resolves *and* every call binds, so
  passing a keyword that upstream added in v0.3.20 sets the floor there
  instead of at the symbol's much older birthday, and passing one upstream has
  since dropped produces a ceiling with no removed symbol involved. Across the
  20-pack corpus this changed no derived range, which is the point: it closes
  a hole without inflating anybody's floor.
- When no release can satisfy a pack, `derive-requires` now lists the
  conflicting requirements instead of only saying that none does.
- New `--no-signatures` flag on `check` and `derive-requires` turns the pass
  off, and `blame <symbol> --param <name>` attributes a parameter rather than
  a symbol.
- Measured before shipping on 20 real popular packs (1,273 Python files,
  1,116 checkable `comfy.*` call sites, 10 monkeypatches): 2 findings, both
  true on hand-verification against pack and ComfyUI source, 18 of 20 packs
  silent. One is a live TypeError in ComfyUI-Easy-Use's BrushNet path
  (`pick_operations` lost `scaled_fp8` in PR #11000).
- The check summary line now says "breaking reference(s)" instead of
  "missing symbol(s)", since a breaking row can now be a call, not a symbol.

## 1.0.0 - 2026-08-12

First release: `check`, `blame`, `derive-requires`, the shipped removal
ledger, and the read-only HTTP report route.
