"""eKaza Wizard — 1-click PTZ camera setup for Home Assistant."""
from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web
from aiohttp.web_response import StreamResponse
from homeassistant.components.frontend import (
    async_register_built_in_panel,
    async_remove_panel,
)
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import adguard as adguard_mod
from . import check as check_mod
from . import discovery, motion_bridge, provisioner, remover
from .dashboard import list_dashboards
from .const import (
    CONF_RTSP_PASSWORD,
    CONF_TUYA_ACCESS_ID,
    CONF_TUYA_ACCESS_SECRET,
    CONF_TUYA_REGION,
    DOMAIN,
)
from .models import CameraInfo, ProvisionRequest, TuyaCredentials

_LOGGER = logging.getLogger(__name__)
_STATIC = Path(__file__).parent / "static"
PLATFORMS: list[str] = []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["entry"] = entry

    html = await hass.async_add_executor_job(
        (_STATIC / "index.html").read_text, "utf-8"
    )
    hass.http.register_view(EkazaIndexView(html))
    hass.http.register_view(EkazaConfigView(entry))
    hass.http.register_view(EkazaDiscoverView(hass, entry))
    hass.http.register_view(EkazaProvisionView(hass, entry))
    hass.http.register_view(EkazaCheckView(hass))
    hass.http.register_view(EkazaListCamerasView(hass))
    hass.http.register_view(EkazaRemoveView(hass))
    hass.http.register_view(EkazaDashboardsView(hass))
    hass.http.register_view(EkazaAdGuardCheckView(hass))
    hass.http.register_view(EkazaAdGuardStatusView(hass))
    hass.http.register_view(EkazaSmartLifeUnblockView(hass))
    hass.http.register_view(EkazaSmartLifeBlockView(hass))

    from . import frigate_syncer
    hass.loop.create_task(frigate_syncer.setup(hass))

    try:
        async_register_built_in_panel(
            hass,
            component_name="iframe",
            sidebar_title="eKaza Wizard",
            sidebar_icon="mdi:cctv",
            frontend_url_path="ekaza-wizard",
            config={"url": "/api/ekaza_wizard/"},
            require_admin=True,
        )
    except ValueError:
        pass  # panel already registered from a previous setup
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    motion_bridge.stop()
    async_remove_panel(hass, "ekaza-wizard")
    return True


def _build_creds(data: dict) -> TuyaCredentials | None:
    if not data.get(CONF_TUYA_ACCESS_ID):
        return None
    return TuyaCredentials(
        access_id=data[CONF_TUYA_ACCESS_ID],
        access_secret=data[CONF_TUYA_ACCESS_SECRET],
        region=data.get(CONF_TUYA_REGION, "us"),
        default_rtsp_password=data.get(CONF_RTSP_PASSWORD, ""),
    )


class EkazaIndexView(HomeAssistantView):
    url = "/api/ekaza_wizard/"
    name = "api:ekaza_wizard:index"
    requires_auth = False

    def __init__(self, html: str) -> None:
        self._html = html

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text=self._html, content_type="text/html", charset="utf-8")


class EkazaConfigView(HomeAssistantView):
    url = "/api/ekaza_wizard/config"
    name = "api:ekaza_wizard:config"
    requires_auth = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def get(self, request: web.Request) -> web.Response:
        d = self._entry.data
        return self.json({
            "tuya_access_id": d.get(CONF_TUYA_ACCESS_ID, ""),
            "tuya_access_secret": d.get(CONF_TUYA_ACCESS_SECRET, ""),
            "tuya_region": d.get(CONF_TUYA_REGION, "us"),
            "rtsp_password": d.get(CONF_RTSP_PASSWORD, ""),
        })


class EkazaDiscoverView(HomeAssistantView):
    url = "/api/ekaza_wizard/discover"
    name = "api:ekaza_wizard:discover"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry

    async def post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}

        override = body.get("credentials", {})
        creds = _build_creds(override) or _build_creds(self._entry.data)
        if not creds:
            return self.json({"error": "Credenciais Tuya obrigatórias"}, status_code=400)

        try:
            cameras = await discovery.discover(creds)
            return self.json({"cameras": [c.model_dump() for c in cameras]})
        except Exception as exc:
            _LOGGER.exception("Discovery failed")
            return self.json({"error": str(exc)}, status_code=500)


class EkazaCheckView(HomeAssistantView):
    url = "/api/ekaza_wizard/check"
    name = "api:ekaza_wizard:check"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        results = await check_mod.run(self._hass)
        return self.json(results)


class EkazaListCamerasView(HomeAssistantView):
    url = "/api/ekaza_wizard/cameras"
    name = "api:ekaza_wizard:cameras"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        cameras = await remover.list_cameras(self._hass)
        return self.json({"cameras": cameras})


class EkazaRemoveView(HomeAssistantView):
    url = "/api/ekaza_wizard/remove"
    name = "api:ekaza_wizard:remove"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> StreamResponse:
        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "JSON inválido"}, status_code=400)

        slug = body.get("slug", "").strip()
        if not slug:
            return self.json({"error": "'slug' é obrigatório"}, status_code=422)

        device_id = body.get("device_id") or None

        response = StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        async for chunk in remover.remove(self._hass, slug, device_id):
            await response.write(chunk.encode("utf-8"))

        await response.write_eof()
        return response


class EkazaDashboardsView(HomeAssistantView):
    url = "/api/ekaza_wizard/dashboards"
    name = "api:ekaza_wizard:dashboards"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        dashboards = await list_dashboards(self._hass)
        return self.json({"dashboards": dashboards})


class EkazaSmartLifeUnblockView(HomeAssistantView):
    url = "/api/ekaza_wizard/adguard/unblock"
    name = "api:ekaza_wizard:adguard_unblock"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> StreamResponse:
        response = StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        async for chunk in remover.unblock_smartlife_only(self._hass):
            await response.write(chunk.encode("utf-8"))
        await response.write_eof()
        return response


class EkazaSmartLifeBlockView(HomeAssistantView):
    url = "/api/ekaza_wizard/adguard/block"
    name = "api:ekaza_wizard:adguard_block"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> StreamResponse:
        response = StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        async for chunk in remover.block_smartlife_only(self._hass):
            await response.write(chunk.encode("utf-8"))
        await response.write_eof()
        return response


class EkazaAdGuardCheckView(HomeAssistantView):
    url = "/api/ekaza_wizard/adguard/check"
    name = "api:ekaza_wizard:adguard_check"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        ok, msg = await adguard_mod.check_accessible(self._hass)
        return self.json({"ok": ok, "msg": msg})


class EkazaAdGuardStatusView(HomeAssistantView):
    url = "/api/ekaza_wizard/adguard/status"
    name = "api:ekaza_wizard:adguard_status"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        return self.json(await adguard_mod.get_rules_status())


class EkazaProvisionView(HomeAssistantView):
    url = "/api/ekaza_wizard/provision"
    name = "api:ekaza_wizard:provision"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry

    async def post(self, request: web.Request) -> StreamResponse:
        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "JSON inválido"}, status_code=400)

        try:
            req = ProvisionRequest(**body)
        except Exception as exc:
            return self.json({"error": str(exc)}, status_code=422)

        response = StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        async for chunk in provisioner.run(
            self._hass, req.credentials, req.cameras,
            dashboard_path=req.dashboard_path,
        ):
            await response.write(chunk.encode("utf-8"))

        await response.write_eof()
        return response
