"""AdGuard Home API client for SmartLife/Tuya cloud blocking and DNS rewrites.

All operations use the Supervisor backup API (create → download → modify YAML → upload → restore)
because AdGuard binds its HTTP API only to localhost, unreachable from the HA container.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tarfile
from pathlib import Path

import aiohttp
import yaml

_LOGGER = logging.getLogger(__name__)

_RULE_START = "! ekaza-smartlife-start"
_RULE_END = "! ekaza-smartlife-end"

BLOCK_DOMAINS = [
    "tuyaeu.com",
    "tuyacn.com",
    "tuyaus.com",
    "tuyain.com",
    "tuya.com",
    "smart-life.com",
    "smartlifeapp.com",
    "fogcloud.io",
    "nebulae-iot.com",
]

_BLOCK_RULES = (
    [_RULE_START, "! Bloqueio SmartLife/Tuya — gerenciado pelo Ekaza Wizard"]
    + [f"||{d}^" for d in BLOCK_DOMAINS]
    + [_RULE_END]
)

_SUP = "http://supervisor"
_STATUS_FILE = Path("/config/.ekaza_adguard_status")

# Cached slug after first successful detection
_adguard_slug: str | None = None

_REWRITES_CACHE_FILE = Path("/config/.ekaza_adguard_rewrites")


def _read_cached_rewrites() -> list[dict]:
    try:
        return json.loads(_REWRITES_CACHE_FILE.read_text())
    except Exception:
        return []


def _write_cached_rewrites(rewrites: list[dict]) -> None:
    try:
        _REWRITES_CACHE_FILE.write_text(json.dumps(rewrites))
    except Exception as exc:
        _LOGGER.warning("Could not write rewrite cache: %s", exc)


async def _async_write_cached_rewrites(rewrites: list[dict]) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _write_cached_rewrites, rewrites)


async def _get_adguard_slug() -> str:
    """Return the installed AdGuard add-on slug, auto-detecting via Supervisor API."""
    global _adguard_slug
    if _adguard_slug:
        return _adguard_slug
    data = await _sup_get("/addons")
    if data:
        for addon in data.get("data", {}).get("addons", []):
            slug = addon.get("slug", "")
            if "adguard" in slug.lower():
                _adguard_slug = slug
                return slug
    _LOGGER.warning("AdGuard add-on not found via Supervisor API")
    return ""


def _read_cached_status() -> str | None:
    try:
        return _STATUS_FILE.read_text().strip()
    except Exception:
        return None


def _write_cached_status(active: bool) -> None:
    try:
        _STATUS_FILE.write_text("active" if active else "inactive")
    except Exception as exc:
        _LOGGER.warning("Could not write AdGuard status cache: %s", exc)


async def get_rules_status() -> dict:
    """Return {"active": bool, "domains": int, "source": "cache"|"unknown"}."""
    cached = _read_cached_status()
    if cached is not None:
        return {
            "active": cached == "active",
            "domains": len(BLOCK_DOMAINS),
            "source": "cache",
        }
    return {"active": False, "domains": len(BLOCK_DOMAINS), "source": "unknown"}


def _sup_headers() -> dict:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


# ---------------------------------------------------------------------------
# Backup helpers (pure Python, no subprocess)
# ---------------------------------------------------------------------------


def _extract_yaml_from_backup(backup_bytes: bytes, addon_slug: str) -> bytes:
    """Extract AdGuardHome.yaml content from a supervisor partial backup."""
    outer = tarfile.open(fileobj=io.BytesIO(backup_bytes))
    try:
        inner_fobj = outer.extractfile(f"{addon_slug}.tar.gz")
    except KeyError:
        inner_fobj = None
    if inner_fobj is None:
        raise ValueError(f"addon tar '{addon_slug}.tar.gz' not found in backup")
    inner = tarfile.open(fileobj=io.BytesIO(inner_fobj.read()), mode="r:gz")
    yml_fobj = inner.extractfile("data/adguard/AdGuardHome.yaml")
    if yml_fobj is None:
        raise ValueError("AdGuardHome.yaml not found in addon tar")
    return yml_fobj.read()


def _modify_adguard_yaml(backup_bytes: bytes, modifier, addon_slug: str) -> bytes:
    """Call modifier(config_dict) in place and return modified backup bytes."""
    outer = tarfile.open(fileobj=io.BytesIO(backup_bytes))

    inner_fobj = outer.extractfile(f"{addon_slug}.tar.gz")
    inner = tarfile.open(fileobj=io.BytesIO(inner_fobj.read()), mode="r:gz")
    new_inner_buf = io.BytesIO()
    with tarfile.open(fileobj=new_inner_buf, mode="w:gz") as new_inner:
        for member in inner.getmembers():
            fobj = inner.extractfile(member)
            if member.name == "data/adguard/AdGuardHome.yaml" and fobj:
                config = yaml.safe_load(fobj.read())
                modifier(config)
                new_content = yaml.dump(
                    config, default_flow_style=False, allow_unicode=True
                ).encode("utf-8")
                info = tarfile.TarInfo(name=member.name)
                info.size = len(new_content)
                info.mode = member.mode
                info.mtime = member.mtime
                new_inner.addfile(info, io.BytesIO(new_content))
            elif fobj:
                new_inner.addfile(member, fobj)
            else:
                new_inner.addfile(member)
    inner_modified = new_inner_buf.getvalue()

    new_outer_buf = io.BytesIO()
    with tarfile.open(fileobj=new_outer_buf, mode="w") as new_outer:
        for member in outer.getmembers():
            fobj = outer.extractfile(member)
            if member.name == f"{addon_slug}.tar.gz":
                info = tarfile.TarInfo(name=member.name)
                info.size = len(inner_modified)
                info.mode = member.mode
                info.mtime = member.mtime
                new_outer.addfile(info, io.BytesIO(inner_modified))
            elif fobj:
                new_outer.addfile(member, fobj)
            else:
                new_outer.addfile(member)
    return new_outer_buf.getvalue()


def _modify_yaml_in_backup(
    backup_bytes: bytes, new_rules: list[str], addon_slug: str
) -> bytes:
    """Return modified backup bytes with updated user_rules in AdGuardHome.yaml."""

    def _patch(cfg: dict) -> None:
        cfg["user_rules"] = new_rules

    return _modify_adguard_yaml(backup_bytes, _patch, addon_slug)


# ---------------------------------------------------------------------------
# Supervisor API helpers
# ---------------------------------------------------------------------------


async def _sup_get(path: str) -> dict | None:
    hdrs = _sup_headers()
    if not hdrs:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.get(
                f"{_SUP}{path}", headers=hdrs, timeout=aiohttp.ClientTimeout(total=10)
            )
            if r.status == 200:
                return await r.json()
            _LOGGER.debug("Supervisor GET %s → %s", path, r.status)
    except Exception as exc:
        _LOGGER.debug("Supervisor GET %s failed: %s", path, exc)
    return None


async def _sup_post(path: str, json_body: dict, timeout: int = 60) -> dict | None:
    hdrs = _sup_headers()
    if not hdrs:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.post(
                f"{_SUP}{path}",
                headers=hdrs,
                json=json_body,
                timeout=aiohttp.ClientTimeout(total=timeout),
            )
            if r.status in (200, 201):
                return await r.json()
            text = await r.text()
            _LOGGER.debug("Supervisor POST %s → %s: %s", path, r.status, text[:100])
    except Exception as exc:
        _LOGGER.debug("Supervisor POST %s failed: %s", path, exc)
    return None


async def _sup_delete(path: str) -> None:
    hdrs = _sup_headers()
    if not hdrs:
        return
    try:
        async with aiohttp.ClientSession() as s:
            await s.delete(
                f"{_SUP}{path}", headers=hdrs, timeout=aiohttp.ClientTimeout(total=10)
            )
    except Exception:
        pass


async def _wait_for_job(job_id: str, timeout: int = 120) -> bool:
    """Poll /jobs/{job_id} until done or timeout. Returns True on success."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        data = await _sup_get(f"/jobs/{job_id}")
        if data:
            job = data.get("data", {})
            if job.get("done"):
                errors = job.get("errors", [])
                if errors:
                    _LOGGER.warning("Job %s finished with errors: %s", job_id, errors)
                return not errors
        await asyncio.sleep(2)
    _LOGGER.warning("Job %s timed out after %ss", job_id, timeout)
    return False


