"""AdGuard Home API client for eKaza/TUTK cloud blocking.

Primary mechanism: Supervisor backup API (create → download → modify YAML → upload → restore).
Used because AdGuard binds its HTTP API only to localhost (unreachable from add-on container).
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import tarfile
from pathlib import Path

import aiohttp
import yaml

_LOGGER = logging.getLogger(__name__)

_RULE_START = "! ekaza-cloud-start"
_RULE_END   = "! ekaza-cloud-end"

BLOCK_DOMAINS = [
    "tutk.com",
    "iotcplatform.com",
    "avservices.io",
    "p2ptunnell.com",
    "gwipc.com",
    "yosee.com",
]

_BLOCK_RULES = (
    [_RULE_START, "! Bloqueio eKaza/TUTK — gerenciado pelo eKaza Wizard"]
    + [f"||{d}^" for d in BLOCK_DOMAINS]
    + [_RULE_END]
)

_SUP = "http://supervisor"
_STATUS_FILE = Path("/data/.ekaza_adguard_status")

_adguard_slug: str | None = None


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


async def get_status() -> dict:
    """Return {"active": bool, "domains": int, "source": "cache"|"unknown"}."""
    cached = _read_cached_status()
    if cached is not None:
        return {"active": cached == "active", "domains": len(BLOCK_DOMAINS), "source": "cache"}
    return {"active": False, "domains": len(BLOCK_DOMAINS), "source": "unknown"}


def _sup_headers() -> dict:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


# ---------------------------------------------------------------------------
# AdGuard slug discovery
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------

def _extract_yaml_from_backup(backup_bytes: bytes, addon_slug: str) -> bytes:
    outer = tarfile.open(fileobj=io.BytesIO(backup_bytes))
    inner_fobj = outer.extractfile(f"{addon_slug}.tar.gz")
    if inner_fobj is None:
        raise ValueError(f"addon tar '{addon_slug}.tar.gz' not found in backup")
    inner = tarfile.open(fileobj=io.BytesIO(inner_fobj.read()), mode="r:gz")
    yml_fobj = inner.extractfile("data/adguard/AdGuardHome.yaml")
    if yml_fobj is None:
        raise ValueError("AdGuardHome.yaml not found in addon tar")
    return yml_fobj.read()


def _modify_yaml_in_backup(backup_bytes: bytes, new_rules: list[str], addon_slug: str) -> bytes:
    outer = tarfile.open(fileobj=io.BytesIO(backup_bytes))
    inner_fobj = outer.extractfile(f"{addon_slug}.tar.gz")
    inner = tarfile.open(fileobj=io.BytesIO(inner_fobj.read()), mode="r:gz")
    new_inner_buf = io.BytesIO()
    with tarfile.open(fileobj=new_inner_buf, mode="w:gz") as new_inner:
        for member in inner.getmembers():
            fobj = inner.extractfile(member)
            if member.name == "data/adguard/AdGuardHome.yaml" and fobj:
                config = yaml.safe_load(fobj.read())
                config["user_rules"] = new_rules
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


# ---------------------------------------------------------------------------
# Supervisor API helpers
# ---------------------------------------------------------------------------

async def _sup_get(path: str) -> dict | None:
    hdrs = _sup_headers()
    if not hdrs:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.get(f"{_SUP}{path}", headers=hdrs, timeout=aiohttp.ClientTimeout(total=10))
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
                f"{_SUP}{path}", headers=hdrs, json=json_body,
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
            await s.delete(f"{_SUP}{path}", headers=hdrs, timeout=aiohttp.ClientTimeout(total=10))
    except Exception:
        pass


async def _wait_for_job(job_id: str, timeout: int = 120) -> bool:
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


async def _backup_upload_and_restore(backup_bytes: bytes, addon_slug: str) -> tuple[bool, str]:
    hdrs = _sup_headers()
    form = aiohttp.FormData()
    form.add_field("file", backup_bytes, content_type="application/octet-stream",
                   filename="backup.tar")
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.post(
                f"{_SUP}/backups/new/upload", headers=hdrs,
                data=form, timeout=aiohttp.ClientTimeout(total=120),
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
# Public API
# ---------------------------------------------------------------------------

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


async def check_accessible() -> tuple[bool, str]:
    """Return (accessible, message)."""
    if not _sup_headers():
        return False, "SUPERVISOR_TOKEN não disponível"
    addon_slug = await _get_adguard_slug()
    if not addon_slug:
        return False, "Add-on AdGuard não encontrado no Supervisor"
    data = await _sup_get(f"/addons/{addon_slug}/info")
    if data is None:
        return False, "Supervisor API não acessível"
    state = data.get("data", {}).get("state", "?")
    version = data.get("data", {}).get("version", "?")
    if state != "started":
        return False, f"AdGuard não está rodando (state: {state})"
    return True, f"AdGuard {version} acessível"


async def add_block_rules() -> tuple[bool, str]:
    """Add eKaza/TUTK blocking rules to AdGuard. Briefly restarts AdGuard."""
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
        return True, f"{len(BLOCK_DOMAINS)} domínios eKaza/TUTK bloqueados no AdGuard"
    return False, f"Falha ao restaurar backup AdGuard: {msg}"


async def remove_block_rules() -> tuple[bool, str]:
    """Remove eKaza/TUTK blocking rules from AdGuard. Briefly restarts AdGuard."""
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
        return True, "Nenhuma regra eKaza encontrada no AdGuard (ok)"

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
        return True, "Regras de bloqueio eKaza/TUTK removidas do AdGuard"
    return False, f"Falha ao restaurar backup AdGuard: {msg}"
