# comfy-import-guard

[![tests](https://github.com/Booyaka101/comfy-import-guard/actions/workflows/test.yml/badge.svg)](https://github.com/Booyaka101/comfy-import-guard/actions/workflows/test.yml)

Predicts which of your ComfyUI custom-node packs will die on the next `git pull`,
and names the commit and PR that killed them. Catches both failure shapes:
the ImportError from a removed `comfy.*` symbol, and the TypeError from a call
or monkeypatch whose parameter list no longer matches upstream.

Zero third-party dependencies. It has to load inside a ComfyUI whose other packs
are already broken, so it uses nothing but the standard library and `git`.

## The problem

ComfyUI's `comfy.*` modules are internal. There is no deprecation policy, no
`__all__`, no shim. Packs import from them anyway, because there is no other way
to hook the sampler or patch a model. So a refactor lands and a pack stops loading:

```
ImportError: cannot import name 'precompute_freqs_cis' from 'comfy.ldm.lightricks.model'
```

That one broke ComfyUI-TeaCache and ComfyUI-MagCache on 2026-01-05
([Comfy-Org/ComfyUI#11660](https://github.com/Comfy-Org/ComfyUI/issues/11660), still open).
The same shape hit `comfy.ldm.minimax.model.time_shift_slope` on 2026-08-06
([T8mars/comfyui-minimax-h3-blockcache-T8#1](https://github.com/T8mars/comfyui-minimax-h3-blockcache-T8/issues/1)).

You find out when the console scrolls past at startup. This tells you before.

The other shape does not even need a removal. Core adds a parameter, a pack
keeps calling with the old shape or keeps shipping its own replacement of the
function, and the workflow dies mid-run:

```
TypeError: calculate_weight() got an unexpected keyword argument 'intermediate_dtype'
```

That is [Comfy-Org/ComfyUI#5355](https://github.com/Comfy-Org/ComfyUI/issues/5355),
a pack shipping an outdated `calculate_weight` while core grew
`intermediate_dtype`. The same class hit `WanAttentionBlock.forward` /
`context_img_len` in
[#12134](https://github.com/Comfy-Org/ComfyUI/issues/12134) and
`patched_forward_orig` / `timestep_zero_index` in
[#13136](https://github.com/Comfy-Org/ComfyUI/issues/13136). Since 1.1.0 this
tool binds every direct `comfy.*` call site and monkeypatch against the real
parameter list at the target ref and reports the ones that cannot bind.

**Why grep does not work here.** PR #11632 deleted the module-level
`def precompute_freqs_cis(...)` and added a private `_precompute_freqs_cis`
*method* on the model class. The string `precompute_freqs_cis` still appears
twice in that file on master today. Any substring or grep-based checker reports
SAFE and is wrong. comfy-import-guard builds the importable-name set from
top-level `ast.FunctionDef` / `AsyncFunctionDef` / `ClassDef` / `Assign` /
`AnnAssign` / `ImportFrom` nodes only, so a function demoted to a method or
renamed with a leading underscore is correctly reported as gone.

## Install

```bash
pip install comfy-import-guard
```

Or as a ComfyUI node pack (it registers zero nodes; it adds one read-only
report route):

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/Booyaka101/comfy-import-guard
```

Needs Python 3.10+ and `git` on PATH. On first use it clones ComfyUI
(~1 min, anonymous, no token) into a cache directory:

| platform | default cache |
| --- | --- |
| Windows | `%LOCALAPPDATA%\comfy-import-guard\cache` |
| Linux / macOS | `$XDG_CACHE_HOME/comfy-import-guard` or `~/.cache/comfy-import-guard` |

Override with `--cache-dir` or `COMFY_IMPORT_GUARD_CACHE`.

## Usage

### `check`: will my install survive the next update?

```
$ comfy-import-guard check --comfy-dir D:/ComfyUI_windows_portable/ComfyUI
comfy-import-guard check
  install : D:\ComfyUI_windows_portable\ComfyUI\custom_nodes
  target  : origin/master (bd34f338a)

[ok] comfyui_controlnet_aux  SAFE
     667 python file(s), 86 comfy.* reference(s)

1 pack(s): 0 will break, 1 safe, 0 warn, 0 skipped; 0 breaking reference(s)
```

Against a set of packs with real breakage:

```
$ comfy-import-guard check --comfy-dir /scratch/comfy
comfy-import-guard check
  install : /scratch/comfy/custom_nodes
  target  : origin/master (bd34f338a)

[!!] ComfyUI-MagCache  WILL BREAK
     3 python file(s), 21 comfy.* reference(s)
       MISSING  comfy.ldm.lightricks.model.precompute_freqs_cis
                nodes.py:13  (from)
                removed by f2b002372 in PR #11632 on 2026-01-05
                last good v0.7.0, first bad v0.8.0
       MISSING  comfy.ldm.lightricks.model.precompute_freqs_cis
                nodes_calibration.py:13  (from)
                removed by f2b002372 in PR #11632 on 2026-01-05
                last good v0.7.0, first bad v0.8.0

[!!] ComfyUI-TeaCache  WILL BREAK
     8 python file(s), 12 comfy.* reference(s)
       MISSING  comfy.ldm.lightricks.model.precompute_freqs_cis
                nodes.py:12  (from)
                removed by f2b002372 in PR #11632 on 2026-01-05
                last good v0.7.0, first bad v0.8.0

[??] comfyui-minimax-h3-blockcache-T8  WARN
     5 python file(s), 34 comfy.* reference(s)
       SOFT     comfy.ldm.minimax.model.time_shift_slope  nodes.py:15 (guarded by try/except)
       SHIM     comfy.model_prefetch.cleanup_prefetched_modules  nodes.py:96
                missing required argument 'comfy_modules'; another call to it in this file binds, so this looks like a version shim
     note: 1 guarded reference(s) that would fail; 1 version-shim call(s) that do not bind here

3 pack(s): 2 will break, 0 safe, 1 warn, 0 skipped; 3 breaking reference(s)
Run `comfy-import-guard blame <module.Symbol>` for the commit that removed it.
```

Exit code is 1 when anything will break, so it drops straight into CI.

`--target` takes any ref: a tag (`v0.31.0`), a sha, or `origin/master` (default).
Check what a specific update will do to you before you take it.

### Signature checks

Real output against ComfyUI-Easy-Use at master today (import rows elided):

```
[!!] ComfyUI-Easy-Use  WILL BREAK
     99 python file(s), 449 comfy.* reference(s)
       ...
       BADCALL  comfy.ops.pick_operations
                py/modules/brushnet/__init__.py:676  (call)
                unexpected keyword argument 'scaled_fp8'
                upstream accepts (weight_dtype, compute_dtype, load_device=..., disable_fast_fp8=..., fp8_optimizations=..., model_config=...)
                parameter 'scaled_fp8' removed by 43071e3de in PR #11000 on 2025-12-05
                last good v0.3.77, first bad v0.4.0
       SIGDRIFT comfy.clip_vision.load_clipvision_from_sd  py/modules/kolors/loader.py:282
                replacement drops 'sd', 'prefix', 'convert_keys' present upstream
```

`BADCALL` is a call that cannot bind at the target ref: an unknown keyword
with no `**kwargs`, too many positionals with no `*args`, or a now-required
parameter missing. It raises TypeError the moment it runs, so it counts toward
WILL BREAK. `SIGDRIFT` is a monkeypatch replacement that no longer accepts a
parameter the upstream original has. Whether core passes that argument on your
path is not statically decidable, so it grades WARN, not a break. Both name
the commit that moved the signature, same as removals do.

A pack that supports several ComfyUI versions calls the same function with one
arity per branch behind a `hasattr` probe. Exactly one branch binds at any
given ref, so the other is dead code there, not a bug. Those are reported as
`SHIM` under WARN rather than failing the build, as in the T8 pack above.

Measured before shipping on 20 real popular packs (Impact-Pack, KJNodes,
Manager, WAS suite, IPAdapter_plus, VideoHelperSuite and friends: 1,273 Python
files, 1,116 checkable `comfy.*` call sites, 10 monkeypatches): 2 findings,
both true on hand-verification against the pack and ComfyUI source, 18 of 20
packs silent. The `pick_operations` row above is one of the two, a live
TypeError in Easy-Use's BrushNet path.

Silent by design: calls spreading `*args`/`**kwargs`, decorated targets or
replacements, `functools.partial`, names the file rebinds to something else,
calls guarded by `except TypeError`, and anything the alias machinery cannot
resolve. `--no-signatures` turns the whole pass off.

### `blame --param`: who moved this parameter?

```
$ comfy-import-guard blame comfy.lora.calculate_weight --param intermediate_dtype
comfy.lora.calculate_weight  parameter 'intermediate_dtype'
  added         : c26ca2720
  commit        : Move calculate function to comfy.lora
  changed on    : 2024-08-22T17:12:00-04:00
  last good tag : v0.1.0
  first bad tag : v0.1.1
```

Same walk as plain `blame`, but presence means "is a parameter of that def"
rather than "is bound at module scope". It takes a class path too, so
`blame comfy.ldm.wan.model.WanAttentionBlock.forward --param context_img_len`
answers the #12134 question directly.

### `blame`: who removed this symbol?

```
$ comfy-import-guard blame comfy.ldm.minimax.model.time_shift_slope
comfy.ldm.minimax.model.time_shift_slope
  (from ledger; pass --no-ledger to re-derive from git)
  removed in    : bdcb886a4
  commit        : Fix sampler issues for audio with minimax, support more samplers. (#15243)
  pull request  : Comfy-Org/ComfyUI#15243
                  https://github.com/comfyanonymous/ComfyUI/pull/15243
  removed on    : 2026-08-06T13:36:34-07:00
  last good tag : v0.30.2
  first bad tag : v0.31.0
  known packs   : comfyui-minimax-h3-blockcache-T8
```

The shipped `ledger.json` answers instantly and offline for known removals.
Anything not in it is derived from git and can be written back with `--record`:

```
$ comfy-import-guard blame comfy.ldm.lightricks.model.precompute_freqs_cis --no-ledger
comfy.ldm.lightricks.model.precompute_freqs_cis
  removed in    : f2b002372
  commit        : Support the LTXV 2 model. (#11632)
  pull request  : Comfy-Org/ComfyUI#11632
                  https://github.com/comfyanonymous/ComfyUI/pull/11632
  removed on    : 2026-01-05T01:58:59-05:00
  last good tag : v0.7.0
  first bad tag : v0.8.0
  introduced in : 5e16f1d24  (2024-11-22)
```

### `derive-requires`: what should my pyproject claim?

For pack authors. Finds the oldest ComfyUI release in which every symbol your
pack references already exists, and the first release in which one of them stops
existing.

```
$ comfy-import-guard derive-requires ./ComfyUI-TeaCache
derive-requires: ComfyUI-TeaCache
  8 python file(s), 11 hard comfy.* reference(s)
  already removed at head:
    comfy.ldm.lightricks.model.precompute_freqs_cis  (nodes.py:12)
  floor set by  : comfy.ldm.flux.layers.apply_mod
  probed 8 release tag(s)

Paste under [tool.comfy] in the pack's pyproject.toml:

  requires-comfyui = ">=0.3.25,<0.8.0"
```

The upper bound only appears when the pack references something that is already
gone. A healthy pack gets a plain floor:

```
$ comfy-import-guard derive-requires ./tests/packs/recent_pack
derive-requires: recent_pack
  1 python file(s), 2 hard comfy.* reference(s)
  floor set by  : comfy.ldm.minimax, comfy.ldm.minimax.model
  probed 8 release tag(s)

Paste under [tool.comfy] in the pack's pyproject.toml:

  requires-comfyui = ">=0.30.0"
```

`requires-comfyui` is the [Comfy Registry field](https://docs.comfy.org/registry/specifications)
that tells ComfyUI-Manager which ComfyUI versions your node supports.

### HTTP route

Installed as a node pack, it adds one read-only route:

```
GET /comfy_import_guard/report?target=origin/master
```

It returns the same JSON as `check --json` for the running install. It is
deliberately offline: it uses whatever clone the CLI already made and never
downloads anything from inside the server process. If no clone exists yet it
returns `{"ok": false, "hint": "..."}` telling you which command to run once.

## Configuration

| flag | effect |
| --- | --- |
| `--comfy-dir` | ComfyUI install root, or a `custom_nodes` directory directly |
| `--target` | ref to resolve against (tag, sha, `origin/master`) |
| `--pack NAME` | check only these packs (repeatable) |
| `--param NAME` | on `blame`: attribute a parameter of the symbol instead of the symbol |
| `--cache-dir` | where the ComfyUI clone lives |
| `--ledger` | alternate `ledger.json` |
| `--no-signatures` | skip call-site and monkeypatch signature checks |
| `--offline` | never touch the network; answer from the existing clone and the ledger |
| `--no-update` | skip the `git fetch` before checking |
| `--json` | machine-readable output for every command |
| `--quiet` | suppress progress notes on stderr |

Global flags work before or after the subcommand.

### Verdicts

| verdict | meaning |
| --- | --- |
| `SAFE` | every reference resolves and every checkable call binds at the target ref |
| `WILL BREAK` | an unguarded reference is gone (`MISSING`), or a call cannot bind (`BADCALL`); exit code 1 |
| `WARN` | only guarded (`try/except`) references fail, a monkeypatch is behind the upstream signature (`SIGDRIFT`), a call is a version shim (`SHIM`), or something could not be resolved statically |
| `SKIPPED` | the pack vendors its own `comfy/` package, so it resolves pack-locally |

## How it works

1. `ast.walk` every `.py` in each pack. Collect `from comfy.… import x`, plain
   `import comfy.x.y as z` plus attribute chains rooted at those aliases, and
   `getattr(comfy.x, "literal")`. Also collect every direct call into those
   chains (positional count, keyword names, `*`/`**` spreads) and every
   monkeypatch assignment whose replacement is a function or lambda defined in
   the same file.
2. `git show <ref>:comfy/…/model.py` for each referenced module, parse it, and
   build the set of names bound at module scope.
3. Anything referenced but not bound is a break. `git log -S'\bsymbol\b'
   --pickaxe-regex` finds the commit that changed it; `git tag --contains` turns
   that into a release boundary.
4. For call sites and monkeypatches, resolve the real parameter list at the
   ref (through one class level, so `WanAttentionBlock.forward` works, and
   against `__init__` when the target is a class), try to bind the call
   against it, and diff the replacement's parameters against it. The same
   pickaxe walk then names the commit where the signature changed.

Only the public ComfyUI git repository is used. No API, no token, no account.

## Limitations

- **Static only.** It never imports a pack and never runs one. A pack that
  builds an import name at runtime out of non-literal strings is invisible to it.
- **`from comfy.x import *`** is reported as unresolvable, not guessed.
- **Attribute chains are best-effort.** `comfy.samplers.KSampler.SAMPLERS`
  is checked as far as `KSampler`; class internals are not tracked.
- **Local shadowing is not modelled for references.** A local variable that
  happens to reuse an alias name can produce a spurious reference. It shows up
  as `WARN`/`MISSING` with a file and line, so it is cheap to dismiss. Call
  sites are held to a stricter rule, because a bad bind is a hard failure: any
  name the file rebinds is dropped from signature checking entirely.
- **Signature checks cover parameter lists, not behaviour.** A changed return
  type, a changed default value, or changed semantics behind an unchanged
  parameter list are all still invisible. Calls that spread `*args`/`**kwargs`,
  decorated targets or replacements, `functools.partial`, re-exported names and
  calls on instances (rather than through the module or class) are deliberately
  silent: a wrong TypeError prediction is worse than a missed one.
- **Files this interpreter cannot parse are counted and printed**, never
  silently skipped. If you see `UNPARSED`, the pack uses syntax newer than your
  Python and that file was not analysed.
- **Not a dependency checker.** pip conflicts belong to ComfyUI-Manager. No
  auto-fixing, no runtime import hooks, no model downloads.

## Tests

```bash
pip install pytest
python -m pytest tests -q
```

117 tests. They assert against live public ComfyUI history rather than recorded
fixtures: the real commits `f2b002372` and `bdcb886a4`, the real tags
`v0.7.0`/`v0.8.0` and `v0.30.2`/`v0.31.0`, and for the signature checks the
real parameter additions behind issues #5355 and #12134 (`c26ca2720` and
`0d720e436`). They need `git` and a one-time clone, and skip cleanly if
neither is available.

## Publishing

`pyproject.toml` is already registry-shaped. Before `comfy node publish`, set
`[tool.comfy] PublisherId` to your Comfy Registry publisher ID (the placeholder
is a GitHub handle, not a verified publisher ID) and confirm the `Icon` URL
resolves once the repo is public.

## License

MIT
