"""Camera discovery: Tuya cloud (primary) + local UDP scan (IP enrichment)."""
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
    except Exception as exc:
        _LOGGER.debug("Tuya GET %s failed: %s", path, exc)
        return {}


def _cloud_devices(creds: TuyaCredentials) -> list[dict]:
    base = _TUYA_BASE.get(creds.region, _TUYA_BASE["us"])

    # 1. Get token + uid (uid identifies the account owner)
    token_r = _tuya_get(base, "/v1.0/token?grant_type=1", creds.access_id, creds.access_secret)
    if not token_r.get("success"):
        _LOGGER.warning("Tuya token failed: %s", token_r.get("msg", token_r))
        return []
    token = token_r["result"]["access_token"]
    uid = token_r["result"].get("uid", "")
    if not uid:
        _LOGGER.warning("Tuya token response missing uid — check access_id/secret/region")
        return []

    # 2. List ALL devices in account via Cloud API (no UDP scan required)
    devices: list[dict] = []
    page = 1
    while True:
        r = _tuya_get(
            base,
            f"/v1.0/iot-03/devices?user_id={uid}&page_size=100&page_no={page}&schema=1",
            creds.access_id, creds.access_secret, token,
        )
        if not r.get("success"):
            _LOGGER.warning("Device list page %d failed: %s", page, r.get("msg", r))
            break
        batch = r.get("result", {}).get("list", [])
        if not batch:
            break
        devices.extend(batch)
        _LOGGER.debug("Tuya cloud: %d devices on page %d", len(batch), page)
        if len(batch) < 100:
            break
        page += 1

    if not devices:
        _LOGGER.warning("No devices returned from Tuya Cloud for uid=%s", uid)
        return []

    # 3. UDP scan for IP enrichment (best-effort — may fail in container environments)
    local: dict[str, dict] = {}
    try:
        scan = tinytuya.deviceScan(verbose=False, maxretry=4)
        if isinstance(scan, dict):
            for info in scan.values():
                dev_id = info.get("gwId") or info.get("id", "")
                if dev_id:
                    local[dev_id] = info
        _LOGGER.debug("UDP scan found %d devices", len(local))
    except Exception as exc:
        _LOGGER.debug("UDP scan failed (normal in container): %s", exc)

    # 4. Merge local IPs into cloud device list
    for dev in devices:
        dev_id = dev.get("id", "")
        if dev_id in local:
            dev["ip"] = local[dev_id].get("ip", dev.get("ip", ""))
            dev["mac"] = local[dev_id].get("mac", dev.get("mac", ""))
        else:
            dev.setdefault("ip", "")
            dev.setdefault("mac", "")

    return devices


def _local_scan() -> dict[str, dict]:
    result = tinytuya.deviceScan(verbose=False, maxretry=6)
    return result if isinstance(result, dict) else {}


async def discover(creds: TuyaCredentials) -> list[CameraInfo]:
    loop = asyncio.get_running_loop()
    creds_dict = {"region": creds.region, "access_id": creds.access_id, "access_secret": creds.access_secret}

    devices = await loop.run_in_executor(None, _cloud_devices, creds)

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
