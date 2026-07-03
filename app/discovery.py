"""Tuya cloud discovery + local network scan + schema resolution."""
import asyncio
import re
from typing import Any

import tinytuya

import schema_store
from models import CameraInfo, TuyaCredentials


def _to_slug(name: str) -> str:
    slug = name.lower()
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
    """Returns {device_id: {ip, mac, productKey, version, ...}}"""
    result = tinytuya.deviceScan(verbose=False, maxretry=6)
    if not isinstance(result, dict):
        return {}
    return result


async def discover(creds: TuyaCredentials) -> list[CameraInfo]:
    loop = asyncio.get_event_loop()
    creds_dict = {
        "region": creds.region,
        "access_id": creds.access_id,
        "access_secret": creds.access_secret,
    }

    devices, ip_map = await asyncio.gather(
        loop.run_in_executor(None, _cloud_devices, creds),
        loop.run_in_executor(None, _local_scan),
    )

    cameras: list[CameraInfo] = []
    seen_slugs: dict[str, int] = {}

    for dev in devices:
        device_id  = dev.get("id", "")
        product_id = dev.get("product_id", "")
        local_info = ip_map.get(device_id, {})

        # product_id from local scan (productKey) takes precedence when cloud field is empty
        if not product_id:
            product_id = local_info.get("productKey", "")

        # Resolve schema (local first, cloud fallback for unknown models)
        sch = await schema_store.get(product_id, device_id, creds_dict)

        if not schema_store.is_camera(sch, dev):
            continue

        ip  = local_info.get("ip", "unknown")
        mac = local_info.get("mac", "")

        slug = _to_slug(dev.get("name", device_id))
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
                local_key=dev.get("local_key", dev.get("key", "")),
                ip=ip,
                mac=mac,
                product_id=product_id,
                rtsp_password=creds.default_rtsp_password,
                rtsp_username=creds.rtsp_username,
                online=dev.get("online", False),
            )
        )

    return cameras
