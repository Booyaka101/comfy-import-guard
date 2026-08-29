# PROGRESS: comfy-import-guard

Status: **v1.1.0 built and verified locally, ready to ship. v1.0.0 remains the
published version on PyPI and the Comfy Registry.**

Date: 2026-08-29

## What 1.1.0 adds

Signature checks. The 2026 tracker's loudest custom-node failure is a
TypeError, not an ImportError (#5355 calculate_weight/intermediate_dtype,
#12134 WanAttentionBlock.forward/context_img_len, #13136
patched_forward_orig/timestep_zero_index). `check` now:

- binds every direct `comfy.*` call site (positional count, keyword names,
  `*`/`**` spreads) against the real parameter list at the target ref;
  unbindable calls are `BADCALL` rows under WILL BREAK
- diffs monkeypatch replacements (`comfy.x.f = my_f`, `setattr` with a literal
  name, lambdas) against the upstream parameter list; a replacement missing an
  upstream parameter is a `SIGDRIFT` row under WARN
- attributes both through the pickaxe blame path: commit, PR, tag boundary of
  the signature move
- `--no-signatures` turns the pass off

New module `comfy_import_guard/signature.py` (ParamSpec, SignatureResolver,
bind_call, replacement_drops), extraction in `extract.py` (CallSite,
Monkeypatch), blame in `blame.py` (blame_signature), wiring in `report.py`,
rendering in `cli.py`.

## Phase 0: resource verification (all passed, 2026-08-29)

| resource | verified |
| --- | --- |
| Comfy-Org/ComfyUI#5355 | closed, Custom Nodes Bug; `calculate_weight() got an unexpected keyword argument 'intermediate_dtype'`; outdated pack replacement of a core function |
| comfy/lora.py @ master | `def calculate_weight(patches, weight, key, intermediate_dtype=torch.float32, original_weights=None):` |
| Comfy-Org/ComfyUI#13136 | opened 2026-03-24; `TypeError: patched_forward_orig() got an unexpected keyword argument 'timestep_zero_index'` |

Ground truth derived from the live clone, not taken on faith:

- `context_img_len` added to `WanAttentionBlock.forward` by `0d720e436`
  (2025-04-17, last good v0.3.28, first bad v0.3.29)
- `comfy.lora.calculate_weight` carried `intermediate_dtype` from the module's
  creation in `c26ca2720` (2024-08-22, "Move calculate function to comfy.lora")
- `comfy/ldm/wan/model.py` has no BOM; the BOM seen in a PowerShell pipe is
  the shell's own injection (LESSONS 2026-08-15). `signature.py` strips
  `\ufeff` before parsing anyway.

Cost model: unchanged, anonymous git + codeload tarballs only.

## Corpus measurement (the shipping gate)

20 real popular packs downloaded as codeload tarballs into
`D:\tmp\cig-corpus\custom_nodes` (Manager, Impact-Pack, KJNodes,
VideoHelperSuite, AnimateDiff-Evolved, IPAdapter_plus, Custom-Scripts,
rgthree, efficiency-nodes, GGUF, Easy-Use, WAS suite, UltimateSDUpscale,
Frame-Interpolation, WD14-Tagger, TeaCache, MagCache, WanVideoWrapper,
controlnet_aux, cg-use-everywhere), plus the real local install.

Result against origin/master (`e7051b037`), full JSON at
`D:\tmp\cig-corpus\report.json`:

- 1,273 Python files, 1,992 references, 1,174 direct call sites, 10 monkeypatches
- signature findings: **2**, both in ComfyUI-Easy-Use, both hand-verified true
  - BADCALL `comfy.ops.pick_operations` brushnet/__init__.py:676 passes
    `scaled_fp8=`, removed upstream by `43071e3de` PR #11000 (v0.3.77 ->
    v0.4.0). Live TypeError on current master; verified against both sources.
  - SIGDRIFT `comfy.clip_vision.load_clipvision_from_sd`
    kolors/loader.py:282, replacement `load_clipvision_vitl_336(path)` vs
    upstream `(sd, prefix="", convert_keys=False)`. Real drift, correctly WARN.
