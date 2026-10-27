# SCI delivery step

**Scope.** `sci-delivery.sh` is this repository's boot-test vehicle: the
boot test and the integration job run it inside a Garden Linux VM to
prove that the built set merges and activates. It is not a supported
consumer agent. The supported output is the built extension set.
How a consumer selects, stages
and merges those bytes, provisions the verity anchor in the base image,
and keeps a staging directory across restarts is the consumer's
decision. Image mode and fetch mode are two ways to run the test
vehicle, not two delivery products.

## What the delivery step does

The following is the pass as the boot test runs it; fetch mode is a
variant of the same pass.

`sci-delivery.sh` runs against a host root mounted at `HOST_ROOT` (default
`/host`):

* a manifest entry is `<name> <restart-unit>...`; the unit list may be empty,
  `#` comments and empty lines are ignored, and the manifest is validated
  completely before anything is changed. The manifest also carries the
  `TARGET_BASE=<major>` directive, and the pass is gated on the host's base
  line (see "Base-version gate" below);
* the staged raws are verified against `SHA256SUMS` right after manifest
  validation and before the base-version gate or any deployment (see
  "Staged-raw integrity" below);
* a missing or content-different `<name>.raw` is copied to
  `<HOST_ROOT>/var/lib/extensions/` (write `.tmp`, then `mv`);
* a missing or content-different `<name>-activation.service` shim is copied
  to `<HOST_ROOT>/etc/systemd/system/` the same way;
* a `REMOVE=<name>` directive makes the delivery step delete only what it
  names: `<HOST_ROOT>/var/lib/extensions/<name>.raw`,
  `<HOST_ROOT>/etc/systemd/system/<name>-activation.service` and its
  `multi-user.target.wants` enable symlink, before the entries reconcile,
  and never restarts that extension's units;
* when anything changed, exactly one sequence runs: `systemctl
  daemon-reload` → `systemd-sysext refresh` → `systemctl reload
  dbus.service` → `systemctl restart <units>` for each extension whose raw
  changed → `systemctl enable <name>-activation.service` for every shim,
  plus `systemctl restart <name>-activation.service` for changed or new
  entries;
* a failing per-extension unit restart is tolerated: it is logged on
  stderr with the extension name and the units, and the flow continues —
  the remaining restarts and the whole shim loop still run, so one broken
  unit (for example a service whose artifact and base version are skewed)
  does not block the activation of healthy extensions. The pass then
  exits non-zero, so the failure stays visible to the operator and in the
  container status. Only the unit restarts are tolerant; the refresh trio and
  the shim enable/restart calls stay fatal, because they are the
  activation machinery itself. Service retries belong to systemd; the
  delivery step owns deployment and activation;
* when nothing changed, no host-mutating command runs at all;
* crash-safe activation reconciliation:
  `<HOST_ROOT>/var/lib/extensions/.sci-delivery-pending` is written
  immediately before the activation flow
  and removed only after the flow completed. A pass that starts with the
  marker present replays the full activation flow for every entry even when
  no file changed, so a pass killed after deploying files (SIGKILL, OOM,
  eviction, a fatal activation call) cannot strand raw+shim files without
  refresh, restarts and enablement. The marker is cleared whenever the flow
  completes, even if an individual unit restart failed: a service-level
  failure is not an activation stranding. The dot-file is not an
  extension: it is not in the manifest and is never deployed or
  md5-compared;
* as container PID 1 the delivery step traps `TERM`/`INT` and exits immediately.
  The interval wait uses a background `sleep` plus `wait`, so the trap is
  not deferred until the sleep returns.

Host-mutating commands go through `nsenter -t 1 -m --` (`NSENTER=1`, the
default) so they run in PID 1's mount namespace; file operations use the
`HOST_ROOT` paths directly. `INTERVAL` (default 60) is the seconds between
passes; `INTERVAL=0` runs one pass and exits.

## Staged-raw integrity

The staging directory carries `SHA256SUMS` in standard `sha256sum` output:

```
<64-hex-digest>  <name>.raw
```

one line per manifest entry (extra lines are ignored; a staged raw without a
manifest entry is never deployed). Right after manifest validation and
**before** the base-version gate and any deployment, the delivery step

* requires the file (a missing `SHA256SUMS` is fatal and names it),
* requires a line for every manifest entry (a missing line is fatal and names
  the entry), and
* recomputes every staged raw's sha256 and compares it with its line (a
  mismatch is fatal and names the raw).

