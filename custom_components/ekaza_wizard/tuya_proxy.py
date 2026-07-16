"""MITM TLS proxy: intercepts camera Tuya cloud MQTT events → triggers Frigate.

Architecture:
  Camera ──TLS:8883──► [AdGuard DNS redirect → HA IP] ──► iptables PREROUTING REDIRECT
                                                                  │ :8883 → proxy_port
                                                                  ▼
                                                          TuyaProxy (:proxy_port)
                                                                  │
                         lê SNI do TLS ClientHello ───────────────┤
                         gera cert auto-assinado ─────────────────┤
                                                                  │
                         inspeciona MQTT PUBLISH ─────────────────┤
                         DP 185 encontrado → fire_event            │
                                                                  │
                         (não repassa à Tuya cloud — SmartLife bloqueado)

Porta padrão do proxy: 18883 (8883 pode estar em uso pelo Mosquitto MQTT/TLS).
iptables PREROUTING REDIRECT por IP de câmera: 8883 → proxy_port.

Dependências: ssl, struct, asyncio (stdlib) + cryptography (dep HA existente).
Sem novos requirements no manifest.json.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import ssl
import struct
from pathlib import Path
from typing import Awaitable, Callable

from homeassistant.core import HomeAssistant

from .models import CameraInfo

_LOGGER = logging.getLogger(__name__)
_CAMERA_MQTT_PORT = 8883  # Port cameras connect to (via DNS rewrite → HA IP)
_PROXY_DEFAULT_PORT = 18883  # Default listen port (avoids conflict with Mosquitto TLS)
_UPSTREAM_PORT = 8883  # Real Tuya upstream port (used in transparent proxy mode)
_ALARM_DP = 185
_CERT_DIR = Path("/config/.storage/ekaza_wizard_proxy_certs")


# ---------------------------------------------------------------------------
# TLS certificate helpers
# ---------------------------------------------------------------------------


def _generate_cert(domain: str, cert_path: Path, key_path: Path) -> None:
    """Generate a self-signed cert for domain using cryptography (already HA dep)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(domain)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    _LOGGER.debug("Proxy: generated TLS cert for %s", domain)


def _get_or_create_cert_sync(domain: str) -> tuple[str, str]:
    """Return (cert_path, key_path) for domain, generating on first call."""
    _CERT_DIR.mkdir(parents=True, exist_ok=True)
    safe = domain.replace(".", "_").replace("*", "wildcard")
    cert_path = _CERT_DIR / f"{safe}.crt"
    key_path = _CERT_DIR / f"{safe}.key"
    if not cert_path.exists() or not key_path.exists():
        _generate_cert(domain, cert_path, key_path)
    return str(cert_path), str(key_path)


# ---------------------------------------------------------------------------
# MQTT wire protocol helpers
# ---------------------------------------------------------------------------


def _parse_mqtt_packets(buf: bytes) -> tuple[list[tuple[int, int, bytes]], bytes]:
    """Parse complete MQTT packets from buf.

    Returns ([(msg_type, flags, payload), ...], remainder).
    Does not consume incomplete packets — they stay in remainder for next call.
    """
    packets: list[tuple[int, int, bytes]] = []
    offset = 0
    while offset < len(buf):
        if offset + 1 >= len(buf):
            break  # need at least 2 bytes

        first_byte = buf[offset]
        msg_type = (first_byte >> 4) & 0xF
        flags = first_byte & 0xF

        # Decode variable-length remaining-length field
        multiplier, remaining_len, i = 1, 0, offset + 1
        while i < len(buf) and i < offset + 5:
            byte = buf[i]
            remaining_len += (byte & 0x7F) * multiplier
            multiplier *= 128
            i += 1
            if not (byte & 0x80):
                break
        else:
            break  # length field incomplete

        header_len = i - offset
        total = header_len + remaining_len
        if offset + total > len(buf):
            break  # payload incomplete

        payload = buf[offset + header_len : offset + total]
        packets.append((msg_type, flags, payload))
        offset += total

    return packets, buf[offset:]


def _decode_publish(flags: int, payload: bytes) -> tuple[str, bytes] | None:
    """Return (topic, message_body) from a MQTT PUBLISH payload, or None on error."""
    try:
        if len(payload) < 2:
            return None
        topic_len = struct.unpack(">H", payload[:2])[0]
        if len(payload) < 2 + topic_len:
            return None
        topic = payload[2 : 2 + topic_len].decode("utf-8", errors="ignore")
        body_start = 2 + topic_len
        qos = (flags >> 1) & 0x3
        if qos > 0:
            body_start += 2  # skip packet identifier
        return topic, payload[body_start:]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Port availability check
