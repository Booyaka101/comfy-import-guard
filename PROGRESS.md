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

- 1,273 Python files, 1,992 references, 1,116 checkable call sites (1,174
  before the review's shadow guard dropped 58), 10 monkeypatches
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

## Senior review pass (2026-08-29, after the first 1.1.0 build)

Three defects found by reviewing the first cut, all fixed and regression-tested.
Two of them would have failed real users' CI on working code.

1. **Shadowed names produced hard false positives.** `from comfy.lora import
   calculate_weight` followed by any rebinding of that name (a local `def`, a
   parameter, a loop target, a walrus, a later import) still resolved calls
   against upstream, so a pack with its own `calculate_weight` would be graded
   WILL BREAK on code that is fine. Fixed with a file-wide conservative shadow
   set applied to call targets only, so 1.0.x reference behaviour is untouched.
   Dropped 58 of 1,174 corpus call sites, none of which were producing
   findings.
2. **Version shims produced hard false positives, and this one is worse
   because it punishes the careful packs.** comfyui-minimax-h3-blockcache-T8
   probes `hasattr(comfy.model_prefetch, "GRAPH_MODULES")` and calls the
   matching arity in each branch. Exactly one branch binds at any ref, so the
   other is dead code there, and the tool graded the pack WILL BREAK. Now a
   failing call whose sibling in the same file binds is a `SHIM` row under
   WARN with the reason printed. `except TypeError` also softens a call now,
   since that is the explicit "I know the signature moved" idiom.
3. **`_name_of` and `_deco_name` were an 83% clone** (house rule: diff new
   functions against the ones they parallel). Collapsed into one `name_of` in
   signature.py. A full difflib sweep now reports 0 of 91 function pairs over
   the 60% threshold.

Also: CHANGELOG.md was missing from the sdist include list, and the
`blame --param` output column was misaligned by one. Both fixed.

## derive-requires closed the signature gap too

`derive-requires` used to answer presence only, so a pack passing a keyword
that upstream added later got a floor as old as the *symbol*, not as old as
the *call*. That range installs the pack onto a ComfyUI where its own call
raises TypeError.

A release now satisfies a pack when every hard reference resolves and every
hard call binds. The binding-and-shim logic is shared with `check` via
`signature.check_call_sites`, so both commands judge a call identically. That
sharing is load-bearing rather than tidiness: a wrongly hard call in `check`
is one bad row, but in `derive` it makes every release look unsatisfiable and
the pack gets no range at all.

Measured, with the parameter boundaries verified independently by
`blame --param`:

| pack | presence only | with signatures |
| --- | --- | --- |
| calls `load_torch_file(p, return_metadata=True)` | `>=0.0.1` (wrong) | `>=0.3.20` |
| calls `pick_operations(..., scaled_fp8=...)` | no bound from a call | `>=0.2.4,<0.4.0` |
| the T8 shim pack | `>=0.30.0` | `>=0.30.0` (does not collapse) |
| all 20 corpus packs | - | identical, 20/20 |

The corpus being unchanged is the result to keep: the constraint closes a hole
without inflating anybody's floor. Cost is about 60% more wall clock on the
largest pack measured (21s to 34s on Easy-Use, 99 files and 136 call sites),
and nothing noticeable on small packs. `--no-signatures` now works on
`derive-requires` as well as `check`.

Also new: when no release can satisfy a pack, the result lists the conflicting
requirements under `conflict` instead of only saying that none does.

## What is VERIFIED working

- 124 tests green on Python 3.11 and 3.12: `python -m pytest tests -q` (59
  existing + 65 new; the two issue-reconstruction fixtures assert rule, file,
  line, blamed commit and tag boundary against live history).
- Corpus re-run after each review fix, compared row by row against the
  previous JSON: both true findings survive, all 20 verdicts unchanged, zero
  shim suppressions on the corpus. Reports kept at
  `D:\tmp\cig-corpus\report{,2,3}.json`.
- The HTTP route driven for real under aiohttp with a stand-in PromptServer:
  HTTP 200, `ok: true`, and the Easy-Use badcall + drift rows present in the
  JSON the route returns.
- `blame --param intermediate_dtype` from the installed wheel names
  `c26ca2720` with the v0.1.0/v0.1.1 boundary.
- Every error path still prints a message and exits 2, never a traceback:
  missing dir, no custom_nodes, non-comfy path, invented module, missing pack,
  and an unknown `--param` (exit 1 with an explanation).
- sdist and wheel contents listed and checked: `signature.py` and CHANGELOG.md
  both shipped, ledger.json still force-included in the wheel.
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

1. **Call checking is deliberately stricter about shadowing than reference
   checking.** Do not "simplify" by reusing one shadow policy for both: a
   spurious reference is a WARN somebody dismisses, a spurious bind is a red
   CI. Same reason the shim rule exists.
2. **Access through a class yields the plain function**, so a call site
   `Cls.method(obj, x)` passes self explicitly and a replacement is written
   with self: nothing is dropped on either side. Only `classmethod` (cls binds
   on access) and constructor calls (`Cls(...)` binds against `__init__`) drop
   the first parameter.
3. **`_value_alias_map` (local `orig = comfy.x.f` aliases) feeds only call
   resolution, never `_attribute_refs`.** Feeding it into reference extraction
   would change 1.0.x verdicts.
4. **Everything undecidable is silent**: `*`/`**` at the call, decorated
   targets or replacements, `functools.partial`, duplicate same-name defs with
   different params, re-exported names, more than one class level. The corpus
   run is the evidence this calibration is right (2 findings / 1,116 calls,
   both true).
5. **The summary line now says "breaking reference(s)"** because a breaking
   row can be a call. README examples were updated to match.
6. Signature blame walks the same word-anchored pickaxe as removals; the
   POSIX-ERE/lookbehind trap from LESSONS applies here too.

## Left undone (deliberate)

- Not pushed, not published. Branch `signature-checks` is committed locally;
  the owner ships from the phone (PR to main per house rules, then tag; the
  registry workflow publishes on its own, PyPI needs `twine upload dist/*`).
- Ledger schema untouched: signature attributions are derived live from git,
  not cached in ledger.json. Caching parameter moves in the ledger would make
  `derive-requires` and repeat `check` runs faster offline.
- Return types and attribute shapes remain out of scope, as the README says.
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
