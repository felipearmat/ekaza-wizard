"""Camera removal — reverses provisioner steps."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, device_registry as dr

from . import adguard as adguard_mod
from . import frigate as frigate_mod
from .adguard import remove_dns_rewrite
from .ha_helpers import delete_input_boolean, load_cameras, remove_camera_from_store
from . import dashboard as dashboard_mod

_LOGGER = logging.getLogger(__name__)
_SCRIPTS_DIR = Path("/config/scripts")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _reload_frigate_integration(hass: HomeAssistant) -> bool:
    for entry in hass.config_entries.async_entries():
        if entry.domain == "frigate":
            try:
                await hass.config_entries.async_reload(entry.entry_id)
                return True
            except Exception as exc:
                _LOGGER.warning("Frigate integration reload failed: %s", exc)
    return False


async def _find_localtuya_device(
    hass: HomeAssistant, slug: str
) -> tuple[str | None, str | None]:
    from homeassistant.util import slugify

    for entry in hass.config_entries.async_entries("localtuya"):
        for dev_id, dev_data in entry.data.get("devices", {}).items():
            fn = dev_data.get("friendly_name", "")
            if slugify(fn) == slug:
                return dev_id, dev_data.get("host", "")
    return None, None


async def _remove_from_localtuya(
    hass: HomeAssistant, device_id: str
) -> tuple[bool, str]:
    removed: list[str] = []
    for entry in hass.config_entries.async_entries("localtuya"):
        devices = dict(entry.data.get("devices", {}))
        if device_id not in devices:
            continue
        del devices[device_id]
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "devices": devices}
        )
        try:
            await hass.config_entries.async_reload(entry.entry_id)
        except Exception as exc:
            _LOGGER.warning("LocalTuya reload failed: %s", exc)
        removed.append(entry.entry_id[:8])

    if removed:
        return True, "Removido do LocalTuya — entidades descarregadas"
    return True, "Dispositivo não encontrado no LocalTuya (já removido)"


async def _cleanup_registries(
    hass: HomeAssistant, slug: str, device_id: str | None
) -> str:
    """Remove orphan entity and device registry entries for a removed camera."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    removed: list[str] = []

    # LocalTuya entities: unique_id contains device_id (e.g. "local_{device_id}_{dp}")
    if device_id:
        for entry in list(ent_reg.entities.values()):
            if (
                entry.platform == "localtuya"
                and entry.unique_id
                and device_id in entry.unique_id
            ):
                ent_reg.async_remove(entry.entity_id)
                removed.append(entry.entity_id)

    # Script entities matching this slug (e.g. script.{slug}_ptz_up)
    prefix = f"script.{slug}_"
    for entry in list(ent_reg.entities.values()):
        if entry.entity_id.startswith(prefix) and entry.platform == "script":
            ent_reg.async_remove(entry.entity_id)
            removed.append(entry.entity_id)

    # Device registry entries registered by LocalTuya for this device_id
    if device_id:
        for device in list(dev_reg.devices.values()):
            for identifier in device.identifiers:
                if device_id in str(identifier):
                    dev_reg.async_remove_device(device.id)
                    removed.append(f"device:{device.id[:8]}")
                    break

    return f"Removidas {len(removed)} entidades/dispositivos orfãos do registry"


async def block_smartlife_only(hass: HomeAssistant) -> AsyncGenerator[str, None]:
    """Standalone: add SmartLife blocking rules to AdGuard without touching cameras."""
    yield _sse("start", {"slug": "__smartlife_only__", "name": "Bloqueio SmartLife"})
    ok, msg = await adguard_mod.add_block_rules(hass)
    yield _sse("step", {"step": "smartlife_block", "ok": ok, "detail": msg})
    yield _sse("done", {"slug": "__smartlife_only__", "name": "Bloqueio SmartLife"})


async def unblock_smartlife_only(hass: HomeAssistant) -> AsyncGenerator[str, None]:
    """Standalone: remove SmartLife blocking rules from AdGuard without touching cameras."""
    yield _sse("start", {"slug": "__smartlife_only__", "name": "Regras SmartLife"})
    ok, msg = await adguard_mod.remove_block_rules(hass)
    yield _sse("step", {"step": "smartlife_unblock", "ok": ok, "detail": msg})
    yield _sse("done", {"slug": "__smartlife_only__", "name": "Regras SmartLife"})


async def list_cameras(hass: HomeAssistant) -> list[dict]:
    """Return cameras present in Frigate config, enriched with HA/LocalTuya info."""
    slugs = await frigate_mod.get_camera_slugs(hass)
    cameras = []

    for slug in slugs:
        cam_state = hass.states.get(f"camera.{slug}")
        name = (
            (cam_state.attributes.get("friendly_name") or slug) if cam_state else slug
        )
        device_id, ip = await _find_localtuya_device(hass, slug)
        cameras.append(
            {
                "slug": slug,
                "name": name,
                "device_id": device_id,
                "ip": ip or "",
                "has_frigate": True,
                "has_localtuya": device_id is not None,
                "has_scripts": (_SCRIPTS_DIR / f"{slug}_ptz.yaml").exists(),
            }
        )

    return sorted(cameras, key=lambda c: c["name"].lower())


