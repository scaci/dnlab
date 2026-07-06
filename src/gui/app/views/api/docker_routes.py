"""Docker image discovery API routes (all authenticated)."""

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import get_current_user
from app.auth.models import User
from app.config import settings
from app.controllers.docker_controller import DockerController
from app.models.docker_image import DockerImage
from app.services import device_catalog


router = APIRouter(prefix="/api/docker", tags=["docker"])
_ctrl = DockerController()


@router.get("/images")
async def list_all_images(user: Annotated[User, Depends(get_current_user)]):
    images = await _list_images()
    return [img.model_dump() for img in images]


@router.get("/images/network")
async def list_network_images(user: Annotated[User, Depends(get_current_user)]):
    """Return only images recognised as ContainerLab node types."""
    images = await _list_images()
    return [
        img.model_dump()
        for img in images
        if img.kind != "linux" or img.is_vrnetlab
    ]


@router.get("/interfaces")
def get_interface_map(user: Annotated[User, Depends(get_current_user)]):
    """Return the interface naming map for kind."""
    return device_catalog.interface_map(settings.KIND_INTERFACES)


async def _list_images() -> list[DockerImage]:
    if settings.DNLAB_MULTINODE_API_URL:
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                response = await client.get(f"{settings.DNLAB_MULTINODE_API_URL}/docker/images")
        except httpx.HTTPError as exc:
            raise HTTPException(503, f"multinode Docker image API request failed: {exc}") from exc
        if response.status_code >= 400:
            raise HTTPException(response.status_code, _api_error_detail(response))
        data = response.json()
        return [_remote_image(item) for item in data.get("images", [])]
    return _ctrl.list_images()


def _remote_image(item: dict) -> DockerImage:
    image = DockerImage.model_validate(item)
    kind, vendor = device_catalog.resolve_kind_and_vendor(image.repository)
    return image.model_copy(update={"kind": kind, "vendor": vendor})


def _api_error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text
    if isinstance(data, dict):
        detail = data.get("detail")
        return detail if isinstance(detail, str) else str(detail or data)
    return str(data)
