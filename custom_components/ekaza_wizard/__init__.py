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
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant

from . import adguard as adguard_mod
from . import check as check_mod
from . import companion as companion_mod
from . import discovery, provisioner, remover
from .dashboard import ensure_card_resource, list_dashboards
from .const import (
    CONF_RTSP_PASSWORD,
    CONF_SEED_DEVICE_ID,
    CONF_TUYA_ACCESS_ID,
    CONF_TUYA_ACCESS_SECRET,
    CONF_TUYA_REGION,
    DOMAIN,
)
from .models import ProvisionRequest, TuyaCredentials

_LOGGER = logging.getLogger(__name__)
_STATIC = Path(__file__).parent / "static"
PLATFORMS: list[str] = []


async def _deploy_card_resource(hass: HomeAssistant) -> None:
    """Copy bundled card JS to /config/www/ and register Lovelace resource.

    Defers registration to EVENT_HOMEASSISTANT_STARTED when called during startup,
    since the lovelace resources store may not be ready yet.
    """

    async def _do() -> None:
        ok, msg = await ensure_card_resource(hass)
        if ok:
            _LOGGER.warning("Card resource: %s", msg)
        else:
            _LOGGER.error("Card resource failed: %s", msg)

    async def _on_started(_event: Event) -> None:
        await _do()

    if hass.is_running:
        await _do()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["entry"] = entry

    html = await hass.async_add_executor_job(
        (_STATIC / "index.html").read_text, "utf-8"
    )
    hass.http.register_view(EkazaIndexView(html))
    hass.http.register_view(EkazaConfigView(hass, entry))
    hass.http.register_view(EkazaDiscoverView(hass, entry))
    hass.http.register_view(EkazaDebugDiscoveryView(hass, entry))
    hass.http.register_view(EkazaProvisionView(hass, entry))
    hass.http.register_view(EkazaCheckView(hass))
    hass.http.register_view(EkazaListCamerasView(hass))
    hass.http.register_view(EkazaRemoveView(hass))
    hass.http.register_view(EkazaDashboardsView(hass))
    hass.http.register_view(EkazaAdGuardCheckView(hass))
    hass.http.register_view(EkazaAdGuardStatusView(hass))
    hass.http.register_view(EkazaSmartLifeUnblockView(hass))
    hass.http.register_view(EkazaSmartLifeBlockView(hass))
    hass.http.register_view(EkazaProxyToggleView(hass))
    hass.http.register_view(EkazaProxyStatusView(hass))
    hass.http.register_view(EkazaAdguardDebugYamlView())
    hass.http.register_view(EkazaAdguardRestartTestView())
    hass.http.register_view(EkazaAdguardRewriteSyncView(hass))
    hass.http.register_view(EkazaAdguardRewriteView(hass))
    hass.http.register_view(EkazaCompanionStatusView(hass))

    from . import frigate_syncer
    from .ha_helpers import load_cameras

    hass.loop.create_task(frigate_syncer.setup(hass))

    async def _sync_companion_on_startup() -> None:
        cameras = await load_cameras(hass)
        companion_status = await companion_mod.get_status(hass)
        if companion_status is not None:
            cam_payloads = [
                {
                    "slug": c.slug,
                    "ip": c.ip,
                    "proxy_enabled": c.proxy_enabled,
                    "tuya_mqtt_domain": c.tuya_mqtt_domain or "m.tuyaus.com",
                    "device_id": c.device_id,
                    "local_key": c.local_key,
                }
                for c in cameras
            ]
            ok = await companion_mod.update_cameras(hass, cam_payloads)
            if ok:
                _LOGGER.warning(
                    "Companion synced %d camera(s) on startup", len(cam_payloads)
                )
            else:
                _LOGGER.error("Companion camera sync failed on startup")
        else:
            _LOGGER.warning(
                "Tuya Proxy Companion not running — cam→frigate mode unavailable; "
                "install the Companion add-on to enable it"
            )

    hass.async_create_task(_sync_companion_on_startup())
    hass.async_create_task(_deploy_card_resource(hass))

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

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry

    async def get(self, request: web.Request) -> web.Response:
        d = self._entry.data
        return self.json(
            {
                "tuya_access_id": d.get(CONF_TUYA_ACCESS_ID, ""),
                "tuya_access_secret": d.get(CONF_TUYA_ACCESS_SECRET, ""),
                "tuya_region": d.get(CONF_TUYA_REGION, "us"),
                "rtsp_password": d.get(CONF_RTSP_PASSWORD, ""),
                "seed_device_id": d.get(CONF_SEED_DEVICE_ID, ""),
            }
        )

    async def post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "JSON inválido"}, status_code=400)

        new_data = dict(self._entry.data)
        for key in (
            CONF_TUYA_ACCESS_ID,
            CONF_TUYA_ACCESS_SECRET,
            CONF_TUYA_REGION,
            CONF_RTSP_PASSWORD,
            CONF_SEED_DEVICE_ID,
        ):
            if key in body:
                new_data[key] = body[key]

        self._hass.config_entries.async_update_entry(self._entry, data=new_data)
        return self.json({"ok": True})


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
            return self.json(
                {"error": "Credenciais Tuya obrigatórias"}, status_code=400
            )

        try:
            seed = (
                override.get("seed_device_id")
                or self._entry.data.get(CONF_SEED_DEVICE_ID, "")
            ).strip()
            # Auto-save seed_device_id if user provided a new one
            if seed and seed != self._entry.data.get(CONF_SEED_DEVICE_ID, ""):
                new_data = dict(self._entry.data)
                new_data[CONF_SEED_DEVICE_ID] = seed
                self._hass.config_entries.async_update_entry(self._entry, data=new_data)
            cameras = await discovery.discover(
                creds, hass=self._hass, seed_device_id=seed
            )
            return self.json({"cameras": [c.model_dump() for c in cameras]})
        except Exception as exc:
            _LOGGER.exception("Discovery failed")
            return self.json({"error": str(exc)}, status_code=500)


