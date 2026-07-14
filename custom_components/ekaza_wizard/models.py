from pydantic import BaseModel, Field
from typing import Optional


class TuyaCredentials(BaseModel):
    access_id: str
    access_secret: str
    region: str = "us"
    default_rtsp_password: str
    rtsp_username: str = "admin"


class CameraInfo(BaseModel):
    name: str                   # name in SmartLife
    slug: str                   # HA entity prefix (e.g. "garagem")
    device_id: str
    local_key: str
    ip: str
    mac: str = ""
    product_id: str = ""        # Tuya product_id (from cloud) — used for schema lookup
    rtsp_password: str
    rtsp_username: str = "admin"
    rtsp_port: int = 554        # probed during provisioning (some models use 8554)
    online: bool = True
    # camera → frigate mode: MITM proxy captures Tuya cloud MQTT events
    proxy_enabled: bool = False
    tuya_mqtt_domain: Optional[str] = None  # e.g. "m.tuyaus.com"; auto-discovered


class DiscoverRequest(BaseModel):
    credentials: TuyaCredentials


class ProvisionRequest(BaseModel):
    credentials: TuyaCredentials
    cameras: list[CameraInfo]
    dashboard_path: str | None = None


class StepResult(BaseModel):
    step: str
    status: str   # "ok" | "error" | "skip"
    detail: str = ""


class CameraResult(BaseModel):
    camera: str
    steps: list[StepResult] = Field(default_factory=list)
