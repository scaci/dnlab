"""Network link model."""

from pydantic import BaseModel


class Link(BaseModel):
    source: str
    source_iface: str = ""
    target: str
    target_iface: str = ""

    def endpoint_a(self) -> str:
        iface = self.source_iface or "eth1"
        return f"{self.source}:{iface}"

    def endpoint_b(self) -> str:
        iface = self.target_iface or "eth1"
        return f"{self.target}:{iface}"

    def to_clab_dict(self) -> dict[str, list[str]]:
        return {"endpoints": [self.endpoint_a(), self.endpoint_b()]}