# ---------------------------------------------------------------------------


def port_is_free(port: int) -> bool:
    """Return True if the TCP port is available to bind (blocking — run in executor)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


async def async_port_is_free(port: int) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, port_is_free, port)


# ---------------------------------------------------------------------------
# iptables PREROUTING REDIRECT helpers
# ---------------------------------------------------------------------------


async def _iptables_nat(action: str, chain: str, *args: str) -> tuple[bool, str]:
    """Run `iptables -t nat <action> <chain> [args]` and return (ok, output)."""
    cmd = ["iptables", "-t", "nat", action, chain, *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
        return proc.returncode == 0, (err or out).decode().strip()
    except Exception as exc:
        return False, str(exc)


def _redirect_args(camera_ip: str, from_port: int, to_port: int) -> tuple[str, ...]:
    return (
        "-s",
        camera_ip,
        "-p",
        "tcp",
        "--dport",
        str(from_port),
        "-j",
        "REDIRECT",
        "--to-port",
        str(to_port),
    )


async def _add_camera_redirect(camera_ip: str, from_port: int, to_port: int) -> bool:
    """Add PREROUTING REDIRECT rule for camera_ip if not already present."""
    args = _redirect_args(camera_ip, from_port, to_port)
    exists, _ = await _iptables_nat("-C", "PREROUTING", *args)
    if exists:
        return True
    ok, msg = await _iptables_nat("-A", "PREROUTING", *args)
    if not ok:
        _LOGGER.warning(
            "iptables: failed to add redirect %s:%d→%d: %s",
            camera_ip,
            from_port,
            to_port,
            msg,
        )
    else:
        _LOGGER.warning(
            "iptables: added redirect %s:%d → :%d", camera_ip, from_port, to_port
        )
    return ok


async def _remove_camera_redirect(camera_ip: str, from_port: int, to_port: int) -> None:
    """Remove PREROUTING REDIRECT rule for camera_ip if present."""
    args = _redirect_args(camera_ip, from_port, to_port)
    exists, _ = await _iptables_nat("-C", "PREROUTING", *args)
    if not exists:
        return
    ok, msg = await _iptables_nat("-D", "PREROUTING", *args)
    if ok:
        _LOGGER.warning(
            "iptables: removed redirect %s:%d → :%d", camera_ip, from_port, to_port
        )
    else:
        _LOGGER.warning(
            "iptables: failed to remove redirect %s:%d: %s", camera_ip, from_port, msg
        )


# ---------------------------------------------------------------------------
# TuyaProxy
# ---------------------------------------------------------------------------


class TuyaProxy:
    """MITM TLS proxy: intercepts camera MQTT, fires events to Frigate, blocks SmartLife.

    Listens on proxy_port (default 18883). iptables PREROUTING REDIRECT rules per camera
    IP transparently redirect camera connections from :8883 → :proxy_port.
    """

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._hass: HomeAssistant | None = None
        self._fire_fn: Callable[[str, HomeAssistant], Awaitable] | None = None
        self._cameras: dict[str, CameraInfo] = {}  # ip → CameraInfo
        self._sni_map: dict[int, str] = {}  # id(ssl_obj) → SNI domain
        self._proxy_port: int = _PROXY_DEFAULT_PORT

    @property
    def proxy_port(self) -> int:
        return self._proxy_port

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_cameras(self, cameras: list[CameraInfo]) -> None:
        old_ips = set(self._cameras)
        self._cameras = {c.ip: c for c in cameras if c.proxy_enabled}
        new_ips = set(self._cameras)
        if self._server is not None:
            asyncio.get_event_loop().create_task(
                self._reconcile_iptables(old_ips, new_ips)
            )

    async def _reconcile_iptables(self, old_ips: set[str], new_ips: set[str]) -> None:
        for ip in new_ips - old_ips:
            await _add_camera_redirect(ip, _CAMERA_MQTT_PORT, self._proxy_port)
        for ip in old_ips - new_ips:
            await _remove_camera_redirect(ip, _CAMERA_MQTT_PORT, self._proxy_port)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        """Build the server SSL context (blocking I/O — run in executor)."""
        try:
            default_cert, default_key = _get_or_create_cert_sync("ekaza-proxy.local")
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(default_cert, default_key)
            ctx.set_servername_callback(self._sni_callback)
            return ctx
        except Exception as exc:
            _LOGGER.error("Tuya proxy: SSL context error: %s", exc)
            return None

    async def start(
        self,
        hass: HomeAssistant,
        cameras: list[CameraInfo],
        fire_fn: Callable[[str, HomeAssistant], Awaitable],
        port: int = _PROXY_DEFAULT_PORT,
    ) -> None:
        if self._server is not None:
            self.update_cameras(cameras)
            _LOGGER.debug("Proxy already running — camera list updated")
            return

        self._hass = hass
        self._fire_fn = fire_fn
        self._proxy_port = port

        # Check port availability before trying to bind
        loop = asyncio.get_event_loop()
        free = await loop.run_in_executor(None, port_is_free, port)
        if not free:
            _LOGGER.error(
                "Tuya proxy: port %d is in use — cannot start. "
                "Change proxy_port in eKaza Wizard settings or free the port.",
                port,
            )
            return

        ssl_ctx = await loop.run_in_executor(None, self._build_ssl_context)
        if ssl_ctx is None:
            _LOGGER.error("Tuya proxy: SSL context setup failed, proxy not started")
            return

        try:
            self._server = await asyncio.start_server(
                self._handle_connection,
                host="0.0.0.0",
                port=port,
                ssl=ssl_ctx,
            )
            _LOGGER.warning("Tuya MITM proxy listening on :%d", port)
        except OSError as exc:
            _LOGGER.error("Tuya proxy: cannot bind :%d — %s", port, exc)
            self._server = None
            return

        # Add iptables PREROUTING REDIRECT rules for each proxy-enabled camera
        self._cameras = {c.ip: c for c in cameras if c.proxy_enabled}
        for ip in self._cameras:
            await _add_camera_redirect(ip, _CAMERA_MQTT_PORT, port)

    async def stop(self) -> None:
        if self._server:
            # Remove iptables rules before closing
            for ip in list(self._cameras):
                await _remove_camera_redirect(ip, _CAMERA_MQTT_PORT, self._proxy_port)
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            _LOGGER.warning("Tuya MITM proxy stopped")

    def is_running(self) -> bool:
        return self._server is not None

    # ------------------------------------------------------------------
    # TLS SNI callback (synchronous — called by OpenSSL during handshake)
    # ------------------------------------------------------------------

    def _sni_callback(
        self,
        ssl_object: ssl.SSLObject,
        server_name: str | None,
        _original_ctx: ssl.SSLContext,
    ) -> None:
        if not server_name:
            return
        # Store SNI so _handle_connection can retrieve it
        self._sni_map[id(ssl_object)] = server_name
        try:
            cert, key = _get_or_create_cert_sync(server_name)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            ssl_object.context = ctx
        except Exception as exc:
            _LOGGER.warning("Proxy SNI callback failed for %s: %s", server_name, exc)

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else "unknown"

        ssl_obj = writer.transport.get_extra_info("ssl_object")
        domain = self._sni_map.pop(id(ssl_obj), None)

        if not domain:
            _LOGGER.debug("Proxy: no SNI from %s — dropping", peer_ip)
            writer.close()
            return

        cam = self._cameras.get(peer_ip)
        _LOGGER.warning(
            "Proxy: %s → %s (câmera: %s)",
            peer_ip,
            domain,
            cam.slug if cam else "desconhecida",
        )

        if cam:
            # Known camera in cam_fr mode: intercept only, no upstream relay.
            # This cuts the camera off from Tuya cloud → SmartLife stops receiving events.
            await self._intercept_only(reader, writer, cam)
        else:
            # Unknown camera: transparent proxy to real Tuya upstream.
            await self._transparent_proxy(reader, writer, domain)

    async def _intercept_only(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        cam: CameraInfo,
    ) -> None:
        """Read MQTT from camera, fire Frigate events, never relay to Tuya cloud."""
        remainder = b""
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(4096), timeout=120)
                except asyncio.TimeoutError:
                    break
                if not data:
                    break
                remainder = await self._inspect(remainder + data, cam)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _transparent_proxy(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        domain: str,
    ) -> None:
        """Transparent relay to real Tuya upstream for unconfigured cameras."""
        try:
            upstream_ctx = ssl.create_default_context()
            ur, uw = await asyncio.wait_for(
                asyncio.open_connection(domain, _UPSTREAM_PORT, ssl=upstream_ctx),
                timeout=10,
            )
        except Exception as exc:
            _LOGGER.warning("Proxy: upstream %s unreachable: %s", domain, exc)
            writer.close()
            return

        try:
            await asyncio.gather(
                self._pipe(reader, uw),
                self._pipe(ur, writer),
            )
        except Exception:
            pass
        finally:
            for w in (writer, uw):
                try:
                    w.close()
                except Exception:
                    pass

    async def _pipe(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Bidirectional pipe without inspection."""
        while True:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=120)
            except asyncio.TimeoutError:
                break
            if not data:
                break
            writer.write(data)
            try:
                await writer.drain()
            except Exception:
                break

    # ------------------------------------------------------------------
    # MQTT inspection
    # ------------------------------------------------------------------

    async def _inspect(self, buf: bytes, cam: CameraInfo) -> bytes:
        """Parse MQTT packets, fire on alarm DP. Returns unconsumed remainder."""
        packets, remainder = _parse_mqtt_packets(buf)
        for msg_type, flags, payload in packets:
            if msg_type == 3:  # PUBLISH
                result = _decode_publish(flags, payload)
                if result:
                    topic, body = result
                    _LOGGER.debug(
                        "Proxy MQTT PUBLISH cam=%s topic=%s body_len=%d",
                        cam.slug,
                        topic,
                        len(body),
                    )
                    if await self._decode_tuya_event(body, cam):
                        _LOGGER.warning(
                            "Proxy: evento de movimento detectado — câmera %s", cam.slug
                        )
        return remainder

    async def _decode_tuya_event(self, body: bytes, cam: CameraInfo) -> bool:
        """Try to detect alarm DP in Tuya MQTT payload. Returns True if found.

        Logs raw bytes at DEBUG level to aid format discovery on first deployments.
        Tries multiple Tuya payload formats in order of likelihood.
        """
        _LOGGER.debug("Proxy payload hex (%s): %s", cam.slug, body[:120].hex())

        # Format 1: plain JSON with dps at root or nested
        #   {"dps": {"185": "..."}}
        #   {"data": {"dps": {"185": "..."}}}
        #   {"status": [{"code": "alarm_message", "value": "..."}]}
        try:
            text = body.decode("utf-8", errors="ignore").strip()
            if text.startswith("{"):
                data = json.loads(text)
                dps = (
                    data.get("dps")
                    or data.get("data", {}).get("dps")
                    or data.get("status", {})
                    or {}
                )
                if str(_ALARM_DP) in dps or _ALARM_DP in dps:
                    asyncio.create_task(self._fire(cam))
                    return True
        except Exception:
            pass

        # Format 2: JSON embedded in binary payload (Tuya protocol wrapper)
        try:
            start = body.find(b'{"')
            if start >= 0:
                fragment = body[start:].decode("utf-8", errors="ignore")
                depth = end = 0
                for idx, ch in enumerate(fragment):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = idx + 1
                            break
                if end:
                    data = json.loads(fragment[:end])
                    dps = data.get("dps") or {}
                    if str(_ALARM_DP) in dps or _ALARM_DP in dps:
                        asyncio.create_task(self._fire(cam))
                        return True
        except Exception:
            pass

        return False

    async def _fire(self, cam: CameraInfo) -> None:
        if self._fire_fn and self._hass:
            try:
                await self._fire_fn(cam.slug, self._hass)
            except Exception as exc:
                _LOGGER.warning("Proxy: fire event failed for %s: %s", cam.slug, exc)


# ---------------------------------------------------------------------------
# Module-level singleton + public API
# ---------------------------------------------------------------------------

_proxy = TuyaProxy()


async def start(
    hass: HomeAssistant,
    cameras: list[CameraInfo],
    fire_fn: Callable[[str, HomeAssistant], Awaitable],
    port: int = _PROXY_DEFAULT_PORT,
) -> None:
    """Start the proxy (or update camera list if already running)."""
    await _proxy.start(hass, cameras, fire_fn, port=port)


async def stop() -> None:
    await _proxy.stop()


def update_cameras(cameras: list[CameraInfo]) -> None:
    _proxy.update_cameras(cameras)


def proxy_port() -> int:
    return _proxy.proxy_port


def is_running() -> bool:
    return _proxy.is_running()
