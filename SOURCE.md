# Source Availability

dNLab container images are distributed through GitHub Container Registry
(`GHCR`). The operational repositories used to build those images may remain
private and are not the public source distribution channel.

For each public dNLab image tag, the corresponding source code will be
published as a source archive attached to the matching dNLab release. The
archive for a release tag must match the container images published with that
same tag.

For example:

- Image: `ghcr.io/scaci/dnlab-gui:0.1.0`
- Source: release `v0.1.0`, artifact `dnlab-gui-0.1.0-source.tar.gz`

The corresponding source archive must include the application source,
Dockerfiles, build scripts, release metadata and configuration needed to build,
install, run and modify the distributed image.

The exact source archive generation and publication workflow is maintained as a
release engineering process. This document records the public source
availability policy; it does not require private operational repositories to be
made public.
