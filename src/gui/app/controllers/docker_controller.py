"""Docker image discovery controller."""

from app.models.docker_image import DockerImage
from app.services.docker_service import DockerService


class DockerController:
    def __init__(self) -> None:
        self._docker: DockerService | None = None

    @property
    def docker(self) -> DockerService:
        if self._docker is None:
            self._docker = DockerService()
        return self._docker

    def list_images(self) -> list[DockerImage]:
        return self.docker.list_images()

    def list_network_images(self) -> list[DockerImage]:
        """Return only images usable as ContainerLab nodes."""
        return [
            img for img in self.docker.list_images()
            if img.kind != "linux" or img.is_vrnetlab
        ]
