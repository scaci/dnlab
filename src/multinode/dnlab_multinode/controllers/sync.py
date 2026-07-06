"""Image sync controller — distribute images from master to workers."""

from __future__ import annotations

import logging
from pathlib import Path

from dnlab_multinode.services.config import parse_topology
from dnlab_multinode.services.progress import ProgressCallback, make_timer
from dnlab_multinode.services.resources import check_images_on_hosts, sync_image_to_host
from dnlab_multinode.services.ssh import create_clients

log = logging.getLogger(__name__)


class SyncController:
    """Synchronize Docker images from master to workers."""

    def __init__(
        self,
        topology_file: str,
        *,
        hosts_file: str | None = None,
        progress: ProgressCallback | None = None,
    ):
        self.topology_file = topology_file
        self.hosts_file = hosts_file
        self._progress = make_timer(progress)

    def run(self) -> dict[str, list[str]]:
        """Check and sync missing images.

        Returns: {image: [hosts_synced]} for images that were synced
        """
        self._progress.emit("sync", "start", detail="Checking image alignment")

        topo = parse_topology(self.topology_file, hosts_file=self.hosts_file)
        unique_images = {n.image for n in topo.nodes.values()}

        clients = create_clients(topo.all_hosts)
        synced: dict[str, list[str]] = {}

        try:
            for client in clients.values():
                client.connect()

            self._progress.emit(
                "sync-check", "start",
                detail=f"Inspecting {len(unique_images)} image(s) across {len(clients)} host(s)",
            )
            missing = check_images_on_hosts(unique_images, clients)
            self._progress.emit(
                "sync-check", "ok",
                detail=f"{len(missing)} image(s) need sync",
                data={"missing": {img: list(hs) for img, hs in missing.items()}},
            )

            if not missing:
                log.info("All images aligned across all nodes")
                self._progress.emit("sync", "ok", detail="All images already aligned")
                return synced

            for image, hosts in missing.items():
                synced[image] = []
                for host_name in hosts:
                    if host_name == "master":
                        msg = f"Image {image} missing on master — cannot sync"
                        log.error(msg)
                        self._progress.emit(
                            "sync-image", "error",
                            host=host_name, detail=msg,
                            data={"image": image},
                        )
                        continue

                    log.info("Syncing %s → %s", image, host_name)
                    self._progress.emit(
                        "sync-image", "start",
                        host=host_name, detail=f"Transferring {image}",
                        data={"image": image},
                    )
                    ok = sync_image_to_host(image, clients[host_name])
                    if ok:
                        synced[image].append(host_name)
                        self._progress.emit(
                            "sync-image", "ok",
                            host=host_name, detail=f"{image} synced",
                            data={"image": image},
                        )
                    else:
                        log.error("Failed to sync %s to %s", image, host_name)
                        self._progress.emit(
                            "sync-image", "error",
                            host=host_name, detail=f"Failed to sync {image}",
                            data={"image": image},
                        )

            total = sum(len(h) for h in synced.values())
            self._progress.emit(
                "sync", "ok",
                detail=f"Synced {total} image-host pair(s)",
                data={"synced": synced},
            )
            return synced

        except Exception as exc:
            self._progress.emit("sync", "error", detail=str(exc))
            raise
        finally:
            for client in clients.values():
                client.close()
