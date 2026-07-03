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
    rtsp_password: str
    rtsp_username: str = "admin"
    online: bool = True


class DiscoverRequest(BaseModel):
    credentials: TuyaCredentials


class ProvisionRequest(BaseModel):
    credentials: TuyaCredentials
    cameras: list[CameraInfo]


class StepResult(BaseModel):
    step: str
    status: str   # "ok" | "error" | "skip"
    detail: str = ""


class CameraResult(BaseModel):
    camera: str
    steps: list[StepResult] = Field(default_factory=list)