async def remove(
    hass: HomeAssistant,
    slug: str,
    device_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Remove a provisioned camera. Yields SSE events for each step.

    Total: 8 steps
      1. proxy_cleanup  — remove DNS rewrite (if cam→frigate mode) + remove from store
      2. frigate_config — remove from Frigate config + trigger Frigate restart
      3. reload_frigate_integration — wait for Frigate restart, reload HA integration
      4. scripts        — delete PTZ scripts file
      5. reload_scripts — reload HA scripts domain
      6. localtuya      — remove from LocalTuya
      7. motion_bridge_boolean — delete input_boolean cam_fr mode indicator
      8. dashboard      — remove card from Lovelace
    """
    cam_state = hass.states.get(f"camera.{slug}")
    name = (cam_state.attributes.get("friendly_name") or slug) if cam_state else slug

    yield _sse("start", {"slug": slug, "name": name})

    # 1 — Remove AdGuard DNS rewrite (if cam→frigate mode) + remove from camera store
    try:
        all_cams = await load_cameras(hass)
        removed_cam = next((c for c in all_cams if c.slug == slug), None)
        if removed_cam and removed_cam.proxy_enabled and removed_cam.tuya_mqtt_domain:
            ha_ip = (
                hass.config.api.local_ip
                if hass.config.api and hass.config.api.local_ip
                else "127.0.0.1"
            )
            await remove_dns_rewrite(removed_cam.tuya_mqtt_domain, ha_ip)

        await remove_camera_from_store(hass, slug)

        yield _sse(
            "step",
            {
                "step": "proxy_cleanup",
                "ok": True,
                "detail": "Configuração proxy removida",
            },
        )
    except Exception as exc:
        yield _sse("step", {"step": "proxy_cleanup", "ok": False, "detail": str(exc)})

    # 2 — Remove from Frigate config via API (triggers Frigate restart automatically)
    ok, msg = await frigate_mod.remove_camera(slug, hass=hass)
    yield _sse("step", {"step": "frigate_config", "ok": ok, "detail": msg})

    # 3 — Wait for Frigate restart, then reload HA integration to unregister camera entities
    await asyncio.sleep(15)
    reloaded = await _reload_frigate_integration(hass)
    yield _sse(
        "step",
        {
            "step": "reload_frigate_integration",
            "ok": reloaded,
            "detail": "Integração Frigate recarregada — entidades de câmera removidas"
            if reloaded
            else "Recarregue a integração Frigate manualmente",
        },
    )

    # 4 — Delete PTZ scripts file
    scripts_path = _SCRIPTS_DIR / f"{slug}_ptz.yaml"
    try:
        if scripts_path.exists():
            scripts_path.unlink()
            yield _sse(
                "step",
                {"step": "scripts", "ok": True, "detail": "Scripts PTZ removidos"},
            )
        else:
            yield _sse(
                "step",
                {
                    "step": "scripts",
                    "ok": True,
                    "detail": "Scripts PTZ não encontrados (ok)",
                },
            )
    except Exception as exc:
        yield _sse("step", {"step": "scripts", "ok": False, "detail": str(exc)})

    # 5 — Reload HA scripts to unregister PTZ script entities
    try:
        await hass.services.async_call("script", "reload", blocking=True)
        yield _sse(
            "step",
            {"step": "reload_scripts", "ok": True, "detail": "Scripts HA recarregados"},
        )
    except Exception as exc:
        yield _sse("step", {"step": "reload_scripts", "ok": False, "detail": str(exc)})

    # 6 — Remove from LocalTuya
    if not device_id:
        device_id, _ = await _find_localtuya_device(hass, slug)

    if device_id:
        ok, msg = await _remove_from_localtuya(hass, device_id)
    else:
        ok, msg = (
            True,
            "Dispositivo não encontrado no LocalTuya (já removido ou não configurado)",
        )
    yield _sse("step", {"step": "localtuya", "ok": ok, "detail": msg})

    # 7 — Delete input_boolean.{slug}_motion_bridge
    ok, msg = await delete_input_boolean(hass, f"{slug}_motion_bridge")
    yield _sse("step", {"step": "motion_bridge_boolean", "ok": ok, "detail": msg})

    # 8 — Remove card from Lovelace dashboard
    ok, msg = await dashboard_mod.remove_card(hass, slug)
    yield _sse("step", {"step": "dashboard", "ok": ok, "detail": msg})

    # 9 — Clean up orphan entity/device registry entries
    try:
        detail = await _cleanup_registries(hass, slug, device_id)
        yield _sse("step", {"step": "registry_cleanup", "ok": True, "detail": detail})
    except Exception as exc:
        yield _sse(
            "step", {"step": "registry_cleanup", "ok": False, "detail": str(exc)}
        )

    yield _sse("done", {"slug": slug, "name": name})
