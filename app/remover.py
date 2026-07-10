"""Removes eKaza cameras from go2rtc, Frigate config, and LocalTuya."""
from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator

import frigate
import ha_client


def _event(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _ip_from_stream(stream_entry) -> str | None:
    """Extract camera IP from a Frigate go2rtc stream entry."""
    first = stream_entry[0] if isinstance(stream_entry, list) and stream_entry else stream_entry
    m = re.search(r"@([\d.]+):", str(first or ""))
    return m.group(1) if m else None


async def list_cameras(frigate_slug: str) -> list[str]:
    """Return sorted list of camera slugs currently in Frigate config."""
    path = frigate.find_config(frigate_slug)
    if path is None:
        return []
    config = frigate._read(path)
    return sorted(config.get("cameras", {}).keys())


async def remove(slugs: list[str], frigate_slug: str) -> AsyncGenerator[str, None]:
    """SSE generator — removes cameras from go2rtc, Frigate, and LocalTuya."""
    path = frigate.find_config(frigate_slug)
    if path is None:
        yield _event({"step": "init", "name": "global", "ok": False,
                      "detail": "Config Frigate não encontrada"})
        return

    config = frigate._read(path)
    streams = config.get("go2rtc", {}).get("streams", {})
    cameras_cfg = config.get("cameras", {})

    ips: list[str] = []
    for slug in slugs:
        ip = _ip_from_stream(streams.get(slug))
        if ip:
            ips.append(ip)

        removed_streams = sum(1 for k in (slug, f"{slug}_sub") if streams.pop(k, None) is not None)
        yield _event({"step": "go2rtc", "name": slug, "ok": True,
                      "detail": f"{removed_streams} stream(s) removidos"})

        had = cameras_cfg.pop(slug, None) is not None
        yield _event({"step": "frigate", "name": slug, "ok": had,
                      "detail": "Câmera removida da config" if had else "Câmera não encontrada"})

    if ips:
        yield _event({"step": "localtuya", "name": "global", "ok": False,
                      "detail": "Removendo entidades LocalTuya…"})
        count = await ha_client.remove_localtuya_by_host(ips)
        yield _event({"step": "localtuya", "name": "global", "ok": True,
                      "detail": f"{count} entidade(s) LocalTuya removidas"})

    yield _event({"step": "frigate_save", "name": "global", "ok": False, "detail": "Salvando config…"})
    try:
        frigate._write(path, config)
        yield _event({"step": "frigate_save", "name": "global", "ok": True, "detail": "Config salva"})
    except Exception as e:
        yield _event({"step": "frigate_save", "name": "global", "ok": False, "detail": str(e)})
        return

    yield _event({"step": "restart_frigate", "name": "global", "ok": False, "detail": "Reiniciando Frigate…"})
    ok = await ha_client.addon_restart(frigate_slug)
    yield _event({"step": "restart_frigate", "name": "global", "ok": ok,
                  "detail": "Frigate reiniciando" if ok else "Erro ao reiniciar Frigate"})

    yield _event({"step": "done", "name": "global", "ok": True,
                  "detail": f"{len(slugs)} câmera(s) removida(s)"})