async def _backup_create(addon_slug: str) -> tuple[str | None, bytes]:
    """Create a partial AdGuard backup and return (slug, backup_bytes)."""
    resp = await _sup_post(
        "/backups/new/partial",
        {"addons": [addon_slug], "homeassistant": False, "name": "ekaza-adguard-tmp"},
        timeout=120,
    )
    if not resp:
        return None, b""

    slug = resp.get("data", {}).get("slug")
    if not slug:
        return None, b""

    # Download backup bytes
    hdrs = _sup_headers()
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.get(
                f"{_SUP}/backups/{slug}/download",
                headers=hdrs,
                timeout=aiohttp.ClientTimeout(total=120),
            )
            backup_bytes = await r.read()
        return slug, backup_bytes
    except Exception as exc:
        _LOGGER.error("Failed to download backup %s: %s", slug, exc)
        return slug, b""


async def _backup_upload_and_restore(
    backup_bytes: bytes, addon_slug: str
) -> tuple[bool, str]:
    """Upload modified backup and restore AdGuard from it."""
    hdrs = _sup_headers()
    form = aiohttp.FormData()
    form.add_field(
        "file",
        backup_bytes,
        content_type="application/octet-stream",
        filename="backup.tar",
    )
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.post(
                f"{_SUP}/backups/new/upload",
                headers=hdrs,
                data=form,
                timeout=aiohttp.ClientTimeout(total=120),
            )
            up_resp = await r.json()
    except Exception as exc:
        return False, f"Upload failed: {exc}"

    new_slug = up_resp.get("data", {}).get("slug")
    if not new_slug:
        return False, f"Upload returned no slug: {up_resp}"

    rest_resp = await _sup_post(
        f"/backups/{new_slug}/restore/partial",
        {"addons": [addon_slug], "homeassistant": False},
        timeout=180,
    )
    if rest_resp is None:
        await _sup_delete(f"/backups/{new_slug}")
        return False, "Restore request failed"

    if isinstance(rest_resp.get("job_id"), str):
        await _wait_for_job(rest_resp["job_id"])
    elif isinstance(rest_resp.get("data"), str):
        await _wait_for_job(rest_resp["data"])

    await _sup_delete(f"/backups/{new_slug}")
    await asyncio.sleep(5)
    return True, "ok"


