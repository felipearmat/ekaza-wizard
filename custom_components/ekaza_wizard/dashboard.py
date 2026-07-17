"""Update Lovelace dashboard with ekaza-camera-card entries."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .models import CameraInfo

_LOGGER = logging.getLogger(__name__)
_DASHBOARD_PATH = "cameras"
_CARD_TYPE = "custom:ekaza-camera-card"


def _card(cam: CameraInfo) -> dict:
    return {"type": _CARD_TYPE, "entity": f"camera.{cam.slug}", "name": cam.name}


def _get_dashboards(lovelace):
    if hasattr(lovelace, "dashboards"):
        return lovelace.dashboards
    if isinstance(lovelace, dict):
        return lovelace.get("dashboards", {})
    return None


async def list_dashboards(hass: HomeAssistant) -> list[dict]:
    """Return [{key, title, url_path}] for all loadable Lovelace storage dashboards.

    Title and url_path come from the dashboard registry object (not the config content),
    since the config content stores views, not metadata.
    """
    from homeassistant.components.lovelace.const import ConfigNotFound

    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        return []
    dashboards = _get_dashboards(lovelace)
    if not dashboards or not hasattr(dashboards, "items"):
        return []

    result = []
    for key, dash in dashboards.items():
        if not hasattr(dash, "async_load"):
            continue
        try:
            config = await dash.async_load(force=False)
            if config is None:
                continue
        except ConfigNotFound:
            continue
        except Exception:
            continue

        # Metadata (title, url_path) lives in the dashboard registry object, not config content.
        # HA exposes it via dash.config (a dict) or dash.url_path / dash.title attributes.
        dash_config = getattr(dash, "config", {}) or {}
        title = (
            dash_config.get("title")
            or getattr(dash, "title", None)
            or config.get("title")
            or key
        )
        url_path = (
            dash_config.get("url_path")
            or getattr(dash, "url_path", None)
            or config.get("url_path")
            or key
        )
        result.append({"key": key, "title": title, "url_path": url_path})
    return result


async def _find_target_and_config(
    lovelace, url_path: str | None = None
) -> tuple | None:
    """Return (dashboard_obj, config_dict) matching url_path, or first loadable if None."""
    from homeassistant.components.lovelace.const import ConfigNotFound

    dashboards = _get_dashboards(lovelace)
    if not dashboards or not hasattr(dashboards, "items"):
        _LOGGER.warning("lovelace.dashboards not usable; type=%s", type(lovelace))
        return None

    for key, dash in dashboards.items():
        if not hasattr(dash, "async_load"):
            continue
        try:
            config = await dash.async_load(force=False)
        except ConfigNotFound:
            _LOGGER.debug("Dashboard key=%s has no config, skipping", key)
            continue
        except Exception as exc:
            _LOGGER.warning("Dashboard key=%s async_load error: %s", key, exc)
            continue
        if config is None:
            continue

        if url_path is not None:
            if config.get("url_path") == url_path or key == url_path:
                return dash, config
            continue

        # No filter: return first loadable
        return dash, config

    if url_path is not None:
        _LOGGER.warning("Dashboard url_path=%r not found", url_path)
    else:
        _LOGGER.warning(
            "No loadable dashboard found; keys=%s",
            list(dashboards.keys()) if hasattr(dashboards, "keys") else "?",
        )
    return None


def _filter_cards(cards: list, entity: str) -> tuple[list, int]:
    """Recursively remove cards referencing entity from a card list.

    Returns (filtered_cards, removed_count). Recurses into nested card lists
    (vertical-stack, horizontal-stack, grid, etc.).
    """
    result = []
    removed = 0
    for card in cards:
        if card.get("entity") == entity:
            removed += 1
            continue
        if "cards" in card:
            nested, n = _filter_cards(card["cards"], entity)
            removed += n
            card = {**card, "cards": nested}
        result.append(card)
    return result, removed


async def remove_card(hass: HomeAssistant, slug: str) -> tuple[bool, str]:
    """Remove all cards referencing camera.{slug} from every Lovelace dashboard."""
    from homeassistant.components.lovelace.const import ConfigNotFound

    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        return True, "Lovelace não carregado (nada a remover)"

    dashboards = _get_dashboards(lovelace)
    if not dashboards or not hasattr(dashboards, "items"):
        return True, "Sem dashboards (nada a remover)"

    entity = f"camera.{slug}"
    total_removed = 0
    dashes_modified = 0

    for key, dash in dashboards.items():
        if not hasattr(dash, "async_load") or not hasattr(dash, "async_save"):
            continue
        try:
            config = await dash.async_load(force=False)
        except ConfigNotFound:
            continue
        except Exception as exc:
            _LOGGER.debug("Dashboard %s load error: %s", key, exc)
            continue
        if config is None:
            continue

        modified = False
        for view in config.get("views", []):
            filtered, n = _filter_cards(view.get("cards", []), entity)
            if n:
                view["cards"] = filtered
                total_removed += n
                modified = True

        if modified:
            try:
                await dash.async_save(config)
                dashes_modified += 1
            except Exception as exc:
                _LOGGER.warning("Dashboard %s save error: %s", key, exc)

    if total_removed:
        return (
            True,
            f"{total_removed} card(s) removido(s) de {dashes_modified} dashboard(s)",
        )
    return True, "Nenhum card encontrado para remover"


async def update_dashboard(
    hass: HomeAssistant, cameras: list[CameraInfo], target_path: str | None = None
) -> tuple[bool, str]:
    try:
        lovelace = hass.data.get("lovelace")
        if lovelace is None:
            return False, "Lovelace não carregado"

        result = await _find_target_and_config(lovelace, url_path=target_path)
        if result is None:
            return (
                False,
                f"Dashboard '{target_path}' não encontrado"
                if target_path
                else "Nenhum dashboard Lovelace gravável encontrado",
            )

        target, config = result
        views: list = config.get("views", [])

        # Always add to the first existing tab — never create a second unnamed view
        if not views:
            views = [{"cards": []}]
            config["views"] = views
        target_view = views[0]

        existing = {
            c.get("entity")
            for c in target_view.get("cards", [])
            if c.get("type") == _CARD_TYPE
        }
        new_cards = [
            _card(cam) for cam in cameras if f"camera.{cam.slug}" not in existing
        ]
        target_view.setdefault("cards", []).extend(new_cards)

        config["views"] = views
        await target.async_save(config)
        return True, f"{len(new_cards)} card(s) adicionado(s)"

    except Exception as exc:
        _LOGGER.warning("Dashboard update failed: %s", exc, exc_info=True)
        return False, f"Erro: {exc or type(exc).__name__}"
