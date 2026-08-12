"""comfy-import-guard.

Doubles as a ComfyUI custom-node pack. It registers no nodes; it adds one
read-only HTTP route that reports which installed packs will break against a
ComfyUI ref. Every ComfyUI-specific import is guarded so that a plain
``pip install comfy-import-guard`` outside ComfyUI imports cleanly.
"""

__version__ = "1.0.0"

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

ROUTE = "/comfy_import_guard/report"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "__version__", "ROUTE"]


def _build_report(target, comfy_dir):
    """Synchronous check; run off the event loop by the route handler."""
    from .ledger import Ledger
    from .report import check
    from .repo import Repo

    repo = Repo(offline=True, quiet=True)
    if not (repo.path / ".git").exists():
        return {
            "ok": False,
            "error": "no local ComfyUI clone yet",
            "hint": (
                "Run `comfy-import-guard check --comfy-dir %s` once from a terminal. "
                "It clones ComfyUI into %s; this route stays offline and never "
                "downloads anything itself." % (comfy_dir, repo.path)
            ),
        }
    repo.ensure(deep=False)
    report = check(repo, comfy_dir, target, Ledger())
    report["ok"] = True
    return report


def _locate_comfy_dir():
    """Find the running ComfyUI root.

    ``folder_paths`` sits next to main.py in every ComfyUI layout. If it is not
    importable (a pip install being poked at by hand), walk up from this file
    looking for the directory that owns custom_nodes.
    """
    import os
    import sys

    try:
        import folder_paths
        return os.path.dirname(os.path.abspath(folder_paths.__file__))
    except Exception:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        parent = os.path.dirname(here)
        if os.path.isdir(os.path.join(parent, "custom_nodes")):
            return parent
        if os.path.basename(here) == "custom_nodes":
            return parent
        here = parent
    return os.path.abspath(sys.path[0] or os.getcwd())


def _register_routes():
    """Attach the report route to ComfyUI's aiohttp server, if we are inside one."""
    import asyncio
    import os

    from aiohttp import web
    from server import PromptServer

    if PromptServer.instance is None:
        return False

    comfy_dir = _locate_comfy_dir()

    @PromptServer.instance.routes.get(ROUTE)
    async def comfy_import_guard_report(request):
        target = request.query.get("target", "origin/master")
        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, _build_report, target, comfy_dir)
        except Exception as exc:  # never take the server down
            return web.json_response(
                {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, status=200
            )
        return web.json_response(data)

    return True


try:
    _ROUTE_REGISTERED = _register_routes()
except Exception:
    # No ComfyUI, no aiohttp, or a ComfyUI whose server module has moved.
    # A pip-only install must import cleanly regardless.
    _ROUTE_REGISTERED = False