# ---------------------------------------------------------------------------
# DNS Rewrite helpers (backup-based — AdGuard API is localhost-only)
# ---------------------------------------------------------------------------

# Known Tuya MQTT broker domains, ordered by likelihood for BR/US region
_TUYA_MQTT_DOMAINS = [
    "m.tuyaus.com",
    "m.tuyaeu.com",
    "m.tuyacn.com",
    "m.tuyain.com",
    "a1.tuyaus.com",
    "a1.tuyaeu.com",
]


async def add_dns_rewrite(domain: str, answer: str) -> tuple[bool, str]:
    """Add a DNS rewrite via AdGuard backup/restore (~40s, restarts AdGuard briefly)."""
    addon_slug = await _get_adguard_slug()
    if not addon_slug:
        return False, "Add-on AdGuard não encontrado"

    slug, backup_bytes = await _backup_create(addon_slug)
    if not backup_bytes:
        if slug:
            await _sup_delete(f"/backups/{slug}")
        return False, "Falha ao criar backup do AdGuard"

    try:
        yaml_bytes = _extract_yaml_from_backup(backup_bytes, addon_slug)
        config = yaml.safe_load(yaml_bytes)
        # AdGuard v6: rewrites live under filtering.rewrites, not top-level
        rewrites: list[dict] = list(
            (config.get("filtering") or {}).get("rewrites") or []
        )
    except Exception as exc:
        if slug:
            await _sup_delete(f"/backups/{slug}")
        return False, f"Falha ao ler config do AdGuard: {exc}"

    if any(r.get("domain") == domain and r.get("answer") == answer for r in rewrites):
        if slug:
            await _sup_delete(f"/backups/{slug}")
        await _async_write_cached_rewrites(
            [{"domain": r["domain"], "answer": r["answer"]} for r in rewrites]
        )
        return True, f"DNS rewrite já existe: {domain} → {answer}"

    rewrites.append({"domain": domain, "answer": answer, "enabled": True})

    def _patch(cfg: dict) -> None:
        if "filtering" not in cfg:
            cfg["filtering"] = {}
        cfg["filtering"]["rewrites"] = rewrites

    try:
        modified = _modify_adguard_yaml(backup_bytes, _patch, addon_slug)
    except Exception as exc:
        if slug:
            await _sup_delete(f"/backups/{slug}")
        return False, f"Falha ao modificar backup: {exc}"

    if slug:
        await _sup_delete(f"/backups/{slug}")

    ok, msg = await _backup_upload_and_restore(modified, addon_slug)
    if ok:
        await _async_write_cached_rewrites(
            [{"domain": r["domain"], "answer": r["answer"]} for r in rewrites]
        )
        _LOGGER.warning("DNS rewrite adicionado: %s → %s", domain, answer)
        return True, f"DNS rewrite adicionado: {domain} → {answer}"
    return False, f"Falha ao restaurar backup AdGuard: {msg}"


