# Changelog

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
- New `--no-signatures` flag on `check` turns the whole pass off.
- Measured before shipping on 20 real popular packs (1,273 Python files,
  1,174 `comfy.*` call sites, 10 monkeypatches): 2 findings, both true on
  hand-verification against pack and ComfyUI source, 18 of 20 packs silent.
  One is a live TypeError in ComfyUI-Easy-Use's BrushNet path
  (`pick_operations` lost `scaled_fp8` in PR #11000).
- The check summary line now says "breaking reference(s)" instead of
  "missing symbol(s)", since a breaking row can now be a call, not a symbol.

## 1.0.0 - 2026-08-12

First release: `check`, `blame`, `derive-requires`, the shipped removal
ledger, and the read-only HTTP report route.
