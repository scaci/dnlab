"""Running lab and container state models."""

from pydantic import BaseModel


class ContainerInfo(BaseModel):
    name: str
    container_id: str = ""
    image: str = ""
    kind: str = ""
    state: str = ""
    ipv4_address: str = ""
    ipv6_address: str = ""
    lab_name: str = ""
    node_name: str = ""
    apply_mode: str = ""


class Lab(BaseModel):
    name: str
    topology_file: str = ""
    status: str = "stopped"   # stopped | running | partial
    containers: list[ContainerInfo] = []

    @property
    def is_running(self) -> bool:
        return self.status == "running"