async def remove_dns_rewrite(domain: str, answer: str) -> tuple[bool, str]:
    """Remove a DNS rewrite via AdGuard backup/restore (~40s, restarts AdGuard briefly)."""
    addon_slug = await _get_adguard_slug()
    if not addon_slug:
        return False, "Add-on AdGuard não encontrado"

    slug, backup_bytes = await _backup_create(addon_slug)
    if not backup_bytes:
        if slug:
            await _sup_delete(f"/backups/{slug}")
        return False, "Falha ao criar backup do AdGuard"

    try:
        yaml_bytes = _extract_yaml_from_backup(backup_bytes, addon_slug)
        config = yaml.safe_load(yaml_bytes)
        # AdGuard v6: rewrites live under filtering.rewrites, not top-level
        rewrites: list[dict] = list(
            (config.get("filtering") or {}).get("rewrites") or []
        )
    except Exception as exc:
        if slug:
            await _sup_delete(f"/backups/{slug}")
        return False, f"Falha ao ler config do AdGuard: {exc}"

    new_rewrites = [
        r
        for r in rewrites
        if not (r.get("domain") == domain and r.get("answer") == answer)
    ]

    if len(new_rewrites) == len(rewrites):
        if slug:
            await _sup_delete(f"/backups/{slug}")
        await _async_write_cached_rewrites(
            [{"domain": r["domain"], "answer": r["answer"]} for r in rewrites]
        )
        return True, f"DNS rewrite não encontrado (ok): {domain}"

    def _patch(cfg: dict) -> None:
        if "filtering" not in cfg:
            cfg["filtering"] = {}
        cfg["filtering"]["rewrites"] = new_rewrites

    try:
        modified = _modify_adguard_yaml(backup_bytes, _patch, addon_slug)
    except Exception as exc:
        if slug:
            await _sup_delete(f"/backups/{slug}")
        return False, f"Falha ao modificar backup: {exc}"

    if slug:
        await _sup_delete(f"/backups/{slug}")

    ok, msg = await _backup_upload_and_restore(modified, addon_slug)
    if ok:
        await _async_write_cached_rewrites(
            [{"domain": r["domain"], "answer": r["answer"]} for r in new_rewrites]
        )
        _LOGGER.warning("DNS rewrite removido: %s", domain)
        return True, f"DNS rewrite removido: {domain}"
    return False, f"Falha ao restaurar backup AdGuard: {msg}"


