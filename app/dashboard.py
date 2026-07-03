"""Create / update the Lovelace "Câmeras" dashboard with one card per camera."""
from ha_client import lovelace_get_config, lovelace_save_config
from models import CameraInfo

_VIEW_PATH = "cameras"
_VIEW_TITLE = "Câmeras"
_VIEW_ICON = "mdi:cctv"


def _card(cam: CameraInfo, frigate_host: str) -> dict:
    return {
        "type": "custom:security-camera-card",
        "entity": f"camera.{cam.slug}",
        "name": cam.name,
        "frigate_host": frigate_host,
    }


async def update_dashboard(cameras: list[CameraInfo], frigate_host: str) -> tuple[bool, str]:
    """
    Add a "Câmeras" view (or update the existing one) in the default Lovelace dashboard.
    Returns (success, detail).
    """
    try:
        config = await lovelace_get_config()
        if config is None:
            return False, "Could not read Lovelace config"

        cards = [_card(cam, frigate_host) for cam in cameras]
        views: list[dict] = config.get("views", [])

        # find existing cameras view
        existing = next((v for v in views if v.get("path") == _VIEW_PATH), None)
        if existing is not None:
            # merge: add cards not already present (by entity)
            existing_entities = {c.get("entity") for c in existing.get("cards", [])}
            for card in cards:
                if card["entity"] not in existing_entities:
                    existing.setdefault("cards", []).append(card)
        else:
            views.append({
                "title": _VIEW_TITLE,
                "path": _VIEW_PATH,
                "icon": _VIEW_ICON,
                "cards": cards,
            })

        config["views"] = views
        ok = await lovelace_save_config(config)
        return ok, "Dashboard updated" if ok else "Failed to save Lovelace config"

    except Exception as e:
        return False, str(e)
