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

import adguard
import discovery
import frigate
import motion_bridge
import provisioner
import remover
from models import CameraInfo, DiscoverRequest, ProvisionRequest, RemoveRequest

_HERE = Path(__file__).parent
_FRIGATE_URL = f"http://localhost:{os.environ.get('FRIGATE_PORT', '5000')}"

app = FastAPI(title="eKaza Wizard", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

_bridge: motion_bridge.BridgeManager | None = None


@app.on_event("startup")
async def _startup():
    global _bridge
    _bridge = motion_bridge.BridgeManager(_FRIGATE_URL)
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


@app.get("/api/cameras")
async def api_cameras():
    """Return list of camera slugs currently configured in Frigate."""
    try:
        frigate_slug = await provisioner._get_frigate_slug()
    except RuntimeError:
        return {"cameras": []}
    cameras = await remover.list_cameras(frigate_slug)
    return {"cameras": cameras}


@app.post("/api/remove")
async def api_remove(req: RemoveRequest):
    """SSE stream — removes selected cameras from Frigate and LocalTuya."""
    async def _stream() -> AsyncGenerator[str, None]:
        try:
            frigate_slug = await provisioner._get_frigate_slug()
        except RuntimeError as e:
            import json
            yield f"data: {json.dumps({'step':'init','name':'global','ok':False,'detail':str(e)})}\n\n"
            return
        async for chunk in remover.remove(req.cameras, frigate_slug):
            yield chunk

    return EventSourceResponse(_stream())


@app.get("/api/adguard/status")
async def api_adguard_status():
    accessible, msg = await adguard.check_accessible()
    status = await adguard.get_status()
    return {**status, "accessible": accessible, "message": msg}


@app.post("/api/adguard/enable")
async def api_adguard_enable():
    ok, detail = await adguard.add_block_rules()
    return {"ok": ok, "detail": detail}


@app.post("/api/adguard/disable")
async def api_adguard_disable():
    ok, detail = await adguard.remove_block_rules()
    return {"ok": ok, "detail": detail}


@app.get("/api/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7788))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