async def list_dns_rewrites() -> list[dict]:
    """Return cached AdGuard DNS rewrite rules (set by add/remove or sync_dns_rewrites)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _read_cached_rewrites)


async def sync_dns_rewrites() -> tuple[bool, list[dict]]:
    """Read current DNS rewrites from AdGuard backup and update cache.

    Creates a temporary backup, reads AdGuardHome.yaml, updates the local cache,
    then deletes the backup. Takes ~20s but does NOT restart AdGuard.
    """
    addon_slug = await _get_adguard_slug()
    if not addon_slug:
        return False, []

    slug, backup_bytes = await _backup_create(addon_slug)
    if not backup_bytes:
        if slug:
            await _sup_delete(f"/backups/{slug}")
        return False, []

    try:
        yaml_bytes = _extract_yaml_from_backup(backup_bytes, addon_slug)
        config = yaml.safe_load(yaml_bytes)
        # AdGuard v6: rewrites live under filtering.rewrites, not top-level
        all_rewrites: list[dict] = list(
            (config.get("filtering") or {}).get("rewrites") or []
        )
        rewrites = [
            {"domain": r["domain"], "answer": r["answer"]} for r in all_rewrites
        ]
    except Exception as exc:
        _LOGGER.warning("sync_dns_rewrites: failed to read YAML: %s", exc)
        if slug:
            await _sup_delete(f"/backups/{slug}")
        return False, []
    finally:
        if slug:
            await _sup_delete(f"/backups/{slug}")

    await _async_write_cached_rewrites(rewrites)
    return True, rewrites


async def discover_camera_mqtt_domain(camera_ip: str) -> str | None:
    """Discover the Tuya MQTT broker domain used by a camera via TCP probe."""
    import socket as _socket

    def _probe(domain: str) -> bool:
        try:
            with _socket.create_connection((domain, 8883), timeout=3):
                return True
        except Exception:
            return False

    loop = asyncio.get_running_loop()
    for domain in _TUYA_MQTT_DOMAINS:
        reachable = await loop.run_in_executor(None, _probe, domain)
        if reachable:
            _LOGGER.debug("Domínio MQTT Tuya descoberto via TCP probe: %s", domain)
            return domain

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def check_accessible(hass=None) -> tuple[bool, str]:
    """Return (accessible, message)."""
    if not _sup_headers():
        return False, "SUPERVISOR_TOKEN não disponível"
    addon_slug = await _get_adguard_slug()
    if not addon_slug:
        return False, "Add-on AdGuard não encontrado no Supervisor"
    data = await _sup_get(f"/addons/{addon_slug}/info")
    if data is None:
        return False, "Supervisor API não acessível — verifique o SUPERVISOR_TOKEN"
    state = data.get("data", {}).get("state", "?")
    version = data.get("data", {}).get("version", "?")
    if state != "started":
        return False, f"AdGuard não está rodando (state: {state})"
    return True, f"AdGuard {version} acessível via backup API"


def _strip_our_rules(rules: list[str]) -> list[str]:
    result, inside = [], False
    for line in rules:
        if line.strip() == _RULE_START:
            inside = True
            continue
        if line.strip() == _RULE_END:
            inside = False
            continue
        if not inside:
            result.append(line)
    return result


async def add_block_rules(hass=None) -> tuple[bool, str]:
    """Add SmartLife blocking rules to AdGuard. Restarts AdGuard briefly."""
    addon_slug = await _get_adguard_slug()
    if not addon_slug:
        return False, "Add-on AdGuard não encontrado"

    slug, backup_bytes = await _backup_create(addon_slug)
    if not backup_bytes:
        if slug:
            await _sup_delete(f"/backups/{slug}")
        return False, "Falha ao criar backup do AdGuard"

    try:
        yaml_bytes = _extract_yaml_from_backup(backup_bytes, addon_slug)
        config = yaml.safe_load(yaml_bytes)
        current_rules = list(config.get("user_rules") or [])
    except Exception as exc:
        if slug:
            await _sup_delete(f"/backups/{slug}")
        return False, f"Falha ao ler config do AdGuard: {exc}"

    cleaned = _strip_our_rules(current_rules)
    new_rules = cleaned + _BLOCK_RULES

    try:
        modified_backup = _modify_yaml_in_backup(backup_bytes, new_rules, addon_slug)
    except Exception as exc:
        if slug:
            await _sup_delete(f"/backups/{slug}")
        return False, f"Falha ao modificar backup: {exc}"

    if slug:
        await _sup_delete(f"/backups/{slug}")

    ok, msg = await _backup_upload_and_restore(modified_backup, addon_slug)
    if ok:
        _write_cached_status(True)
        return (
            True,
            f"{len(BLOCK_DOMAINS)} domínios SmartLife/Tuya bloqueados no AdGuard",
        )
    return False, f"Falha ao restaurar backup AdGuard: {msg}"


async def remove_block_rules(hass=None) -> tuple[bool, str]:
    """Remove SmartLife blocking rules from AdGuard. Restarts AdGuard briefly."""
    addon_slug = await _get_adguard_slug()
    if not addon_slug:
        return False, "Add-on AdGuard não encontrado"

    slug, backup_bytes = await _backup_create(addon_slug)
    if not backup_bytes:
        if slug:
            await _sup_delete(f"/backups/{slug}")
        return False, "Falha ao criar backup do AdGuard"

    try:
        yaml_bytes = _extract_yaml_from_backup(backup_bytes, addon_slug)
        config = yaml.safe_load(yaml_bytes)
        current_rules = list(config.get("user_rules") or [])
    except Exception as exc:
        if slug:
            await _sup_delete(f"/backups/{slug}")
        return False, f"Falha ao ler config do AdGuard: {exc}"

    cleaned = _strip_our_rules(current_rules)
    if len(cleaned) == len(current_rules):
        if slug:
            await _sup_delete(f"/backups/{slug}")
        _write_cached_status(False)
        return True, "Nenhuma regra SmartLife encontrada no AdGuard (ok)"

    try:
        modified_backup = _modify_yaml_in_backup(backup_bytes, cleaned, addon_slug)
    except Exception as exc:
        if slug:
            await _sup_delete(f"/backups/{slug}")
        return False, f"Falha ao modificar backup: {exc}"

    if slug:
        await _sup_delete(f"/backups/{slug}")

    ok, msg = await _backup_upload_and_restore(modified_backup, addon_slug)
    if ok:
        _write_cached_status(False)
        return True, "Regras de bloqueio SmartLife removidas do AdGuard"
    return False, f"Falha ao restaurar backup AdGuard: {msg}"
