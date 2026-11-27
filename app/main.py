from __future__ import annotations

import inspect
from functools import wraps
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import RuleConfigurationError
from app.core import paths
from app.core.constants import APP_TITLE, APP_VERSION
from app.core.dependencies import STATE_LOCK
from app.core.exceptions import ApplicationError
from app.repositories.json_repository import (
    read,
    write,
    write_atomically,
    write_json_transaction,
)
from app.schemas import COMPATIBILITY_SCHEMAS
from app.controllers import ROUTERS
from app.services import COMPATIBILITY_MODULES

ROOT = paths.ROOT
DATA = paths.DATA
STATIC = paths.STATIC
SAVED_MAPS = paths.SAVED_MAPS
GAME_STATE = paths.GAME_STATE

app = FastAPI(title=APP_TITLE, version=APP_VERSION)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
for router in ROUTERS:
    app.include_router(router)


@app.exception_handler(RuleConfigurationError)
async def handle_rule_configuration_error(request, error: RuleConfigurationError):
    return JSONResponse(status_code=500, content={"detail": str(error)})


@app.exception_handler(ApplicationError)
async def handle_application_error(request, error: ApplicationError):
    return JSONResponse(
        status_code=error.status_code, content={"detail": error.detail}
    )


_compatibility_functions = {
    name: value
    for module in COMPATIBILITY_MODULES
    for name, value in vars(module).items()
    if inspect.isfunction(value) and value.__module__ == module.__name__
}


def _legacy_proxy(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        paths.configure_paths(
            data=DATA, saved_maps=SAVED_MAPS, game_state=GAME_STATE
        )
        try:
            return function(*args, **kwargs)
        except ApplicationError as error:
            raise HTTPException(error.status_code, error.detail) from error

    return wrapped


def __getattr__(name: str) -> Any:
    if name in _compatibility_functions:
        return _legacy_proxy(_compatibility_functions[name])
    if name in COMPATIBILITY_SCHEMAS:
        return COMPATIBILITY_SCHEMAS[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
