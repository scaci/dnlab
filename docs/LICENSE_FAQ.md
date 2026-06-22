# License FAQ

dNLab is licensed under the
[GNU Affero General Public License v3.0 or later](../LICENSE)
(`AGPL-3.0-or-later`).

This document clarifies what that means in practice, especially for
organizations evaluating on-premises deployment.

---

## On-premises and internal use

If you install and run dNLab on your own infrastructure for internal use, you
are not required to publish your modifications or contribute them back solely
because of that internal use.

This applies even if:

- you modify dNLab to fit your environment
- your organization charges internally for access to the platform
- you run it at scale across multiple sites or nodes

The AGPL requirement to make corresponding modified source available is
triggered by distribution to third parties or by offering a modified version as
a network service to external users. It is not triggered merely by internal
on-premises use.

---

## When AGPL obligations apply

The following scenarios require you to make the corresponding modified source
available under `AGPL-3.0-or-later`, unless you have obtained a separate
commercial license:

| Scenario | Obligation |
|---|---|
| You offer a modified dNLab as a managed service to external users | Make corresponding modified source available |
| You distribute modified dNLab binaries, containers, or packages to third parties | Provide corresponding source |
| You embed modified dNLab in a product you sell or license to others | Provide corresponding source |

In short: if external users interact with a modified version of dNLab, whether
by downloading it or accessing it over a network, those modifications must be
made available under the same license unless a separate commercial license
applies. Exact obligations can depend on your users, deployment model and
distribution path.

Public dNLab container images are distributed through GitHub Container Registry.
The corresponding source for public image tags is made available through
matching release source archives. See [SOURCE.md](SOURCE.md) for the source
availability policy.

---

## Commercial licensing

If `AGPL-3.0-or-later` is incompatible with your business model, for example
because you want to distribute dNLab as part of a proprietary product or offer a
modified version as a service without making your modifications available,
please reach out at [dnlab@my-net.cloud](mailto:dnlab@my-net.cloud).

Commercial licensing can cover only code for which dNLab has the necessary
rights, including code owned by the project or third-party contributions covered
by a separate written agreement.

---

## Summary

Use dNLab freely on-premises for internal purposes without publishing internal
modifications. If you build a business on top of a modified version for external
users, make the corresponding modified source available or talk to us about a
commercial license.

---

*This document is provided for informational purposes and does not constitute
legal advice. For formal legal guidance, consult a qualified attorney.*
