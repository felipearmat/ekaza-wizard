"""Camera discovery: Tuya Cloud API (primary) + local UDP scan (IP enrichment)."""
from __future__ import annotations

import asyncio
import logging
import re

import tinytuya

from . import schema_store
from .models import CameraInfo, TuyaCredentials

_LOGGER = logging.getLogger(__name__)


def _slug(name: str) -> str:
    s = re.sub(r"^(c[aâ]mera|cam)\s*[-_]?\s*", "", name.lower()).strip()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "camera"


def _cloud_devices(creds: TuyaCredentials, hint_device_id: str = "") -> list[dict]:
    """Return all Tuya devices in the account using tinytuya.Cloud.

    hint_device_id: any known device_id from the account (e.g. from LocalTuya).
    tinytuya uses it to resolve the Smart Life app user's uid, which is required
    to list all devices. Without it, only devices visible to the developer account
    are returned (usually none).
    """
    try:
        cloud = tinytuya.Cloud(
            apiRegion=creds.region,
            apiKey=creds.access_id,
            apiSecret=creds.access_secret,
            apiDeviceID=hint_device_id or None,
        )
        raw = cloud.getdevices()
    except Exception as exc:
        _LOGGER.warning("tinytuya.Cloud.getdevices() failed: %s", exc)
        return []

    if isinstance(raw, list):
        devices = raw
    elif isinstance(raw, dict):
        devices = raw.get("result", {}).get("list", raw.get("list", []))
    else:
        devices = []

    _LOGGER.debug("Tuya Cloud returned %d device(s)", len(devices))
    if not devices:
        _LOGGER.warning("No devices returned from Tuya Cloud (hint_device_id=%r)", hint_device_id)
        return []

    # Enrich with local IPs via UDP scan (best-effort — may fail in containers)
    local: dict[str, dict] = {}
    try:
        scan = tinytuya.deviceScan(verbose=False, maxretry=4)
        if isinstance(scan, dict):
            for info in scan.values():
                dev_id = info.get("gwId") or info.get("id", "")
                if dev_id:
                    local[dev_id] = info
        _LOGGER.debug("UDP scan found %d local device(s)", len(local))
    except Exception as exc:
        _LOGGER.debug("UDP scan skipped (normal in container): %s", exc)

    for dev in devices:
        dev_id = dev.get("id", "")
        if dev_id in local:
            dev["ip"] = local[dev_id].get("ip", dev.get("ip", ""))
            dev["mac"] = local[dev_id].get("mac", dev.get("mac", ""))
        else:
            dev.setdefault("ip", "")
            dev.setdefault("mac", "")

    return devices


async def discover(creds: TuyaCredentials, hass=None) -> list[CameraInfo]:
    loop = asyncio.get_running_loop()
    creds_dict = {"region": creds.region, "access_id": creds.access_id, "access_secret": creds.access_secret}

    # Use a known device_id from LocalTuya as seed for tinytuya uid resolution
    hint_device_id = ""
    if hass:
        for entry in hass.config_entries.async_entries("localtuya"):
            for dev_id in entry.data.get("devices", {}).keys():
                hint_device_id = dev_id
                break
            if hint_device_id:
                break
        if hint_device_id:
            _LOGGER.debug("Using LocalTuya device as uid seed: %s", hint_device_id[:8] + "...")

    devices = await loop.run_in_executor(None, _cloud_devices, creds, hint_device_id)

    cameras, seen = [], {}
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        device_id = dev.get("id", "")
        product_id = dev.get("product_id", "")

        sch = await schema_store.get(product_id, device_id, creds_dict)
        if not schema_store.is_camera(sch, dev):
            continue

        slug = _slug(dev.get("name", device_id))
        if slug in seen:
            seen[slug] += 1
            slug = f"{slug}_{seen[slug]}"
        else:
            seen[slug] = 1

        cameras.append(CameraInfo(
            name=dev.get("name", device_id),
            slug=slug,
            device_id=device_id,
            local_key=dev.get("local_key", ""),
            ip=dev.get("ip", "unknown"),
            mac=dev.get("mac", ""),
            product_id=product_id,
            rtsp_password=creds.default_rtsp_password,
            rtsp_username=creds.rtsp_username,
            online=dev.get("online", True),
        ))
    return cameras
