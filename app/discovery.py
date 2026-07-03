"""Tuya cloud discovery + local network scan."""
import asyncio
import re
from typing import Any

import tinytuya

from constants import EKAZA_PRODUCT_IDS
from models import CameraInfo, TuyaCredentials


def _to_slug(name: str) -> str:
    slug = name.lower()
    # strip leading "camera"/"câmera"/"cam " prefix
    slug = re.sub(r"^(c[aâ]mera|cam)\s*[-_]?\s*", "", slug).strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    return slug or "camera"


def _cloud_devices(creds: TuyaCredentials) -> list[dict]:
    cloud = tinytuya.Cloud(
        apiRegion=creds.region,
        apiKey=creds.access_id,
        apiSecret=creds.access_secret,
        apiDeviceID="wizard",
    )
    return cloud.getdevices() or []


def _local_scan() -> dict[str, dict]:
    """Returns {device_id: {ip, mac, ...}}"""
    result = tinytuya.deviceScan(verbose=False, maxretry=6)
    if not isinstance(result, dict):
        return {}
    return result


def _is_ekaza(device: dict) -> bool:
    if EKAZA_PRODUCT_IDS and device.get("product_id") in EKAZA_PRODUCT_IDS:
        return True
    # fallback: name heuristic when product_id is unknown
    name = device.get("name", "").lower()
    return any(k in name for k in ("ekaza", "camera", "câmera", "cam", "cctv"))


async def discover(creds: TuyaCredentials) -> list[CameraInfo]:
    loop = asyncio.get_event_loop()

    devices, ip_map = await asyncio.gather(
        loop.run_in_executor(None, _cloud_devices, creds),
        loop.run_in_executor(None, _local_scan),
    )

    cameras: list[CameraInfo] = []
    seen_slugs: dict[str, int] = {}

    for dev in devices:
        if not _is_ekaza(dev):
            continue

        device_id = dev.get("id", "")
        local_info = ip_map.get(device_id, {})
        ip = local_info.get("ip", "unknown")
        mac = local_info.get("mac", "")

        slug = _to_slug(dev.get("name", device_id))
        # deduplicate slugs
        if slug in seen_slugs:
            seen_slugs[slug] += 1
            slug = f"{slug}_{seen_slugs[slug]}"
        else:
            seen_slugs[slug] = 1

        cameras.append(
            CameraInfo(
                name=dev.get("name", device_id),
                slug=slug,
                device_id=device_id,
                local_key=dev.get("local_key", ""),
                ip=ip,
                mac=mac,
                rtsp_password=creds.default_rtsp_password,
                rtsp_username=creds.rtsp_username,
                online=dev.get("online", False),
            )
        )

    return cameras
