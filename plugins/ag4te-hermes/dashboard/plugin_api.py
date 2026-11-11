"""AG4TE REST API, mounted by Hermes at /api/plugins/ag4te/."""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query

router = APIRouter()


_load_lock = threading.Lock()
PLUGIN_ID = "ag4te"
_PRIVATE = "_ag4te_private"


class _HostConfig:
    """Stand-in for the plugin context when Hermes did not hand this copy one.

    It answers get_config from the user's saved plugin settings
    (plugins.entries.ag4te.settings in the Hermes config), so a private copy
    honours the same repo_roots and timeouts as the registered plugin.
    """

    def __init__(self) -> None:
        self.settings: dict = {}

    def get_config(self, key, default=None):
        val = self.settings.get(key)
        return default if val is None else val


def _unreadable(exc: BaseException) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=f"AG4TE cannot read its settings from Hermes ({exc}); not using defaults",
    )


def _read_host_settings() -> dict:
    """The user's saved settings, {} outside Hermes; 503 when Hermes is here but unreadable."""
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore
    except ModuleNotFoundError as exc:
        if exc.name == "hermes_cli" or sys.modules.get("hermes_cli", 0) is None:
            return {}  # no Hermes host (tests, standalone): the plugin defaults stand
        raise _unreadable(exc) from exc
    except ImportError as exc:  # Hermes is here but its loader is not: never fall back silently
        raise _unreadable(exc) from exc
    try:
        entry = ((load_config_readonly() or {}).get("plugins") or {}).get("entries", {}).get(
            PLUGIN_ID
        ) or {}
        return dict(entry.get("settings") or {})
    except Exception as exc:  # noqa: BLE001
        raise _unreadable(exc) from exc


def _plugin():
    # Prefer the module Hermes registered (its _ctx resolves the user's settings). Hermes gives
    # secondary profiles a __home_<digest> module suffix, so find it by this plugin's source file,
    # not by name. Failing that, one private copy is loaded under a lock and fed the saved settings;
    # the running-dig registry is process-wide (see _job_registry), so a second copy sees the same digs.
    target = (Path(__file__).resolve().parent.parent / "__init__.py").resolve()
    fallback = None
    for mod in list(sys.modules.values()):
        filename = getattr(mod, "__file__", None)
        if not filename or not hasattr(mod, "start_dig"):
            continue
        try:
            if Path(filename).resolve() != target:
                continue
        except OSError:
            continue
        if getattr(mod, "_ctx", None) is not None:
            return mod
        fallback = fallback or mod
    if fallback is None:
        with _load_lock:
            fallback = sys.modules.get(_PRIVATE)
            if fallback is None or not hasattr(fallback, "start_dig"):
                fallback = _load_private()
    return _with_settings(fallback)


def _with_settings(mod):
    """Give a module that has no registered context the user's saved settings."""
    ctx = getattr(mod, "_ctx", None)
    if ctx is None:
        ctx = _HostConfig()
        mod._ctx = ctx
    if isinstance(ctx, _HostConfig):
        ctx.settings = _read_host_settings()
    return mod


def _load_private():
    pkg_dir = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(_PRIVATE, pkg_dir / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_PRIVATE] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(_PRIVATE, None)
        raise
    return mod


def _bad(exc: Exception, code: int = 400):
    raise HTTPException(status_code=code, detail=str(exc))


@router.get("/projects")
def projects():
    return {"projects": _plugin().list_projects()}


@router.get("/repos")
def repos():
    p = _plugin()
    return {"roots": [str(r) for r in p.repo_roots()], "repos": p.candidate_repos()}


@router.get("/dig")
def dig(project: str = Query(...)):
    p = _plugin()
    try:
        d = p.load_dig(p.validate_project(project))
    except p.DigError as exc:
        _bad(exc)
    if d is None:
        raise HTTPException(status_code=404, detail=f"no finished dig named {project}")
    return d


@router.get("/status")
def status(project: str = Query(...)):
    p = _plugin()
    try:
        return p.read_status(p.validate_project(project))
    except p.DigError as exc:
        _bad(exc)


@router.post("/dig")
def start(body: dict = Body(...)):
    p = _plugin()
    repo, project = body.get("repo"), body.get("project") or None
    try:
        return p.start_dig(repo, project, resume=bool(body.get("resume")))
    except p.DigError as exc:
        _bad(exc, 409 if isinstance(exc, p.DigBusyError) else 400)


@router.post("/cancel")
def cancel(body: dict = Body(...)):
    p = _plugin()
    try:
        project = p.validate_project(body.get("project") or "")
    except p.DigError as exc:
        _bad(exc)
    try:
        return p.cancel_dig(project)
    except p.DigError as exc:
        _bad(exc, 409)
