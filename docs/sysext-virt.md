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
- `Release=` in `mkosi/mkosi.conf` is the single suite source. It selects the apt
  suite, derives the build timestamp, and seeds every image.
- Activation is native systemd. There is no delivery script, no custom unit, and
  no interpreter in the guest.

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

Numeric prefixes order the merge. systemd-sysext merges the images of
`/run/extensions/*.raw` in lexical order, so 40 follows 10 and precedes the
drivers that need its files.

Each image is a `Format=sysext` + `Overlay=yes` delta with one `BaseTrees=` entry,
the verified tree from `mkosi/base-image.pin`. The shared `mkosi/sysext.conf`
drops documentation, locales, man pages, polkit, augeas, and bash completion
from every image.

Module lists and module options come from the SCI feature through `ExtraTrees=`,
so `/etc/modules-load.d` and `/etc/modprobe.d` have one source.

## 3. Build

`./mkosi/build.py` builds every image. `./mkosi/build.py sci-40-libvirt` builds
one. It is a `uv` script, so it carries its own dependency metadata.

A run does this:

1. Verifies `TARBALL_SHA256` and `ROOTFS_TAR_SHA256`, checks `ID=gardenlinux`
   and `IMAGE_VERSION`, and maintains `current`, `current-kernel`, and
   `current-initrd`.
2. Creates `.build/keys/sysext-verity.key` and its certificate if they are
   missing. The key is machine-local state and is never committed.
3. Installs mkosi 27.1 into `.build/mkosi/venv` with `uv` if the venv is absent.
4. Writes the sandbox apt source from `Release=`, copies the certificate to the
   output directory, and calls `mkosi --source-date-epoch=<epoch> build`. mkosi
   builds its own tools tree from `ToolsTree=default` with
   `ToolsTreeDistribution=ubuntu` and `ToolsTreeRelease=noble`, so the script
   holds no tools tree of its own and takes the repository keyring from
   `SandboxTrees=` instead of copying it into the sandbox.

`--base-only` stops after step 1 and prints the tree path, so the guest build
needs no mkosi. Two inputs remove nondeterminism: `bin/garden-version --epoch
"$SUITE"` becomes `--source-date-epoch=`, and each image carries `Seed=` as
`sha256("sci:<suite>:<image>")`, which fixes the disk id, the partition UUIDs,
and the verity salt. The native tools tree comes from the live Ubuntu archive and
is not content-pinned.

A suite bump changes `Release=`, every `Seed=`, and `mkosi/base-image.pin` in one
commit.

## 4. Trust

Each raw is a GPT disk with an EROFS root, a dm-verity hash tree, and a PKCS#7
signature over the root hash. `systemd-sysext` refuses a tampered or substituted
layer with no trusted intermediary, so a checksum list is unnecessary.

The build signs with the machine-local development key under `.build/keys` and
publishes the matching certificate next to the raws. Nothing is committed. On
systemd 259 the kernel signature check fails with `-ENOKEY`, so the host needs
the userspace anchor `/usr/lib/verity.d/verity.crt`. The downgrade hole closes
only with `[SysExt] ImagePolicy=root=signed` in `sysext.conf`.

## 5. Boot sequence

The host mounts the share that carries the raws read-only at `/run/extensions`
and enables `systemd-sysext.service`. From there the standard units do the work:

1. `local-fs.target` mounts `/run/extensions`.
2. `systemd-sysext.service` merges the seven raws and then reloads PID 1,
   because the extension-releases set `EXTENSION_RELOAD_MANAGER=1`. The reload
   loads the drop-in that the merge just added,
   `/usr/lib/systemd/system/systemd-sysext.service.d/10-sci.conf`, so three
   `ExecStartPost=` steps run in `SERVICE_START_POST`: `systemd-sysusers`
   creates the identities, `systemd-modules-load` loads the extension kernel
   modules, and `systemctl start --no-block` starts the extension services.
   The dash in front of the `systemd-modules-load` path keeps an unloadable
   module from failing the merge.
3. `systemd-tmpfiles-setup.service` runs after the merge, so the extension
   `tmpfiles.d` fragments create `/var/lib/nova`, `/var/lib/libvirt`,
   `/etc/openvswitch/conf.db`, and `/etc/libvirt/qemu.conf`.
4. The drop-in from step 2 queues `openvswitch-switch.service`,
   `ovs-record-hostname.service`, and `libvirtd.service` with `--no-block`, so
   the merge does not wait for them. `libvirtd.service` pulls in
   `virtlogd.service` and the libvirt and virtlogd sockets through its own
   `[Unit]` dependencies, so the images carry no `*.upholds/` symlinks and no
   unit list exists outside them. `sci-22-storage` keeps `multipathd.service`
   and `iscsid.socket` from the base image.

A failed merge or a failed `libvirtd.service` stops `ssh.service`: its drop-in
requires `systemd-sysext.service` and asserts the extension units before the
first login. SSH therefore proves the artifact, its signature, and its
activation.

## 6. CI

`.github/workflows/dev.yml` builds the OCI image and the extension raws in
parallel, and runs the integration suite against each artifact in parallel.
`build_sysext` installs `uv`, calls `./mkosi/build.py`, and uploads
`.build/mkosi/output/` as `sysext-raws`; `test_sysext` passes it to `test.yml`.

`prepare-sysext-hv.py` assembles the guest disk without mkosi: it stages the
seven raws into the virtiofs share, formats the tree with `mkfs.ext4 -d` inside a
user namespace, writes a GPT sized from that image with `sfdisk`, and copies the
filesystem in at the 1 MiB offset. It also copies `vmlinuz` and `initrd.img`
from `current-kernel` and `current-initrd`, because `virtio_blk` is a module that
only the initramfs loads. Nothing is installed into the guest beyond the mount,
the sysinit enablement symlink, and the SSH drop-in.

## 7. Open items

These items are outside this branch:

- [#402](https://github.com/gardenlinux/gardenlinux-sci/issues/402): production
  custody, rotation, and distribution of the signing key
- an independent host anchor for the verity certificate
- `[SysExt] ImagePolicy=root=signed` on the host fleet
- the deferred `rel-2150-dev` re-pin of the base and suite
