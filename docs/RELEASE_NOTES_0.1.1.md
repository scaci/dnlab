# dNLab 0.1.1 Bugfix Report

## Fixed: orphan per-lab service containers were not always cleaned up

### Summary

Version `0.1.1` fixes an orphan cleanup issue where per-lab service containers
could survive after an incomplete destroy, rollback, or cleanup cycle.

The issue was observed with `dnlab-<lab>-runtime-relay`, but the same behavior
could also affect other per-lab service containers such as DNS, jumphost, and
legacy logging services.

### Impact

An inactive lab could leave behind service containers that continued running on
a worker node. Because the cleanup reconciler treated any running runtime
container as evidence that the lab was still active, those orphaned services
were protected from removal.

In practice, this meant that a lab with no running VD/container nodes could
still retain containers such as:

- `dnlab-<lab>-runtime-relay`
- `dnlab-<lab>-dns`
- `dnlab-<lab>-jumphost`
- legacy `syslog` / `log-shipper` containers
- `mgmt-anchor`

### Root Cause

The previous cleanup protection rule was too broad. It used a generic
`container-running` condition, so any running per-lab runtime container could
protect the lab from cleanup.

This was incorrect for service containers because some of them, especially the
runtime relay, use restart policies such as `unless-stopped` and may remain
running even when the lab is no longer considered up.

### Resolution

The cleanup reconciler now protects a lab only when the lab runtime is actually
active, using the same semantic model exposed by multinode status:

- a lab is protected when at least one expected/live VD container is `running`;
- per-lab service containers no longer protect the lab by themselves;
- if no VD is running, cleanup actions are generated for all per-lab containers,
  including running service containers.

The runtime detection covers:

- VD containers from `state.node_runtime`;
- legacy fallback entries from `state.scheduling`;
- live containerlab VD containers for labs without state, excluding service
  containers and `mgmt-anchor`.

### Preserved Guardrails

The fix does not relax the existing cleanup safeguards:

- unreachable hosts expected by the state file still block cleanup;
- artifacts inside the grace window still block cleanup;
- shared networks such as `dnlab-jumphost` and `dnlab-realnet` are never
  removed;
- live interfaces without state remain warnings only.

### Validation

The fix was validated with focused cleanup tests and the full test suite:

- `./venv/bin/pytest tests/test_lab_cleanup.py`
- `./venv/bin/pytest`
- `git diff --check`

## Fixed: duplicated Docker Compose service prefixes

### Summary

Version `0.1.1` also records the Docker Compose naming cleanup that removes the
redundant `dnlab-` prefix from Compose service keys.

### Impact

When the Compose project name was already `dnlab`, service keys that also used
the `dnlab-` prefix produced container names with a duplicated prefix, such as
`dnlab-dnlab-*`.

### Resolution

Compose service keys were normalized so the generated container names are now
clean and predictable, for example:

- `dnlab-proxy-1`
- `dnlab-gui-1`
- `dnlab-multinode-1`

Related internal DNS names and Compose resources were updated consistently,
including `multinode`, `image-build`, `image-sync`, `auth-db`, the `internal`
network, and the `auth-pgdata` / `image-sync-state` volumes.

Distribution documentation now refers to the Compose services as `proxy`, `gui`,
`multinode`, `image-sync`, `lab-cleanup`, `image-build`, and `auth-db`. The
`dnlab-*` names remain documented only where they identify product images,
commands, paths, certificates, SSH keys or release artifacts.
