# SCI system extensions — design

How the SCI layer is built, shaped, signed, activated, and delivered as systemd-sysext images. `../delivery/README.md` is the operational contract; this document gives the reasons.

## 1. Target model

A stock `openstack-gardener_prod` 2150.x worker plus the extensions becomes a KVM hypervisor host: Open vSwitch, multipath/iSCSI/NFS tuning, libvirt, a hypervisor driver, and the chart-consumed agent identities.

- **The base is not rebuilt or patched.** A base A/B update leaves extensions, users, and shims in place; they re-merge through ID-only binding.
- **Extensions are additive file sets** of `/usr` content and the `usr/lib/...` fragments systemd and kmod read; the host has no package manager (`_nopkg`), so an extension is never an install transaction.
- **Immutable systems are first-class.** Nothing assumes a writable `/usr`, a `/var` on the root disk, or confext; `/etc` stays writable.
- `Release=` in `mkosi/mkosi.conf` is the single suite source: it selects the apt suite, derives the build timestamp, and seeds every image's GPT ids. Only the 2150 line is supported; another line needs a separate build line.

## 2. Standalone overlay architecture

Seven overlays: `sci-10-base` (shared base), `sci-20-compute` (identities and state dirs), `sci-21-network` (OVS-Neutron agents and Open vSwitch), `sci-22-storage` (multipath, iSCSI, NFS client), `sci-40-libvirt` (hypervisor-agnostic libvirt and vTPM), `sci-50-virt-ch` (Cloud Hypervisor driver), and `sci-51-virt-qemu` (qemu driver, VNC port policy). Numeric prefixes order the merge: sysext merges `/var/lib/extensions/*.raw` lexically, so 40 follows 10 and precedes the drivers that need its files.

Each extension is a mkosi `Format=sysext` + `Overlay=yes` delta with one `BaseTrees=` entry, the published base tree, and no `Dependencies=`. That tree is the verified extraction of the release named by `mkosi/base-image.pin`, not a mkosi subimage: a rebuilt tree would let an extension re-ship base content.

The virt builds install the libvirt package set through their driver packages' `Depends`; `RemoveFiles=` drops the paths owned by `sci-40-libvirt`, which the merged libvirt extension supplies at runtime. A libvirt change rebuilds only `sci-40-libvirt`; a virt change rebuilds only its own image. The virt flavors stay separate because `libvirt-daemon-system` depends on the qemu driver.

## 3. Reproducibility model

Builds are unprivileged. `mkosi/build.sh` prepares the sandbox, tools tree, and published base tree; runtime state stays under `.build/`, so `mkosi/` never changes. `WithRecommends` must not be set, so the apt default keeps the qemu driver out of `sci-40-libvirt`, and `mkosi.extra/` overrides packages.

Two inputs remove nondeterminism. **Timestamp:** file times and contents derive from the suite, never from build or commit time; `bin/garden-version --epoch "$SUITE"` becomes `--source-date-epoch=`, a CLI-only and multiversal setting in mkosi 27.1. **GPT ids:** each deployable carries `Seed=` in `[Output]` as `sha256("sci:<suite>:<image>")`; it fixes the disk id, partition UUIDs, and the verity salt.

The remaining pin is `mkosi/base-image.pin` (the published base). The tools tree is built from the live Ubuntu 24.04 archive and is not content-pinned, so a mkosi default or config edit can move output bytes while inputs stay unchanged.

A conscious suite bump updates `Release=`, the sandbox `Suites:`, every `Seed=`, `mkosi/base-image.pin`, and each `TARGET_BASE` in `delivery/extensions.manifest` in one commit; the build aborts when they disagree.

## 4. Trust and activation model

Integrity is enforced at activation by dm-verity. A `SHA256SUMS` list is only enforced by software a booting target need not trust; an embedded signature lets `systemd-sysext` refuse a tampered or substituted layer with no trusted intermediary.

`Verity=signed` is local in mkosi 27.1 and not inherited; the subimages use `Verity=auto` with the universal `VerityKey=` and `VerityCertificate=`, and `systemd-repart` signs. `mkosi genkey` is not usable (RSA-2048, fixed names, no overwrite). Each deployable is a GPT disk with an EROFS root, a dm-verity hash tree, and a PKCS#7 signature over the root hash. It carries no kernel and no verity anchor, because the host owns both.

