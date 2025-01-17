#! /usr/bin/env python3
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Set

import uvicorn
from fastapi import Body, FastAPI, Response, status
from fastapi.staticfiles import StaticFiles
from fastapi_versioning import VersionedFastAPI, version
from loguru import logger
from starlette.responses import Response as StarletteResponse

from ArduPilotManager import ArduPilotManager
from mavlink_proxy.Endpoint import Endpoint

FRONTEND_FOLDER = Path.joinpath(Path(__file__).parent.absolute(), "frontend")


class InterceptHandler(logging.Handler):
    def emit(self, record) -> None:  # type: ignore
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


logging.basicConfig(handlers=[InterceptHandler()], level=0)


class PrettyJSONResponse(StarletteResponse):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            separators=(", ", ": "),
        ).encode(self.charset)


app = FastAPI(
    title="ArduPilot Manager API",
    description="ArduPilot Manager is responsible for managing ArduPilot devices connected to Companion.",
    default_response_class=PrettyJSONResponse,
    debug=True,
)
logger.info("Starting ArduPilot Manager.")
autopilot = ArduPilotManager()


@app.get("/endpoints", response_model=List[Dict[str, Any]])
@version(1, 0)
def get_available_endpoints() -> Any:
    return [endpoint.asdict() for endpoint in autopilot.get_endpoints()]


@app.post("/endpoints", status_code=status.HTTP_201_CREATED)
@version(1, 0)
def create_endpoints(response: Response, endpoints: Set[Endpoint] = Body(...)) -> Any:
    try:
        autopilot.add_new_endpoints(endpoints)
    except ValueError as error:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"message": f"{error}"}


@app.delete("/endpoints", status_code=status.HTTP_200_OK)
@version(1, 0)
def remove_endpoints(response: Response, endpoints: Set[Endpoint] = Body(...)) -> Any:
    try:
        autopilot.remove_endpoints(endpoints)
    except ValueError as error:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"message": f"{error}"}


app = VersionedFastAPI(app, version="1.0.0", prefix_format="/v{major}.{minor}", enable_latest=True)
app.mount("/", StaticFiles(directory=str(FRONTEND_FOLDER), html=True))


if __name__ == "__main__":
    autopilot.run()
    # Running uvicorn with log disabled so loguru can handle it
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)