class EkazaDebugDiscoveryView(HomeAssistantView):
    """Temporary diagnostic endpoint — returns raw tinytuya output."""

    url = "/api/ekaza_wizard/debug_discovery"
    name = "api:ekaza_wizard:debug_discovery"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry

    async def get(self, request: web.Request) -> web.Response:
        import asyncio
        import tinytuya as tt

        loop = asyncio.get_running_loop()
        d = self._entry.data
        access_id = d.get(CONF_TUYA_ACCESS_ID, "")
        access_secret = d.get(CONF_TUYA_ACCESS_SECRET, "")
        region = d.get(CONF_TUYA_REGION, "us")

        def _run_sync():
            result = {}
            try:
                result["tinytuya_version"] = tt.__version__
            except Exception as e:
                result["tinytuya_version"] = f"error: {e}"

            try:
                cloud = tt.Cloud(
                    apiRegion=region, apiKey=access_id, apiSecret=access_secret
                )
                raw = cloud.getdevices()
                result["getdevices_type"] = type(raw).__name__
                if isinstance(raw, list):
                    result["getdevices_count"] = len(raw)
                    result["getdevices_sample"] = [
                        {
                            "name": dv.get("name"),
                            "id": dv.get("id", "")[:12],
                            "category": dv.get("category"),
                            "key_field": "key"
                            if "key" in dv
                            else ("local_key" if "local_key" in dv else "none"),
                            "product_id": dv.get("product_id"),
                            "mac": dv.get("mac", ""),
                        }
                        for dv in raw[:5]
                    ]
                else:
                    result["getdevices_raw"] = str(raw)[:500]
            except Exception as e:
                result["getdevices_error"] = str(e)

            try:
                from . import schema_store

                result["schema_bundle"] = [
                    str(p.name) for p in schema_store._BUNDLED.glob("*.json")
                ]
            except Exception as e:
                result["schema_error"] = str(e)

            return result

        data = await loop.run_in_executor(None, _run_sync)

        # Step 2: Test the full discover pipeline (async)
        try:
            from . import schema_store as ss
            from .discovery import _cloud_devices
            from .models import TuyaCredentials as TC

            creds = TC(
                access_id=access_id,
                access_secret=access_secret,
                region=region,
                default_rtsp_password="",
            )
            creds_dict = {
                "region": region,
                "access_id": access_id,
                "access_secret": access_secret,
            }

            devices = await loop.run_in_executor(None, _cloud_devices, creds, "")
            data["pipeline_device_count"] = len(devices)

            per_device = []
            for dev in devices:
                pid = dev.get("product_id", "")
                did = dev.get("id", "")
                schema = await ss.get(pid, did, creds_dict)
                is_cam = ss.is_camera(schema, dev)
                per_device.append(
                    {
                        "name": dev.get("name"),
                        "id": did[:12],
                        "category": dev.get("category"),
                        "product_id": pid,
                        "schema_loaded": schema is not None,
                        "schema_has_ptz": schema.get("capabilities", {}).get("ptz")
                        if schema
                        else None,
                        "is_camera": is_cam,
                    }
                )
            data["pipeline_per_device"] = per_device
        except Exception as e:
            import traceback

            data["pipeline_error"] = str(e)
            data["pipeline_traceback"] = traceback.format_exc()

        return self.json(data)


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