The committed public anchor is `mkosi/verity.crt` (RSA-4096); the private key is gitignored, machine-local state at `.build/keys/sysext-verity.key`, and a machine without it fails closed. This is a development key: every build machine and the landing tree must provision it. On systemd 259 the kernel signature check fails `-ENOKEY`, so the userspace PKCS#7 check against `/usr/lib/verity.d/verity.crt` succeeds; a real host must receive the anchor independently. The downgrade hole closes only with `[SysExt] ImagePolicy=root=signed` in `sysext.conf`; the CI guest writes it, and a production fleet must provision it.

**Activation on systemd 259 and USI.** Extension-shipped units cannot be boot-enabled: PID 1 computes the boot transaction before the sysext merge, and `/etc` symlinks to extension units fail because the files do not exist yet. Activation is therefore delivery-time plus a delivery-installed `/etc` shim, the permanent mechanism. The shims (`sci-20-compute`, `sci-21-network`, `sci-22-storage`, `sci-40-libvirt`) ship as templates under `usr/share/sci/activation/`; they exist before the merge, so `After=`/`Wants=systemd-sysext.service` make a failed merge loud. They run `systemd-sysusers`, then `systemd-tmpfiles --create`, then start services, and never start socket units (systemd 259 refuses "Socket service already active"). On USI, `/var/lib/extensions` is writable, the shim sits in the `/var`-backed `/etc` overlay, and a UKI A/B update keeps `/var`.

Libvirt's `qemu.conf` has no `/usr` fallback, so `sci-51-virt-qemu` ships a template under `usr/share/sci/etc/` and its tmpfiles fragment carries `C /etc/libvirt/qemu.conf`; the libvirt shim applies it after the merge and before `libvirtd` starts. Type `C` is copy-once: the first activation creates the file and later activations preserve operator edits.

`EXTENSION_RESTART_UNITS=` is inert on systemd 259, which needs systemd 262, so the manifest's restart lists are authoritative until then.

## 5. Delivery and CI

`delivery/sci-delivery.py` is the only delivery step. It runs under `uv run --script` on a workstation and under plain `python3` in a minimal container or guest, because it uses only the standard library. `--stage` assembles and checksums the raws; the default mode reconciles a staging directory into a host root; fetch mode downloads and verifies a signed release first. `delivery/build-image.sh` is only the containerisation wrapper.

The `sysext` workflow builds on push and on pull request. `producer` builds and signs the seven raws and uploads them as the `sysext-raws` artifact. `integration` consumes that same artifact and runs the live-migration test. The test guest mounts the artifact share read-only over virtiofs at `/opt/sci`, runs the artifact's own `sci-delivery.py`, and refuses SSH until exactly the manifest's seven extensions merged and `libvirtd.service` is active. `publish` waits for `integration` and releases the signed data on push only.

The guest disk is assembled without mkosi: `mkfs.ext4 -d` formats the root tree into a 7 GiB ext4 image inside a user namespace, `sfdisk` writes a GPT table sized from that image, and `dd` copies the filesystem into the 8 GiB image at the 1 MiB offset. That is unprivileged, owns every file as root, and saves a second mkosi and Ubuntu Noble tools-tree bootstrap in the consumer job.

## 6. Open production items

These remain open and outside `delivery/`; this is not production-ready:

- [#402](https://github.com/gardenlinux/gardenlinux-sci/issues/402): production custody, rotation, and distribution of the signing key;
- an independent host anchor for `verity.crt`;
- `[SysExt] ImagePolicy=root=signed` on the host fleet;
- the deferred `rel-2150-dev` re-pin of the base and suite (the tree still targets 2150.9.0).

Deletion stays explicit: a host extension is deleted only for a `REMOVE=<name>` line. Fetch mode authenticates the whole inventory, manifest included, through `SHA256SUMS.sig`; the published release is signed data. Version retention stays out of `/var/lib/extensions`: `systemd-sysupdate` would merge both instances.
