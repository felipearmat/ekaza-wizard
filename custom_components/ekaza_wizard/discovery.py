"""Camera discovery: Tuya Cloud (primary) + UDP scan (fallback)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time

import requests
import tinytuya

from . import schema_store
from .models import CameraInfo, TuyaCredentials

_LOGGER = logging.getLogger(__name__)

_TUYA_BASE = {
    "us": "https://openapi.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com",
    "in": "https://openapi.tuyain.com",
    "cn": "https://openapi.tuyacn.com",
}


def _slug(name: str) -> str:
    s = re.sub(r"^(c[aâ]mera|cam)\s*[-_]?\s*", "", name.lower()).strip()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "camera"


def _tuya_get(base: str, path: str, access_id: str, secret: str, token: str | None = None) -> dict:
    now = int(time.time() * 1000)
    sha256_body = hashlib.sha256(b"").hexdigest()
    headers: dict = {}
    if token is None:
        payload = access_id + str(now)
        headers["secret"] = secret
    else:
        payload = access_id + token + str(now)
    payload += f"GET\n{sha256_body}\n\n/{path.lstrip('/')}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest().upper()
    headers.update({"client_id": access_id, "t": str(now),
                    "sign_method": "HMAC-SHA256", "sign": sig, "mode": "cors"})
    if token:
        headers["access_token"] = token
    try:
        return requests.get(base + path, headers=headers, timeout=10).json()
    except Exception:
        return {}


def _cloud_devices_via_tinytuya(creds: TuyaCredentials, seed_device_id: str) -> list[dict]:
    """Resolve app user uid via seed device, then list all account devices."""
    try:
        cloud = tinytuya.Cloud(
            apiRegion=creds.region,
            apiKey=creds.access_id,
            apiSecret=creds.access_secret,
            apiDeviceID=seed_device_id or None,
        )

        if seed_device_id:
            # Step 1: look up the seed device to get the real app user uid
            dev_r = cloud.cloudrequest(f"/v1.0/iot-03/devices/{seed_device_id}")
            _LOGGER.debug("Seed device lookup: success=%s", dev_r.get("success"))
            if dev_r.get("success") and isinstance(dev_r.get("result"), dict):
                uid = dev_r["result"].get("uid", "")
                if uid:
                    # Step 2: list all devices for this app user
                    list_r = cloud.cloudrequest(
                        f"/v1.0/iot-03/devices?user_id={uid}&page_size=100&page_no=1"
                    )
                    if list_r.get("success"):
                        devices = list_r.get("result", {}).get("list", [])
                        _LOGGER.debug("Direct uid listing returned %d device(s)", len(devices))
                        if devices:
                            return devices
                    _LOGGER.warning("Device list by uid failed: %s", list_r)
            else:
                _LOGGER.warning("Seed device lookup failed: %s", dev_r)

        # Fallback: tinytuya built-in getdevices() (resolves uid internally)
        _LOGGER.debug("Falling back to tinytuya.Cloud.getdevices()")
        raw = cloud.getdevices()
    except Exception as exc:
        _LOGGER.warning("tinytuya.Cloud discovery failed: %s", exc)
        return []

    if isinstance(raw, list):
        devices = raw
    elif isinstance(raw, dict):
        devices = raw.get("result", {}).get("list", raw.get("list", []))
    else:
        devices = []

    _LOGGER.debug("tinytuya.Cloud.getdevices() returned %d device(s)", len(devices))
    if not devices:
        _LOGGER.warning("No devices from Cloud (seed=%s)", (seed_device_id or "none")[:8])
    return devices


def _cloud_devices_via_udp(creds: TuyaCredentials) -> list[dict]:
    """UDP broadcast scan + per-device cloud lookup (works only on host network)."""
    base = _TUYA_BASE.get(creds.region, _TUYA_BASE["us"])

    token_r = _tuya_get(base, "/v1.0/token?grant_type=1", creds.access_id, creds.access_secret)
    if not token_r.get("success"):
        _LOGGER.warning("Tuya token fetch failed: %s", token_r)
        return []
    token = token_r["result"]["access_token"]

    devices = []
    try:
        scan = tinytuya.deviceScan(verbose=False, maxretry=4)
        _LOGGER.debug("UDP scan returned %d device(s)", len(scan) if isinstance(scan, dict) else 0)
        if isinstance(scan, dict):
            for _ip_key, info in scan.items():
                dev_id = info.get("gwId") or info.get("id", "")
                if not dev_id:
                    continue
                r = _tuya_get(base, f"/v1.0/iot-03/devices/{dev_id}",
                              creds.access_id, creds.access_secret, token)
                if r.get("success") and isinstance(r.get("result"), dict):
                    d = r["result"]
                    d["ip"] = info.get("ip", "")
                    d["mac"] = info.get("mac", "")
                    d["online"] = True
                    devices.append(d)
                else:
                    _LOGGER.debug("Device %s cloud lookup: %s", dev_id[:8], r)
    except Exception as exc:
        _LOGGER.warning("UDP scan failed: %s", exc)

    _LOGGER.debug("UDP discovery found %d device(s)", len(devices))
    return devices


def _cloud_devices(creds: TuyaCredentials, seed_device_id: str = "") -> list[dict]:
    if seed_device_id:
        devices = _cloud_devices_via_tinytuya(creds, seed_device_id)
        if devices:
            return devices
        _LOGGER.warning("Cloud lookup with seed failed, falling back to UDP scan")

    return _cloud_devices_via_udp(creds)


async def discover(creds: TuyaCredentials, hass=None, seed_device_id: str = "") -> list[CameraInfo]:
    loop = asyncio.get_running_loop()
    creds_dict = {"region": creds.region, "access_id": creds.access_id, "access_secret": creds.access_secret}

    # Collect seed from LocalTuya if not provided
    if not seed_device_id and hass:
        for entry in hass.config_entries.async_entries("localtuya"):
            # Try both LocalTuya data structures
            dev_id = entry.data.get("device_id", "")
            if dev_id:
                seed_device_id = dev_id
                break
            for dev_id in entry.data.get("devices", {}).keys():
                seed_device_id = dev_id
                break
            if seed_device_id:
                break
        if seed_device_id:
            _LOGGER.debug("Using LocalTuya device as seed: %s...", seed_device_id[:8])

    devices = await loop.run_in_executor(None, _cloud_devices, creds, seed_device_id)

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
