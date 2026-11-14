# SCI delivery step

**Scope.** `sci-delivery.py` is this repository's boot-test vehicle, not a supported consumer agent. The product is the signed release data (see "Published boundary"); a consumer that reuses the step owns selection, staging persistence, key and anchor provisioning, and restart timing. Design: `../docs/sysext-virt.md`; manifest: `extensions.manifest`.

The script runs under `uv run --script` on a workstation and under plain `python3` in a minimal container or guest: its PEP 723 block declares no dependencies and it imports only the standard library.

## Modes

`--stage <raw-dir> <out-dir>` reads `extensions.manifest`, requires `<raw-dir>/<name>.raw` for every entry, requires every activation shim under `mkosi/mkosi.images/*/mkosi.extra/usr/share/sci/activation/` to match an entry, and copies the raws, shims, manifest, and this script into `<out-dir>` with a `SHA256SUMS` over the manifest raws. It refuses `/`, `.`, and the repository root as `<out-dir>`.

The default mode reconciles `STAGING_DIR` into `HOST_ROOT`. With `FETCH_URL` set, it first downloads `SHA256SUMS` and `SHA256SUMS.sig`, verifies the signature and optional `FETCH_SUMS_DIGEST`, downloads missing or mismatched files through `.part` renames, and prunes what the inventory no longer lists.

## Pass contract

Inputs are checked in order; any failure exits non-zero and writes nothing.

1. Environment: `HOST_ROOT` exists; `INTERVAL` is a non-negative integer.
2. Fetch mode (`FETCH_URL` set) builds and verifies the staging set first.
3. Manifest: `<name> <restart-unit>...`; `#` comments and blank lines are ignored, and `^[A-Z][A-Z0-9_]*=<value>$` is a directive, where unknown directives fail closed. `TARGET_BASE=<major>` is required; every manifest entry must have its `<name>.raw`, and any `<name>-activation.service` shim must match a manifest entry.
4. Staged-raw integrity: `SHA256SUMS` carries one `<64-hex>  <name>.raw` line per entry and every raw hashes to its line, before the gate and deployment.
5. `TARGET_BASE` gate: the first dot-separated component of `IMAGE_VERSION` in `<HOST_ROOT>/etc/os-release` (falling back to `GARDENLINUX_VERSION`) must equal `TARGET_BASE`; otherwise a loud no-op.
6. Reconcile entries and `REMOVE` tombstones, then activate.

Changed raws and shims are copied with `.tmp` and `mv`; equal files are not rewritten, and no host command runs when nothing changed and no marker is pending. `REMOVE=<name>` deletes only that raw, shim, and wants symlink, then runs activation without restarting its units; a duplicate or invalid name fails validation.

One sequence runs when anything changed: `systemd-sysext refresh` → `systemctl daemon-reload` → `systemctl reload dbus.service` → enable and restart changed or new activation shims → restarts for each changed raw. The shims must precede the unit restarts because they run `systemd-sysusers`, `systemd-tmpfiles --create`, and `modprobe`. A failed per-extension restart is logged and the pass exits non-zero; the refresh and shim sequence is fatal, while the dbus reload is best-effort. `/var/lib/extensions/.sci-delivery-pending` makes a later pass replay the activation flow, and is removed on completion.

## Maintainer commands

```sh
bash mkosi/build.sh                            # build all seven raws
bash mkosi/build.sh <image>                    # build one image
bash delivery/build-image.sh                   # stage, verify, build test image
bash delivery/build-image.sh --stage-only      # stage the raws only
bash delivery/build-image.sh --from-staging <dir>
```

`sci-delivery.py --help` lists every environment variable and its default. `build-image.sh` delegates staging to `sci-delivery.py --stage`, which writes standard `sha256sum` lines over the staged raws and verifies them before the docker build; `--from-staging` verifies the shipped file.
## Published boundary

Publishing produces data only: seven `<name>.raw` files, four shims, `extensions.manifest`, `SHA256SUMS`, and `SHA256SUMS.sig`, tagged `<Release>-g<short sha>`. The job removes `sci-delivery.py` and `verity-run.crt`, regenerates and signs the sums, and fails without `SYSEXT_UNHARDENED_SIGNING_KEY`.
