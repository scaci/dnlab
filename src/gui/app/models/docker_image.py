"""Docker image model with ContainerLab kind resolution."""

from pydantic import BaseModel


class DockerImage(BaseModel):
    repository: str
    tag: str
    image_id: str
    kind: str = ""
    vendor: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.repository}:{self.tag}"

    @property
    def is_vrnetlab(self) -> bool:
        return self.repository.startswith("vrnetlab/")
