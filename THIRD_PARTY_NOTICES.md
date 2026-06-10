# Third-Party Notices

dNLab uses and integrates with third-party operating systems, container images,
tools and network device images. This file records the initial project-level
notices. Image-specific software bills of materials and package notices should
be published as release artifacts or added here before a public image release.

## Runtime and Platform Components

- Debian is the recommended host operating system. Debian is not distributed as
  part of this repository; hosts must comply with Debian and package licenses.
- Docker Engine and the Docker Compose plugin are required host tools. They are
  not distributed as part of this repository.
- Containerlab is a required host tool for network lab orchestration. dNLab may
  mount the host `containerlab` binary into services at runtime; if an image
  later redistributes Containerlab, its license notice must be included with
  that image.
- PostgreSQL is used through the stock `postgres:16-alpine` image in the Docker
  Compose stack. If dNLab mirrors or redistributes that image, the relevant
  PostgreSQL, Alpine Linux and package notices must be preserved.
- Apache HTTP Server is used by the dNLab proxy image. The proxy image must
  preserve Apache HTTP Server license and notice requirements.
- Alpine Linux and other base-image packages may be present in dNLab container
  images. Their package-level licenses must be tracked per image.

## Network Operating System Images

dNLab does not distribute proprietary or vendor network operating system images.
Users must provide any NOS/vendor images themselves and are responsible for
complying with the relevant vendor license terms.

## Image SBOM / Package Notices

For each public GHCR image tag, publish image-specific SBOM/package notices as
release artifacts or add an image-specific section here before release
promotion. Do not use placeholder SBOM entries in a public release.

Template:

```text
### ghcr.io/scaci/dnlab-<component>:<version>

- SBOM: <path or release artifact>
- Base image: <image and version>
- Included third-party packages: <summary or generated notice file>
- License exceptions or special obligations: <none or details>
```
