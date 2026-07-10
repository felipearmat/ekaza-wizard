"""Deploy the bundled custom card JS to /config/www and register as Lovelace resource."""
import shutil
from pathlib import Path

import ha_client

_CARD_NAME = "ekaza-control-card"
_CARD_SRC  = Path("/app/static/cards") / f"{_CARD_NAME}.js"
_CARD_DIR  = Path("/config/www/ekaza-wizard")
_CARD_URL  = f"/local/ekaza-wizard/{_CARD_NAME}.js"


def _copy_card() -> None:
    _CARD_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_CARD_SRC, _CARD_DIR / f"{_CARD_NAME}.js")


async def deploy() -> tuple[bool, str]:
    try:
        _copy_card()
    except Exception as e:
        return False, f"Card copy failed: {e}"
    try:
        ok = await ha_client.lovelace_register_resource(_CARD_URL)
        if ok:
            return True, f"Card '{_CARD_NAME}' registered at {_CARD_URL}"
        return False, "Resource registration returned failure"
    except Exception as e:
        return False, f"Resource registration failed: {e}"
