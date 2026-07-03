"""eKaza Wizard — FastAPI entrypoint."""
import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

import discovery
import frigate
import provisioner
from models import CameraInfo, DiscoverRequest, ProvisionRequest

_HERE = Path(__file__).parent
app = FastAPI(title="eKaza Wizard", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (_HERE / "static" / "index.html").read_text()


@app.post("/api/discover")
async def api_discover(req: DiscoverRequest):
    try:
        cameras = await discovery.discover(req.credentials)
        return {"cameras": [c.model_dump() for c in cameras]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/frigate-snippet")
async def api_frigate_snippet(cameras: list[CameraInfo]):
    """Return Frigate YAML snippet for manual paste (fallback)."""
    return {"snippet": frigate.get_snippet(cameras)}


@app.post("/api/provision")
async def api_provision(req: ProvisionRequest):
    """SSE stream — one event per provisioning step."""
    async def _stream() -> AsyncGenerator[str, None]:
        async for chunk in provisioner.provision_all(req.cameras):
            yield chunk

    return EventSourceResponse(_stream())


@app.get("/api/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7788))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
