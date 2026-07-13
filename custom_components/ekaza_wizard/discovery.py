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


# ── LAN IP discovery ──────────────────────────────────────────────────────────

def _ip_from_arp(mac: str) -> str:
    """Look up LAN IP for a MAC by reading the kernel ARP table."""
    if not mac:
        return ""
    try:
        mac_norm = mac.lower().replace("-", ":")
        with open("/proc/net/arp") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[3].lower() == mac_norm:
                    return parts[0]
    except Exception:
        pass
    return ""


def _scan_tuya_port(subnet: str) -> list[str]:
    """Parallel TCP probe of Tuya local port 6668 across a /24 subnet."""
    import concurrent.futures
    import socket

    def _probe(ip: str) -> str | None:
        try:
            with socket.create_connection((ip, 6668), timeout=0.5):
                return ip
        except OSError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        hits = list(pool.map(_probe, (f"{subnet}.{i}" for i in range(1, 255))))
    return [ip for ip in hits if ip]


def _match_ip_by_id(device_id: str, local_key: str, candidates: list[str]) -> str:
    """Return the first candidate IP that responds as device_id via Tuya protocol."""
    for ip in candidates:
        try:
            dev = tinytuya.Device(device_id, ip, local_key, version=3.5)
            dev.set_socketTimeout(2)
            st = dev.status()
            if isinstance(st, dict) and not st.get("Error"):
                return ip
        except Exception:
            pass
    return ""


def _local_subnet() -> str:
    """Detect the LAN subnet (e.g. '192.168.15') by probing a UDP route."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("172.") and "." in ip:
            return ip.rsplit(".", 1)[0]
    except Exception:
        pass
    return ""


async def _enrich_ips(devices: list[dict]) -> None:
    """Fill in missing 'ip' fields for cloud-discovered devices. Modifies in-place."""
    missing = [d for d in devices if not d.get("ip")]
    if not missing:
        return

    loop = asyncio.get_running_loop()

    # 1. ARP lookup — instant, reads /proc/net/arp
    for dev in missing:
        mac = dev.get("mac", "")
        if mac:
            ip = await loop.run_in_executor(None, _ip_from_arp, mac)
            if ip:
                dev["ip"] = ip
                _LOGGER.debug("ARP resolved %s → %s", mac[:11], ip)

    # 2. Subnet port-scan fallback for anything still missing
    still_missing = [d for d in devices if not d.get("ip")]
    if not still_missing:
        return

    subnet = await loop.run_in_executor(None, _local_subnet)
    if not subnet:
        _LOGGER.warning("Could not detect LAN subnet; IP must be entered manually")
        return

    _LOGGER.debug("Scanning %s.x for Tuya devices (port 6668)…", subnet)
    open_ips = await loop.run_in_executor(None, _scan_tuya_port, subnet)
    _LOGGER.debug("Tuya scan found %d candidate(s): %s", len(open_ips), open_ips)

    # Single unknown device + single found IP → direct match
    if len(still_missing) == 1 and len(open_ips) == 1:
        still_missing[0]["ip"] = open_ips[0]
        _LOGGER.debug("IP matched by elimination: %s", open_ips[0])
        return

    # Multiple: authenticate via Tuya protocol to match device_id
    for dev in still_missing:
        lk = dev.get("key") or dev.get("local_key", "")
        did = dev.get("id", "")
        if lk and did:
            ip = await loop.run_in_executor(None, _match_ip_by_id, did, lk, open_ips)
            if ip:
                dev["ip"] = ip
                _LOGGER.debug("IP matched by handshake: %s → %s", did[:8], ip)


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
    # Always try Cloud API first (works with project credentials alone)
    devices = _cloud_devices_via_tinytuya(creds, seed_device_id)
    if devices:
        return devices
    # Fallback: UDP broadcast scan (only works on host-mode networks)
    _LOGGER.warning("Cloud API returned no devices, falling back to UDP scan")
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

    # Enrich missing IPs via ARP + subnet scan before filtering
    await _enrich_ips(devices)

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

        # tinytuya.Cloud.getdevices() returns "key"; direct API returns "local_key"
        local_key = dev.get("local_key") or dev.get("key", "")
        ip = dev.get("ip", "")
        cameras.append(CameraInfo(
            name=dev.get("name", device_id),
            slug=slug,
            device_id=device_id,
            local_key=local_key,
            ip=ip,
            mac=dev.get("mac", ""),
            product_id=product_id,
            rtsp_password=creds.default_rtsp_password,
            rtsp_username=creds.rtsp_username,
            online=dev.get("online", True),
        ))
    return cameras
