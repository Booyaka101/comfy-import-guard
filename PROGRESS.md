# PROGRESS: comfy-import-guard

Status: **v1.0.0 complete and verified end to end.** Not published (owner ships it).

Date: 2026-08-12

## Phase 0: resource verification (all passed)

| resource | verified |
| --- | --- |
| Comfy-Org/ComfyUI#11660 | open; `ImportError: cannot import name 'precompute_freqs_cis' from 'comfy.ldm.lightricks.model'`; TeaCache + MagCache named |
| T8mars/comfyui-minimax-h3-blockcache-T8#1 | `time_shift_slope` from `comfy.ldm.minimax.model`; LukeG89 2026-08-06; names PR #15243 |
| docs.comfy.org/registry/specifications | `requires-comfyui` under `[tool.comfy]`, optional, operators `< > <= >= ~= <> !=` and ranges |
| docs.comfy.org/registry/publishing | `comfy node publish`; `Comfy-Org/publish-node-action@main` with `personal_access_token: secrets.REGISTRY_ACCESS_TOKEN`; required pyproject fields |
| docs.comfy.org/registry/overview | Registry powers ComfyUI-Manager |
| github.com/comfyanonymous/ComfyUI | cloned anonymously, full history, 175 release tags |
| Comfy-Org/comfy-cli | `comfy node bisect` bisects *installed custom nodes*, not ComfyUI history; no import-compat command |

Cost model: no paid API, account, key or hosting. Anonymous git clone only.

Ground truth re-derived from the live repo, not taken on faith:

```
f2b002372b71cf0671a4cf1fa539e1c386d727e4  "Support the LTXV 2 model. (#11632)"        2026-01-05
  diff:  -def precompute_freqs_cis(indices_grid, dim, out_dtype, ...)
         +    def _precompute_freqs_cis(          <- private CLASS METHOD
  tags:  last good v0.7.0, first bad v0.8.0

bdcb886a4705a03cf40f4a7226de9fc7c059fc90  "Fix sampler issues for audio with minimax... (#15243)"  2026-08-06
  tags:  last good v0.30.2, first bad v0.31.0
```

## What is VERIFIED working

- `check` against the real local ComfyUI 0.19.3 install: 1 pack, 667 python files,
  86 comfy.* references, 0 unparseable, verdict SAFE, exit 0. Loose `.py`,
  `.py.example` and `__pycache__` correctly not treated as packs.
- `check` against a scratch custom_nodes holding the three ground-truth packs:
  MagCache + TeaCache `WILL BREAK` on `precompute_freqs_cis` with attribution,
  T8 pack `WARN` (its author has since wrapped the import in try/except, which
  the tool correctly grades SOFT rather than breaking). Exit 1.
- Worked example 2 reproduced exactly: `--target f2b002372^` → both packs SAFE,
  exit 0; `--target f2b002372` → both WILL BREAK, exit 1.
- `blame` for both seeds reproduces commit, PR, date and both tag boundaries.
- `derive-requires` on TeaCache → `>=0.3.25,<0.8.0`; on the recent fixture →
  `>=0.30.0`; on the ancient fixture → `>=0.0.1`.
- Ledger short-circuit answers with a repo object whose every method raises.
- HTTP route driven for real under aiohttp with a stand-in `PromptServer`:
  registered True, HTTP 200, full JSON report.
- `pip install .` into a bare venv: only `comfy-import-guard` resolved
  (plus venv's own pip/setuptools). Console script and packaged ledger work
  from a clean cwd.
- `import comfy_import_guard` with no `server` module: clean, `_ROUTE_REGISTERED
  = False`.
- 59 tests pass: `python -m pytest tests -q`.
- Every failure path prints a message and exits 2, never a traceback: missing
  dir, no custom_nodes, bad ref, `--offline` with no clone, unreachable remote,
  corrupt ledger, invented module, missing pack dir.

## Non-obvious things future work must not undo

1. **`git log -S<pat> --pickaxe-regex` is POSIX ERE plus GNU extensions, so a
   lookbehind matches nothing, silently.** The first implementation used
   `(?<![A-Za-z0-9_])sym(?![A-Za-z0-9_])` and returned zero commits for both
   seeds. `\bsym\b` works. See `repo.pickaxe`.
2. **A plain substring `-S` misses the real removal shape.** `git log -S"precompute_freqs_cis"`
   on `comfy/ldm/lightricks/model.py` does NOT list f2b002372, because that
   commit deleted one occurrence and added two (`_precompute_freqs_cis`), so
   the count is unchanged. Word-anchoring is what makes it visible.
3. **ComfyUI's `comfy/` has no `__init__.py`.** A file-only module test calls
   every `comfy.<submodule>` reference missing. `Repo.is_dir` handles it.
4. **`from a.b import c` names its module unambiguously**; only attribute chains
   need longest-prefix backoff. Mixing the two reported `comfy.ldm.minimax` as a
   missing *symbol* instead of the module being absent.
5. **Exported names come from top-level AST nodes only.** Descending into a
   ClassDef would make `_precompute_freqs_cis` look importable. Descending into
   top-level `if`/`try` bodies is correct and is done.

## Left undone (deliberate)

- Not published. No PyPI upload, no `comfy node publish`, no repo pushed. The
  owner ships from the phone.
- `[tool.comfy] PublisherId` is set to the GitHub handle `booyaka101`. That is
  the one value that must be confirmed against the Comfy Registry account before
  publishing; the README says so.
- The `Icon` URL points at `raw.githubusercontent.com/Booyaka101/comfy-import-guard/main/icon.svg`.
  `icon.svg` is real and in the repo, but the URL only resolves after the repo is
  public under that name.

## Next steps if resumed

1. Push to `github.com/Booyaka101/comfy-import-guard`, confirm the icon URL 200s.
2. Set the real `PublisherId`, then `comfy node publish` (or add the
   `REGISTRY_ACCESS_TOKEN` secret and let `.github/workflows/publish-registry.yml` run).
3. `python -m build && twine upload dist/*` for PyPI.
4. Distribution: comment the derived compatibility range on the two open issues
   (`Comfy-Org/ComfyUI#11660`, `T8mars/...#1`). The affected users are already
   subscribed there. Owner's own voice per CLAUDE.md.