- the other 9 monkeypatches: 5 match upstream exactly, 3 are `*args/**kwargs`
  wrappers (silent by design), 1 fired. No rule fires on a large fraction of
  the corpus; nothing was deleted because nothing over-fired.
- presence rules unchanged: TeaCache/MagCache still WILL BREAK on
  `precompute_freqs_cis`, efficiency-nodes and WAS report their own known
  missing symbols, 15/20 SAFE.

## What is VERIFIED working

- 107 tests green: `python -m pytest tests -q` (59 existing + 48 new; the two
  issue-reconstruction fixtures assert rule, file, line, blamed commit and tag
  boundary against live history).
- Worked example reproduced exactly: 3-param `my_calculate_weight` monkeypatch
  gives SIGDRIFT WARN naming `intermediate_dtype`/`original_weights`; an
  unknown-keyword call gives a BADCALL hard row with the upstream parameter
  list; the plain 3-positional call stays silent.
- `pip install .` into a fresh venv: only `comfy-import-guard 1.1.0`, console
  script works from a clean cwd.
- Acceptance: installed CLI `check --comfy-dir D:\ComfyUI_windows_portable\ComfyUI`
  is SAFE, exit 0.
- `--no-signatures` disables the pass (tested).
- A target module that fails to parse at the ref becomes a counted
  `TARGET_UNPARSED` row (tested with a stub repo).
- Offline/shallow blame failures are caught: the finding ships without
  attribution instead of crashing.

## Non-obvious decisions future work must not undo

1. **Access through a class yields the plain function**, so a call site
   `Cls.method(obj, x)` passes self explicitly and a replacement is written
   with self: nothing is dropped on either side. Only `classmethod` (cls binds
   on access) and constructor calls (`Cls(...)` binds against `__init__`) drop
   the first parameter.
2. **`_value_alias_map` (local `orig = comfy.x.f` aliases) feeds only call
   resolution, never `_attribute_refs`.** Feeding it into reference extraction
   would change 1.0.x verdicts.
3. **Everything undecidable is silent**: `*`/`**` at the call, decorated
   targets or replacements, `functools.partial`, duplicate same-name defs with
   different params, re-exported names, more than one class level. The corpus
   run is the evidence this calibration is right (2 findings / 1,174 calls,
   both true).
4. **The summary line now says "breaking reference(s)"** because a breaking
   row can be a call. README examples were updated to match.
5. Signature blame walks the same word-anchored pickaxe as removals; the
   POSIX-ERE/lookbehind trap from LESSONS applies here too.

## Left undone (deliberate)

- Not pushed, not published. Branch `signature-checks` is committed locally;
  the owner ships from the phone (PR to main per house rules, then tag; the
  registry workflow publishes on its own, PyPI needs `twine upload dist/*`).
- Ledger schema untouched: signature attributions are derived live from git,
  not cached in ledger.json. A future `blame --param` CLI could reuse
  `blame_signature` directly.
- ComfyUI-Easy-Use was not reported upstream. The BADCALL finding
  (`pick_operations`/`scaled_fp8`) is a real bug in their BrushNet path worth
  an issue in the owner's voice, after 1.1.0 ships.

## Next steps if resumed

1. Push `signature-checks`, open a PR, merge on green CI (check-runs API, not
   `gh run watch`), tag v1.1.0.
2. `python -m build` and `twine upload dist/*` (token in `~/.pypirc`).
3. Watch the registry flip 1.1.0 to active as it did for 1.0.0.
4. Optional distribution: a comment on #5355/#12134 in the owner's voice, and
   an issue to ComfyUI-Easy-Use about the `scaled_fp8` call.