All three are pre-deployment failures: no raw, no shim, no pending marker and
no `systemctl`/`systemd-sysext` call. Integrity runs before the gate on
purpose — a corrupt staging set must be reported as such even on a host whose
base line would also be refused (the raw-corrupt (b) variant pins that order).

This check guards the content the delivery image ships. The pass's md5
comparison is a different thing: it detects drift between an already-deployed
host file and the staged copy, and triggers re-deployment. md5 is fine for
that; the sha256 check is the integrity statement about the staged bytes.

## Base-version gate

The manifest grammar is v2: `<name> <restart-unit>...` is an entry, `#`
comments and empty lines are ignored, and a line matching
`^[A-Z][A-Z0-9_]*=<value>$` is a **directive**, not an entry. Unknown
directives are a validation failure (fail closed, so a typo cannot leave the
gate unset). Two directives exist: `TARGET_BASE`, which is required,
and `REMOVE`, the tombstone (see "The delivery manifest"). The gate
reads:

```
TARGET_BASE=2150
```

`TARGET_BASE` is the major line of the Garden Linux release the extensions
were built for — the major of `Release=` in `mkosi/mkosi.conf` (today
`2150.9.0`). It sits in the header block of the manifest
(`delivery/extensions.manifest`).
Zero, duplicate, or non-digit values fail validation before anything is
deployed, and `test_artifact_contracts::test_static_input_contract` keeps the
committed manifest in sync with `Release=`.

