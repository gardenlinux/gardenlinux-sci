# SCI system extensions

This document explains how the SCI virtualization layer is built, signed, and
activated as systemd-sysext images.

## 1. Target model

A stock `openstack-gardener_prod` 2150.x worker plus the extensions becomes a KVM
hypervisor host. The extensions supply Open vSwitch, multipath/iSCSI/NFS tuning,
libvirt, a hypervisor driver, and the identities the chart consumes.

- The base is not rebuilt or patched. A base A/B update leaves the extensions in
  place, and they merge again by ID.
- An extension is an additive file set over `/usr`. The host has no package
  manager, so an extension is never an install transaction.
- Nothing assumes a writable `/usr`, a `/var` on the root disk, or confext.
- Activation is native systemd: no delivery script, no custom unit, and no
  interpreter in the guest.

## 2. The seven overlays

- `sci-10-base`: shared base, conntrack, parted, and the `systemd-sysext.service`
  drop-in that finishes the merge.
- `sci-20-compute`: identities and state directories.
- `sci-21-network`: Open vSwitch and its units.
- `sci-22-storage`: multipath, iSCSI, and the NFS client tuning.
- `sci-40-libvirt`: hypervisor-agnostic libvirt, vTPM, and the resident
  `libvirtd` policy.
- `sci-50-virt-ch`: Cloud Hypervisor driver.
- `sci-51-virt-qemu`: qemu driver and the VNC port policy.

Numeric prefixes order the merge, because systemd-sysext merges
`/run/extensions/*.raw` in lexical order.

Each image is a `Format=sysext` + `Overlay=yes` delta over the verified tree from
`mkosi/base-image.pin`. The shared `mkosi/sysext.conf` supplies extension-release
metadata, uses `WithDocs=no` to omit documentation and man pages, and removes
locales, polkit, augeas, and bash completion.

`sci-40-libvirt` emits its signed raw and a supplemental tar in one invocation
with `SplitArtifacts=tar`. The tar keeps package metadata for the driver builds'
additional lower layer; the drivers depend on it and consume
`%O/sci-40-libvirt.tar`. The tar is a private build output, not part of the
public raws artifact.

Module lists and options come from the SCI feature through `ExtraTrees=`, so
`/etc/modules-load.d` and `/etc/modprobe.d` have one source.

## 3. Build

    MKOSI="mkosi @ git+https://github.com/systemd/mkosi.git@v27.1"
    uv tool run --from "$MKOSI" mkosi --directory mkosi genkey
    uv tool run --from "$MKOSI" mkosi --directory mkosi build

`genkey` creates the machine-local `mkosi/mkosi.key` and `mkosi/mkosi.crt` on
first use. `--dependency sci-40-libvirt` builds one image and its dependencies.

mkosi runs `mkosi/mkosi.sync` before it checks the image inputs. The hook
verifies the release archive SHA-256 from `mkosi/base-image.pin` and extracts
`rootfs.tar` for `BaseTrees=` and `base.qcow2` for the guest disks into the
gitignored `mkosi/base-image/`, which the build-source bind mounts read-write
at `$SRCDIR/base-image`. The same hook runs outside mkosi with `SRCDIR` set to
the `mkosi/` directory, so the guest preparation needs no mkosi and shares the
cache. mkosi builds its own tools tree from
`ToolsTree=default` and takes `mkosi/mkosi.sandbox/` and the repository keyring
from `SandboxTrees=`.

Two inputs remove nondeterminism: `SourceDateEpoch=` in `mkosi.conf` fixes the
build timestamp, and each image carries `Seed=` as
`sha256("sci:<suite>:<image>")`, which fixes the disk id, the partition UUIDs,
and the verity salt. The native tools tree comes from the live Ubuntu archive and
is not content-pinned.

A suite bump changes `Release=`, `SourceDateEpoch=`, every `Seed=`, and
`mkosi/base-image.pin` in one commit.

## 4. Trust

Each raw is a GPT disk with an EROFS root, a dm-verity hash tree, and a PKCS#7
signature over the root hash. `systemd-sysext` refuses a tampered or substituted
layer with no trusted intermediary, so a checksum list is unnecessary.

The build signs with the machine-local development key and publishes the matching
certificate next to the raws. Nothing is committed. On systemd 259 the kernel
signature check fails with `-ENOKEY`, so the host needs the userspace anchor
`/usr/lib/verity.d/verity.crt`. The downgrade hole closes only with
`[SysExt] ImagePolicy=root=signed`.

## 5. Boot sequence

The host mounts the share that carries the raws read-only at `/run/extensions`
and enables `systemd-sysext.service`. The standard units do the rest:

1. `local-fs.target` mounts `/run/extensions`.
2. `systemd-sysext.service` merges the seven raws and reloads PID 1, because the
   extension-releases set `EXTENSION_RELOAD_MANAGER=1`. The reload loads the
   drop-in that the merge just added, and its `ExecStartPost=` steps run
   `systemd-sysusers`, `systemd-modules-load`, and a queued start of
   `openvswitch-switch.service`, `ovs-record-hostname.service`, and
   `libvirtd.service`. The dash in front of `systemd-modules-load` keeps an
   unloadable module from failing the merge.
3. `systemd-tmpfiles-setup.service` runs after the merge, so extension
   `tmpfiles.d` fragments create `/var/lib/nova`, `/var/lib/libvirt`,
   `/etc/openvswitch/conf.db`, and `/etc/libvirt/qemu.conf`.
4. `libvirtd.service` pulls in `virtlogd.service` and the libvirt and virtlogd
   sockets through its own `[Unit]` dependencies. `sci-22-storage` keeps
   `multipathd.service` and `iscsid.socket` from the base image.

A failed merge or a failed `libvirtd.service` stops `ssh.service`: its drop-in
requires `systemd-sysext.service` and asserts the extension units before the
first login. SSH therefore proves the artifact, its signature, and its
activation.

## 6. CI

`.github/workflows/dev.yml` builds the OCI image and the extension raws in
parallel, and runs the integration suite against each artifact in parallel.
`build_sysext` installs `uv`, runs `mkosi genkey` and `mkosi build`, and uploads
the seven raws plus `mkosi/mkosi.crt` as `sysext-raws`; the supplemental tar is
excluded. `test_sysext` passes the fixture to `test.yml`. The consumer action
uses the runner's Python and does not install `uv`.

`prepare-sysext-hv.py` prepares HV1 and HV2 from the pinned `base.qcow2` without
rebuilding a guest filesystem: it stages the raws and the certificate into the
virtiofs share, creates copy-on-write `HV1.qcow2` and `HV2.qcow2` overlays, and
passes guest mounts, SSH authorized keys, `/etc/hosts`,
`SYSTEMD_SYSEXT_IMAGE_POLICY=root=signed`, and unit drop-ins through native
systemd SMBIOS credentials in `hv-sysext.xml`.

## 7. Open items

These items are outside this branch:

- [#402](https://github.com/gardenlinux/gardenlinux-sci/issues/402): production
  custody, rotation, and distribution of the signing key
- an independent host anchor for the verity certificate
- `[SysExt] ImagePolicy=root=signed` on the host fleet
- the deferred `rel-2150-dev` re-pin of the base and suite