class EkazaProxyStatusView(HomeAssistantView):
    """GET /api/ekaza_wizard/proxy/status — proxy running state + DNS rewrites."""

    url = "/api/ekaza_wizard/proxy/status"
    name = "api:ekaza_wizard:proxy_status"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        from .ha_helpers import load_cameras

        cameras = await load_cameras(self._hass)
        proxy_cams = [
            {
                "slug": c.slug,
                "name": c.name,
                "domain": c.tuya_mqtt_domain or "m.tuyaus.com",
                "enabled": c.proxy_enabled,
            }
            for c in cameras
        ]
        companion_status = await companion_mod.get_status(self._hass)
        return self.json(
            {
                "proxy_running": companion_status["proxy_running"]
                if companion_status
                else False,
                "proxy_port": companion_status["proxy_port"]
                if companion_status
                else None,
                "companion_installed": companion_status is not None,
                "cameras": proxy_cams,
            }
        )


class EkazaProxyToggleView(HomeAssistantView):
    """POST /api/ekaza_wizard/proxy/toggle — enable or disable proxy for a camera.

    Body: {"slug": "...", "enable": true}

    Marks camera proxy_enabled in storage, syncs the updated list to the Tuya Proxy
    Companion (which owns the local Tuya listener and iptables rules), and manages the
    AdGuard DNS rewrite so the camera resolves its MQTT broker to this HA host (required
    for the iptables REDIRECT to intercept the camera's cloud MQTT traffic).
    """

    url = "/api/ekaza_wizard/proxy/toggle"
    name = "api:ekaza_wizard:proxy_toggle"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "JSON inválido"}, status_code=400)

        slug = body.get("slug", "").strip()
        enable = bool(body.get("enable", True))
        if not slug:
            return self.json({"error": "'slug' obrigatório"}, status_code=422)

        from .ha_helpers import load_cameras, save_cameras

        cameras = await load_cameras(self._hass)
        cam = next((c for c in cameras if c.slug == slug), None)
        if not cam:
            return self.json(
                {"error": f"Câmera '{slug}' não encontrada no storage"}, status_code=404
            )

        cam.proxy_enabled = enable

        # Sync updated camera list to companion (owns iptables) or fallback in-process proxy.
        cam_payloads = [
            {
                "slug": c.slug,
                "ip": c.ip,
                "proxy_enabled": c.proxy_enabled,
                "tuya_mqtt_domain": c.tuya_mqtt_domain or "m.tuyaus.com",
                "device_id": c.device_id,
                "local_key": c.local_key,
            }
            for c in cameras
        ]
        if await companion_mod.get_status(self._hass) is not None:
            await companion_mod.update_cameras(self._hass, cam_payloads)

        try:
            await save_cameras(self._hass, cameras)
        except Exception as exc:
            _LOGGER.warning("proxy toggle: save_cameras failed: %s", exc)

        # Manage AdGuard DNS rewrite asynchronously — the backup/restore cycle takes ~40s
        # and must not block this response. Without the rewrite, the camera resolves the
        # Tuya MQTT broker to a cloud IP and bypasses the iptables REDIRECT entirely.
        if cam.tuya_mqtt_domain:
            ha_ip = (
                self._hass.config.api.local_ip
                if self._hass.config.api and self._hass.config.api.local_ip
                else "127.0.0.1"
            )

            async def _manage_rewrite() -> None:
                from .adguard import add_dns_rewrite, remove_dns_rewrite

                domain = cam.tuya_mqtt_domain
                if enable:
                    ok, msg = await add_dns_rewrite(domain, ha_ip)
                else:
                    ok, msg = await remove_dns_rewrite(domain, ha_ip)
                if ok:
                    _LOGGER.warning("proxy toggle: DNS rewrite %s: %s", domain, msg)
                else:
                    _LOGGER.error(
                        "proxy toggle: DNS rewrite failed for %s: %s", domain, msg
                    )

            self._hass.async_create_task(_manage_rewrite())

        return self.json(
            {
                "ok": True,
                "slug": slug,
                "proxy_enabled": cam.proxy_enabled,
            }
        )


class EkazaCompanionStatusView(HomeAssistantView):
    """GET /api/ekaza_wizard/companion/status — companion add-on presence and proxy state."""

    url = "/api/ekaza_wizard/companion/status"
    name = "api:ekaza_wizard:companion_status"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        slug = await companion_mod.find_companion_slug(self._hass)
        status = await companion_mod.get_status(self._hass)
        return self.json(
            {
                "installed": slug is not None,
                "slug": slug,
                "running": status is not None,
                "proxy": status or {},
            }
        )


