"""Home Assistant API client — REST (via Supervisor proxy) + WebSocket."""
import json
import os
from typing import Any, Callable, Coroutine

import aiohttp
import websockets

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
_SUPERVISOR = "http://supervisor"
_CORE_API = f"{_SUPERVISOR}/core/api"
_WS_URL = "ws://supervisor/core/websocket"


def _auth_header() -> dict:
    return {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}


# ── Supervisor REST ────────────────────────────────────────────────────────────

async def supervisor_get(path: str) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{_SUPERVISOR}{path}", headers=_auth_header()) as r:
            return await r.json()


async def supervisor_post(path: str, data: dict | None = None) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{_SUPERVISOR}{path}", headers=_auth_header(), json=data or {}
        ) as r:
            return await r.json()


async def addon_restart(slug: str) -> bool:
    r = await supervisor_post(f"/addons/{slug}/restart")
    return r.get("result") == "ok"


async def addon_info(slug: str) -> dict:
    r = await supervisor_get(f"/addons/{slug}/info")
    return r.get("data", {})


# ── HA Core REST (via Supervisor proxy) ───────────────────────────────────────

async def call_service(domain: str, service: str, data: dict | None = None) -> Any:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{_CORE_API}/services/{domain}/{service}",
            headers=_auth_header(),
            json=data or {},
        ) as r:
            return await r.json()


async def get_states() -> list[dict]:
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{_CORE_API}/states", headers=_auth_header()) as r:
            return await r.json()


# ── HA WebSocket ───────────────────────────────────────────────────────────────

async def _ws_send_recv(ws, msg_id: int, msg_type: str, **kwargs) -> dict:
    payload = {"id": msg_id, "type": msg_type, **kwargs}
    await ws.send(json.dumps(payload))
    while True:
        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("id") == msg_id:
            return msg


async def with_websocket(
    action: Callable[..., Coroutine]
) -> Any:
    """Open an authenticated WS connection and run action(ws, next_id_fn)."""
    counter = [1]

    def next_id() -> int:
        n = counter[0]
        counter[0] += 1
        return n

    async with websockets.connect(_WS_URL) as ws:
        # handshake
        msg = json.loads(await ws.recv())
        assert msg["type"] == "auth_required", msg
        await ws.send(json.dumps({"type": "auth", "access_token": SUPERVISOR_TOKEN}))
        msg = json.loads(await ws.recv())
        if msg["type"] != "auth_ok":
            raise PermissionError(f"WebSocket auth failed: {msg}")

        return await action(ws, next_id)


# ── Lovelace ──────────────────────────────────────────────────────────────────

async def lovelace_get_config() -> dict | None:
    async def _action(ws, next_id):
        r = await _ws_send_recv(ws, next_id(), "lovelace/config", force=False)
        if r.get("success"):
            return r["result"]
        return None

    return await with_websocket(_action)


async def lovelace_save_config(config: dict) -> bool:
    async def _action(ws, next_id):
        r = await _ws_send_recv(ws, next_id(), "lovelace/config/save", config=config)
        return r.get("success", False)

    return await with_websocket(_action)


# ── Config flow (generic) ─────────────────────────────────────────────────────

async def config_flow_run(handler: str, steps: list[dict]) -> dict:
    """
    Drive a HA config entry flow to completion.

    steps: list of dicts, one per form step in order.
    Returns the final flow result (type == "create_entry" on success).
    """
    async def _action(ws, next_id):
        # init
        r = await _ws_send_recv(ws, next_id(), "config_entries/flow/init", handler=handler)
        result = r.get("result", {})

        for step_data in steps:
            if result.get("type") not in ("form", "menu"):
                break
            flow_id = result["flow_id"]
            r = await _ws_send_recv(
                ws, next_id(),
                "config_entries/flow/progress",
                flow_id=flow_id,
                user_input=step_data,
            )
            result = r.get("result", {})

        return result

    return await with_websocket(_action)


async def reload_scripts() -> bool:
    try:
        await call_service("script", "reload")
        return True
    except Exception:
        return False
