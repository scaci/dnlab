# dNLab 0.1.2 Logging Report

Version `0.1.2` standardizes runtime infrastructure logging across the Docker
stack. All persistent service logs now live under one root:
`/var/log/dnlab`.

## Breaking Change

`/etc/dnlab/paths.yml` now uses:

```yaml
log_root: /var/log/dnlab
```

The previous public keys `log_dir_gui` and `log_dir_multinode` are removed.
The previous Compose environment variables `DNLAB_LOG_DIR_GUI` and
`DNLAB_LOG_DIR_MULTINODE` are removed. Use `DNLAB_LOG_ROOT` instead when the
host-side log directory must be customized.

## Runtime Log Layout

- proxy: `/var/log/dnlab/proxy/access.log`,
  `/var/log/dnlab/proxy/error.log`
- GUI: `/var/log/dnlab/gui/dnlab-gui.log`
- auth DB: `/var/log/dnlab/auth-db/postgresql.log`
- multinode API/CLI: `/var/log/dnlab/multinode/dnlab-multinode.log`
- image-sync: `/var/log/dnlab/image-sync/dnlab-image-sync.log`
- lab-cleanup: `/var/log/dnlab/lab-cleanup/dnlab-lab-cleanup.log`
- image-build API: `/var/log/dnlab/image-build/dnlab-image-build.log`

Image-build job logs remain under `/var/lib/dnlab-image-build/logs` because
they are job history data used by the GUI, not service infrastructure logs.

## Upgrade Notes

Before recreating services:

1. Create the log root:

   ```bash
   sudo mkdir -p /var/log/dnlab
   ```

2. Replace old logging keys in `/etc/dnlab/paths.yml` with:

   ```yaml
   log_root: /var/log/dnlab
   ```

3. Replace any `.env` customization of `DNLAB_LOG_DIR_GUI` or
   `DNLAB_LOG_DIR_MULTINODE` with:

   ```text
   DNLAB_LOG_ROOT=/var/log/dnlab
   ```

4. Recreate runtime services:

   ```bash
   docker compose -f compose.yml up -d --force-recreate \
     proxy gui auth-db multinode image-sync lab-cleanup image-build
   ```

Run `./smoke.sh` after the upgrade to verify service health and log files.