class EkazaAdguardDebugYamlView(HomeAssistantView):
    """GET /api/ekaza_wizard/debug/adguard_yaml — retorna estrutura do AdGuardHome.yaml."""

    url = "/api/ekaza_wizard/debug/adguard_yaml"
    name = "api:ekaza_wizard:debug_adguard_yaml"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        import yaml as _yaml
        from .adguard import (
            _backup_create,
            _get_adguard_slug,
            _sup_delete,
            _extract_yaml_from_backup,
        )

        addon_slug = await _get_adguard_slug()
        slug, backup_bytes = await _backup_create(addon_slug)
        if not backup_bytes:
            if slug:
                await _sup_delete(f"/backups/{slug}")
            return self.json({"error": "backup falhou"})
        try:
            raw = _extract_yaml_from_backup(backup_bytes, addon_slug)
            cfg = _yaml.safe_load(raw)
            result = {
                "keys": list(cfg.keys()) if cfg else [],
                "rewrites": cfg.get("rewrites"),
                "filtering_rewrites": cfg.get("filtering", {}).get("rewrites")
                if cfg
                else None,
                "user_rules_count": len(cfg.get("user_rules") or []) if cfg else 0,
            }
        except Exception as exc:
            result = {"error": str(exc)}
        finally:
            if slug:
                await _sup_delete(f"/backups/{slug}")
        return self.json(result)


class EkazaAdguardRestartTestView(HomeAssistantView):
    """GET /api/ekaza_wizard/debug/adguard_restart — reinicia só o AdGuard e retorna rewrites."""

    url = "/api/ekaza_wizard/debug/adguard_restart"
    name = "api:ekaza_wizard:debug_adguard_restart"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        import asyncio
        from .adguard import _get_adguard_slug, _sup_post, sync_dns_rewrites

        addon_slug = await _get_adguard_slug()
        if not addon_slug:
            return self.json({"error": "AdGuard não encontrado"})

        before_ok, before = await sync_dns_rewrites()
        r = await _sup_post(f"/addons/{addon_slug}/restart", {}, timeout=60)
        await asyncio.sleep(10)
        after_ok, after = await sync_dns_rewrites()

        return self.json(
            {
                "addon_slug": addon_slug,
                "restart_result": r,
                "before": before,
                "after": after,
                "persisted": before == after,
            }
        )


class EkazaAdguardRewriteSyncView(HomeAssistantView):
    """GET /api/ekaza_wizard/adguard/rewrites/sync — read rewrites from AdGuard backup.

    Creates a temporary backup, reads AdGuardHome.yaml, updates the local cache,
    and returns the current rewrite list. Takes ~20s but does NOT restart AdGuard.
    """

    url = "/api/ekaza_wizard/adguard/rewrites/sync"
    name = "api:ekaza_wizard:adguard_rewrites_sync"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        from .adguard import sync_dns_rewrites

        ok, rewrites = await sync_dns_rewrites()
        return self.json({"ok": ok, "rewrites": rewrites, "count": len(rewrites)})


class EkazaAdguardRewriteView(HomeAssistantView):
    """POST /api/ekaza_wizard/adguard/rewrite — add or remove DNS rewrite only.

    Body: {"slug": "...", "enable": true}

    Unlike proxy/toggle, this does NOT change proxy_enabled or touch the MITM proxy
    process — it only manages the AdGuard DNS rewrite rule for the camera's MQTT domain.
    """

    url = "/api/ekaza_wizard/adguard/rewrite"
    name = "api:ekaza_wizard:adguard_rewrite"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "JSON inválido"}, status_code=400)

        slug = body.get("slug", "").strip()
        enable = bool(body.get("enable", True))
        if not slug:
            return self.json({"error": "'slug' obrigatório"}, status_code=422)

        from .adguard import add_dns_rewrite, remove_dns_rewrite
        from .ha_helpers import load_cameras

        cameras = await load_cameras(self._hass)
        cam = next((c for c in cameras if c.slug == slug), None)
        if not cam:
            return self.json(
                {"error": f"Câmera '{slug}' não encontrada"}, status_code=404
            )

        if not cam.tuya_mqtt_domain:
            from .adguard import discover_camera_mqtt_domain
            from .ha_helpers import save_cameras

            domain = await discover_camera_mqtt_domain(cam.ip)
            cam.tuya_mqtt_domain = domain or "m.tuyaus.com"
            try:
                await save_cameras(self._hass, cameras)
            except Exception:
                pass

        ha_ip = (
            self._hass.config.api.local_ip
            if self._hass.config.api and self._hass.config.api.local_ip
            else "127.0.0.1"
        )

        if enable:
            ok, msg = await add_dns_rewrite(cam.tuya_mqtt_domain, ha_ip)
        else:
            ok, msg = await remove_dns_rewrite(cam.tuya_mqtt_domain, ha_ip)

        return self.json(
            {"ok": ok, "slug": slug, "domain": cam.tuya_mqtt_domain, "detail": msg}
        )


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
            self._hass,
            req.credentials,
            req.cameras,
            dashboard_path=req.dashboard_path,
        ):
            await response.write(chunk.encode("utf-8"))

        await response.write_eof()
        return response
