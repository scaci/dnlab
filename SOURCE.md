# Source Availability

dNLab container images are distributed through GitHub Container Registry
(`GHCR`) under the `ghcr.io/scaci/dnlab-*` image names. Public release packages
are linked to the public `scaci/dnlab` repository, even when the operational
repositories used to build those images remain private.

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

## Release assets

Source archives are published as assets on the matching `scaci/dnlab` GitHub
Release. They are not committed as binary files in this repository.

For a release `vX.Y.Z`, the expected source asset names are:

- `dnlab-gui-X.Y.Z-source.tar.gz`
- `dnlab-proxy-X.Y.Z-source.tar.gz`
- `dnlab-multinode-X.Y.Z-source.tar.gz`
- `dnlab-lab-cleanup-X.Y.Z-source.tar.gz`
- `dnlab-jumphost-X.Y.Z-source.tar.gz`
- `dnlab-dns-X.Y.Z-source.tar.gz`
- `dnlab-runtime-relay-X.Y.Z-source.tar.gz`
- `dnlab-realnet-router-X.Y.Z-source.tar.gz`
- `dnlab-realnet-rr-X.Y.Z-source.tar.gz`
- `dnlab-mgmt-anchor-X.Y.Z-source.tar.gz`
- `dnlab-image-build-X.Y.Z-source.tar.gz`
- `SHA256SUMS`

Each source archive includes a `SOURCE-MANIFEST.json` file that identifies the
image, version, source repository, release tag, commit SHA, Dockerfile and build
context used for the corresponding image.

After downloading the assets for a release, verify them with:

```bash
sha256sum -c SHA256SUMS
```

The source archive generation and publication workflow is maintained as a
release engineering process in the private operational documentation. This
document records the public source availability policy; it does not require
private operational repositories to be made public.