Why it exists: systemd-sysext binds an extension to the host by the `ID=` in
its extension-release only, so a 2150 build merges on *any* Garden Linux
line and silently mixes ABIs. The upstream `SYSEXT_LEVEL` proposal
(nova-issues#909) would make systemd enforce this natively; until it lands
the delivery step does. After manifest validation and the staged-raw integrity
check, and before any file deployment, it

* reads `<HOST_ROOT>/etc/os-release`,
* takes the first dot-separated component of `IMAGE_VERSION` (fallback
  `GARDENLINUX_VERSION`) as the host's major line, and
* compares it with `TARGET_BASE`.

On a match the pass proceeds exactly as described above. On a mismatch, a
missing version field, or an unreadable `os-release`, the pass deploys
nothing — no raw, no shim, no pending marker — makes no `systemctl`/
`systemd-sysext` call, and exits non-zero with one `[sci-delivery] error:`
line naming the host line and `TARGET_BASE` (plus `os-release` when that file
is the problem).

Failure semantics: a mismatched host is a **loud no-op**. The gate never
deletes anything and runs on every pass, so a host that moved to another
base line stays undeployed while the delivery step's log repeats the error on
every pass — the signal to rebuild (or re-select) the delivery image for that
line, not to widen the gate.

## Build the test image

Build the extension raws first with mkosi: `bash mkosi/build.sh` (no
argument) builds every image in one invocation, which is what a delivery
image needs; `bash mkosi/build.sh <image>` builds one image and its
dependency closure and stays the iteration mode.
`build-image.sh` stages every manifest entry from `.build/mkosi/output/`; a
missing raw fails the build before docker runs. It also stages
`SHA256SUMS` (one standard `sha256sum` line per manifest entry, manifest
order) and verifies the staged file with `sha256sum -c` before the docker
build. Source choice: mkosi's own checksum output is preferred when
`<RAW_DIR>/SHA256SUMS` covers every manifest raw **and** its hashes still
match the copied raws (builder provenance); otherwise the script generates
the file with `sha256sum` over the staged raws. mkosi's `Checksum=` is
deliberately not enabled here: the setting has `local` scope in mkosi 27.1,
so a root `mkosi.conf` value never reaches the sysext subimages, and each
subimage would write a separate `<image>.SHA256SUMS` in binary-mode format.

The image is a test artifact, tagged only `$IMAGE_TAG` (default
`sci-delivery:latest`). There is no published image; the image is a
test artifact.

```sh
bash delivery/build-image.sh          # validates, stages into .build/delivery/staging, builds the image
docker images | grep sci-delivery     # sci-delivery:latest
```

The Containerfile pins its base image by digest
(`docker.io/library/debian@sha256:…`, resolved from `debian:trixie-slim`). To bump it:

```sh
docker buildx imagetools inspect docker.io/library/debian:trixie-slim
```

take the index `Digest:`, and update the `FROM` line in the same commit that
records why. A tag would let the delivery step's base image move without
review.

`build-image.sh` fails loudly (before invoking docker) when a manifest entry
has no raw, or when a staged shim is not listed in the manifest.

## The delivery manifest

`delivery/extensions.manifest` lists every extension the repo ships, in
merge order. The boot test uses the full set; fetch mode deploys the
release's manifest. The step never deploys a raw the manifest does not
list.

A tombstone is a directive, not an entry:

```
REMOVE=sci-51-virt-qemu
```

For each `REMOVE` name the delivery step deletes
`<HOST_ROOT>/var/lib/extensions/<name>.raw`,
`<HOST_ROOT>/etc/systemd/system/<name>-activation.service` and its
`multi-user.target.wants` enable symlink, then runs the normal activation
flow. Removal is idempotent: a name that is
already absent is not a change. A name that is also an entry, a
duplicate `REMOVE` name, or a name that does not match
`[A-Za-z0-9][A-Za-z0-9._-]*` fails validation, so a tombstone cannot
unmerge content the same manifest deploys, and it cannot reach outside
the two deployment trees. Dropping an entry deletes nothing on the host;
only a tombstone does.

The `sci-51-virt-qemu` entry carries no restart units: the qemu driver
plugs into the already-running `libvirtd`, and that service is restarted
by `sci-40-libvirt`'s entry — this extension ships no units of its own.

Build every raw first (`bash mkosi/build.sh`), then stage:

```sh
bash delivery/build-image.sh
```

`build-image.sh` stages the entries of the manifest and fails before
docker runs when one of them has no raw.

## Remote delivery (fetch mode, boot test)

`FETCH_URL` switches the delivery step from image mode to fetch mode: it
then downloads one build from that base URL, proves it, and reconciles
it. This mode proves that a published release is consumable; it is not a
supported deployment mechanism. A fetch pass does not trust the staging
directory it finds.

| Variable | Meaning |
|---|---|
| `FETCH_URL` | base URL of one build (unset selects image mode) |
| `FETCH_PUBKEY` | PEM public key for `SHA256SUMS.sig` (required) |
| `FETCH_SUMS_DIGEST` | pinned sha256 of `SHA256SUMS` (optional) |
| `FETCH_DIR` | writable fetch root (default `/var/lib/sci-sysext`) |

`STAGING_DIR` defaults to `$FETCH_DIR/staging` in fetch mode when the
caller does not set it. Image mode keeps `/opt/sci`.

A pass verifies `SHA256SUMS` and `SHA256SUMS.sig`, downloads only the
files whose staged hash differs, proves the complete set with
`sha256sum -c`, and prunes files the inventory no longer lists. Every
failure exits non-zero and writes nothing to the host.

A converged pass downloads the two manifest files only. That needs
`STAGING_DIR` and `FETCH_DIR` on a directory that outlives the
container; the shipped image keeps them in the container layer.

## Safety properties

* **Deletes only what a `REMOVE` directive names**: unlisted host
  extensions in `/var/lib/extensions` are left alone, and the delivery step
  only writes manifest entries. A `REMOVE` name loses its raw and its shim,
  and nothing else. The only other file it removes is its own
  `.sci-delivery-pending` marker.
* **Idempotent**: unchanged files are not rewritten (no mtime change) and
  an unchanged pass makes zero systemctl/systemd-sysext calls.
* **All-or-nothing validation**: a missing manifest, a missing raw, an
  unlisted shim, a missing `HOST_ROOT`, a non-numeric `INTERVAL`, a
  missing/duplicate/non-digit `TARGET_BASE`, a missing `SHA256SUMS`, a
  missing entry line, a staged raw that does not match its line, or a host
  base line that does not match `TARGET_BASE` aborts before any host
  mutation.
* **Staged-content integrity**: every raw the delivery step deploys was
  verified against the image's `SHA256SUMS` in that same pass, before any file
  deployment; the check runs before the base-version gate so a corrupt
  staging set is never masked by a host-line mismatch.
* **Per-unit restart tolerance**: a failing `systemctl restart <units>` for
  one extension is logged on stderr and the flow continues (all shims are
  still enabled); the pass exits non-zero. The failure is visible instead
  of silently ignored, but it does not strand other extensions' activation.
* **Atomic writes**: each file is copied to `<dest>.tmp` and renamed.

## Known limitation

`EXTENSION_RESTART_UNITS=` in the extension-release files is inert on
Garden Linux 2150.x (systemd 259 needs >= 262). The restart lists therefore
live in `delivery/extensions.manifest` and must be kept in sync with the
extensions' `EXTENSION_RESTART_UNITS=` metadata.
