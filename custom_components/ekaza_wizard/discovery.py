"""Camera discovery: Tuya cloud + local UDP scan."""
import asyncio
import hashlib
import hmac
import re
import time

import requests
import tinytuya

from . import schema_store
from .models import CameraInfo, TuyaCredentials

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


def _cloud_devices(creds: TuyaCredentials) -> list[dict]:
    base = _TUYA_BASE.get(creds.region, _TUYA_BASE["us"])

    # Get token
    token_r = _tuya_get(base, "/v1.0/token?grant_type=1", creds.access_id, creds.access_secret)
    if not token_r.get("success"):
        return []
    token = token_r["result"]["access_token"]

    # Get device by known device ID from local scan first
    devices = []
    try:
        scan = tinytuya.deviceScan(verbose=False, maxretry=4)
        if isinstance(scan, dict):
            for ip_key, info in scan.items():
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
    except Exception:
        pass

    return devices


def _local_scan() -> dict[str, dict]:
    result = tinytuya.deviceScan(verbose=False, maxretry=6)
    return result if isinstance(result, dict) else {}


async def discover(creds: TuyaCredentials) -> list[CameraInfo]:
    loop = asyncio.get_event_loop()
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
