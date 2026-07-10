"""eKaza Wizard — FastAPI entrypoint."""
import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

import discovery
import frigate
import motion_bridge
import provisioner
from models import CameraInfo, DiscoverRequest, ProvisionRequest

_HERE = Path(__file__).parent
_HA_HOST    = os.environ.get("HA_HOST", "192.168.15.35")
_FRIGATE_PORT = os.environ.get("FRIGATE_PORT", "5000")

app = FastAPI(title="eKaza Wizard", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

_bridge: motion_bridge.BridgeManager | None = None


@app.on_event("startup")
async def _startup():
    global _bridge
    _bridge = motion_bridge.BridgeManager(f"http://{_HA_HOST}:{_FRIGATE_PORT}")
    await _bridge.start()


@app.on_event("shutdown")
async def _shutdown():
    if _bridge:
        await _bridge.stop()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Inject <base> tag so relative fetch() calls work through HA ingress proxy
    ingress_path = request.headers.get("X-Ingress-Path", "")
    base_tag = f'<base href="{ingress_path}/">' if ingress_path else ""
    html = (_HERE / "static" / "index.html").read_text()
    return html.replace("<title>", f"{base_tag}<title>", 1)


@app.get("/api/config")
async def api_config():
    """Return saved add-on options so the wizard UI can pre-populate its form."""
    return {
        "tuya_access_id":     os.environ.get("TUYA_ACCESS_ID", ""),
        "tuya_access_secret": os.environ.get("TUYA_ACCESS_SECRET", ""),
        "tuya_region":        os.environ.get("TUYA_REGION", "us"),
        "rtsp_password":      os.environ.get("RTSP_PASSWORD", ""),
        "rtsp_username":      "admin",
    }


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
        # Notify bridge of newly provisioned cameras
        if _bridge:
            _bridge.notify_provisioned([c.model_dump() for c in req.cameras])

    return EventSourceResponse(_stream())


@app.get("/api/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7788))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
