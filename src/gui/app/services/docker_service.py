"""Docker service: image discovery and kind resolution."""

import docker
from app.models.docker_image import DockerImage
from app.services import device_catalog


class DockerService:
    def __init__(self) -> None:
        self._client = docker.from_env()

    def list_images(self) -> list[DockerImage]:
        """Return all local Docker images enriched with ContainerLab kind info."""
        images: list[DockerImage] = []
        for img in self._client.images.list():
            for repo_tag in img.tags:
                if ":" in repo_tag:
                    repo, tag = repo_tag.rsplit(":", 1)
                else:
                    repo, tag = repo_tag, "latest"

                kind, vendor = device_catalog.resolve_kind_and_vendor(repo)

                images.append(
                    DockerImage(
                        repository=repo,
                        tag=tag,
                        image_id=img.short_id,
                        kind=kind,
                        vendor=vendor,
                    )
                )
        return images

    def container_exists(self, name: str) -> bool:
        try:
            self._client.containers.get(name)
            return True
        except docker.errors.NotFound:
            return False

    def get_container_ip(self, container_name: str) -> str | None:
        try:
            container = self._client.containers.get(container_name)
            networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            for net in networks.values():
                ip = net.get("IPAddress")
                if ip:
                    return ip
        except docker.errors.NotFound:
            pass
        return None
