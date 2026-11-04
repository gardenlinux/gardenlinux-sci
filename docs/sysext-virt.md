# SCI system extensions — design

How the SCI layer is built, shaped, signed, activated and delivered as
systemd-sysext images. The delivery step's operational contract is in
`delivery/README.md`. The pytest modules under `extensions/tests/` are
the specification. This document gives the reasons behind them.

## 1. Purpose

The SCI layer is SAP's virtualization stack on Garden Linux. Inside an
image feature it couples every libvirt, Cloud Hypervisor or Open
vSwitch change to a base-image build and release, and a host must
re-image to run the stack.

This project ships the stack as systemd-sysext extensions. An
extension is an additive, read-only `/usr` overlay delivered at runtime
to a *standard* Garden Linux base. A stock `openstack-gardener_prod`
worker plus the extensions becomes a KVM hypervisor host: Open vSwitch,
multipath/iSCSI/NFS tuning, libvirt, a hypervisor driver, and the
chart-consumed agent identities.

Four properties of the mechanism shape every decision below:

- **The base is not touched.** No base image is rebuilt or patched. A
  base A/B update leaves extensions, users and shims in place. They
  re-merge on the new base through ID-only binding.
- **Extensions are purely additive.** They carry `/usr` content and the
  `usr/lib/...` fragments systemd and kmod read. Everything else stays
  with the base.
- **There is no package manager on the host** (`_nopkg` variant). An
  extension is a file set with declarative fragments, never an install
  transaction.
- **Immutable systems are first-class.** Nothing assumes a writable
  `/usr` or a `/var` on the root disk (section 12).

## 2. Target environment

| Item | Value |
|---|---|
| Base | Garden Linux 2150.9.0, `openstack-gardener_prod` amd64, features include `_nopkg` |
| Kernel | 6.18.44-cloud-amd64 (kvm, openvswitch, nf_conntrack in-tree) |
| Userland | Debian trixie, systemd 259.7 (`259.7-3gl1+bp2150`) |
| Consumers | Gardener shoot workers. The consumer does not pin the machine image, so the base drifts across 2150.x minor versions |
| Variants | mutable Gardener worker, and the immutable Garden Linux USI variants (UKI with embedded EROFS root, `/var` split off, `/etc` as a `/var`-backed overlay) |

The suite version is an input, not a detail. It selects the apt suite,
it derives the build timestamp, and it seeds every image's GPT
identifiers (section 5). `Release=` in `mkosi/mkosi.conf` is its single
source of truth.

## 3. Deployables and compositions

Seven images are deployable. Each is a mkosi `Format=sysext` +
`Overlay=yes` delta over the published base tree, so it contains only
its own files.

| Extension | Role | Delta over |
|---|---|---|
| `sci-10-base` | the shared base extension in every composition | `base` |
| `sci-20-compute` | the chart-consumed identities, with memberships and state-dir tmpfiles | `base` |
| `sci-21-network` | Open vSwitch, its units and the OVS identities | `base` |
| `sci-22-storage` | multipath, iSCSI and NFS client tuning | `base` |
| `sci-40-libvirt` | the hypervisor-agnostic libvirt daemon and clients, with vTPM | `base` |
| `sci-50-virt-ch` | the Cloud Hypervisor driver and its firmware | `base` |
| `sci-51-virt-qemu` | the qemu flavour: driver and userland, plus the VNC port policy for `etc/libvirt/qemu.conf` | `base` |

**The prefixes encode the merge order.** systemd-sysext merges the
deployed raws (`/var/lib/extensions/<name>.raw`) in lexical filename
order. The names put `sci-10-base` and the service extensions
(`sci-20-compute`, `sci-21-network`, `sci-22-storage`) ahead of
`sci-40-libvirt`. `sci-40-libvirt` precedes the drivers that need the
libvirt files (`sci-50-virt-ch`, `sci-51-virt-qemu`). The order is a
safety net on top of the single-owner rule (section 8): with every path
owned by exactly one extension, the order cannot shadow content. The
gaps in the numbering (10, 20–22, 40, 50–51) leave room to insert an
extension without renumbering.

The base is not a mkosi subimage. It is the verified extraction of the
published Garden Linux release named by `mkosi/base-image.pin`
(section 4).

The composition is chosen by the delivery manifest, not at build time.
The manifest lists every extension in merge order: `sci-10-base`,
`sci-20-compute`, `sci-21-network`, `sci-22-storage`, `sci-40-libvirt`,
`sci-50-virt-ch`, `sci-51-virt-qemu`. The repo curates the full set.
The boot test uses the full set — fetch mode deploys the release's manifest.

**Every extension builds standalone.** Each has exactly one
`BaseTrees=` entry, the published base tree. None carries
`Dependencies=`. The virt driver packages hard-depend on the libvirt
package set, so apt installs that set into each virt build anyway.
`RemoveFiles=` then drops the paths that belong to `sci-40-libvirt`.
The path lists live in the image configs. At runtime the merged
`sci-40-libvirt` supplies them. A libvirt package change therefore
rebuilds only `sci-40-libvirt`. A virt package change rebuilds only its
own image.

Two alternatives do not work. `RemovePackages=` cannot purge the
libvirt set: the driver payload hard-depends on it, and an apt
simulation deletes the driver with it. Double-shipping the paths is
worse. Overlay shadowing is silent on version skew, and the package
ships `usr/lib/sysusers.d/libvirt.conf` as `g libvirt -`, while
`sci-40-libvirt` pins `g libvirt 42437` (section 8).

A build-tier test backstops the rule: the extracted file sets of
`sci-50-virt-ch` and `sci-51-virt-qemu` must be disjoint from
`sci-40-libvirt`'s. A package update that adds a libvirt-owned path
outside the globs fails the suite. The drift is asymmetric: only
under-removal is caught. Over-removal — a future payload file landing
under a generic glob such as `usr/share/man/*` — is silent, and shows
at best as a missing REQUIRED pin or a guest boot-contract failure.

**The qemu stack is its own extension.**
`libvirt-daemon-system` is never installed, because it depends on the
qemu driver. The signed-delta check asserts the absence of the qemu
binaries and drivers in `sci-40-libvirt` and `sci-50-virt-ch`. A
deployment that merges no qemu extension runs no qemu binaries. One
that wants them merges `sci-51-virt-qemu`.

## 4. Build pipeline

mkosi 27.1 builds everything. It is not on PyPI and the host has no
`ensurepip`, so the venv under `.build/mkosi/venv` is a precondition,
not a build product:

```sh
python3 -m venv .build/mkosi/venv
.build/mkosi/venv/bin/pip install 'mkosi @ git+https://github.com/systemd/mkosi.git@v27.1'
```

- **Base tree.** The extensions are deltas over the **published** Garden
  Linux image, the pinned release artifact. The overlay baseline is
  byte-for-byte the `/usr` the delivery target runs. `mkosi/base-image.pin`
  records the accepted artifact and its digests. `mkosi/base-image.sh
  ensure` verifies both, extracts the tree, and marks it ready under
  `.build/mkosi/base-image/<key>/`. The image configs name the stable
  `current` / `current-kernel` symlinks in that cache, so no config text
  embeds the pin-derived key. One exception: mkosi dereferences the
  symlink when it writes `summary --json`, so the resolved-config pin
  embeds the key path. A base-image pin bump therefore moves
  `mkosi/summary.pin` too. `mkosi/build.sh` runs `ensure` before every
  build, and `VERSION` must equal `Release=` in `mkosi/mkosi.conf`. A
  skew, a digest mismatch or a missing inner member fails closed. A
  rebuilt base tree would make each delta relative to an approximation.
  An extension could then re-ship content the real base owns and shadow
  it after the merge.
- **Deltas.** `Format=sysext`, `Overlay=yes`, and a single `BaseTrees=`
  entry naming the published base tree. mkosi drops every path that
  exists in the base and keeps `usr/` and `opt/`. The emitted image is
  exactly the set of paths the base does not own.
- **Two modes.** `bash mkosi/build.sh` with no argument builds every
  configured subimage in one invocation. The delivery image consumes
  that output. `bash mkosi/build.sh <image>` builds one subimage alone.
  That is the cheap iteration mode. The artifact contract reads the
  build-all output.
- **Tooling.** The tools tree (`mkfs.erofs`, `systemd-repart`, …) is
  bootstrapped once under `.build/mkosi/tools/mkosi.tools` from a
  generated config, and reused through `ToolsTree=`. Runtime state stays
  under `.build/`, so the tracked `mkosi/` directory never changes
  during a build. Builds are unprivileged.
- **Recommendations stay off.** `WithRecommends` must not be set
  anywhere. The apt backend's `Install-Recommends=false` default keeps
  `libvirt-daemon`'s Recommends (the qemu driver) out of the
  `sci-40-libvirt` delta, and `passt`/`qemu-block-extra` out of the qemu
  flavour. mkosi
  copies `mkosi.extra/` files after package installation, so an extra
  tree deliberately overwrites package content. That is how the
  libvirt gid pin and the resident dropin win.

## 5. Reproducibility

Two builds of the same tree must produce the same bytes. A delivered raw
can then be traced to its inputs and re-verified. Two sources of
nondeterminism exist. The build eliminates both by construction:

- **Timestamp.** File times and image contents derive from the suite
  version, never from build time or commit time. `bin/garden-version
  --epoch "$SUITE"` is passed as `--source-date-epoch=` on the mkosi
  command line. That is builder parity with Garden Linux, which derives
  its timestamps from the version too. The setting is CLI-only. mkosi
  27.1 treats `SourceDateEpoch` as multiversal: it cannot be set in a
  subimage, and the CLI value passes down to all of them.
- **GPT identifiers.** Each deployable carries a committed `Seed=` in
  `[Output]`, derived as `sha256("sci:<suite>:<image>")` formatted as a
  UUID (the Garden Linux builder's `uuid_hash` scheme). It fixes the
  repart disk id, the partition UUIDs, and — under signing — the verity
  salt. The identities do not depend on whether an image was built alone
  or in a build-all invocation. `<image>` is the `mkosi.images/`
  directory name, so a rename recomputes the seed. A Garden Linux suite
  bump invalidates every seed. The static input contract re-derives them
  all.

Three committed pins make the rest of the input space conscious:

| Pin | What it pins |
|---|---|
| `mkosi/suite.pin` | `SUITE=` and the sha256 of the apt suite's `InRelease` |
| `mkosi/tools.pin` | sha256 of the bootstrapped tools tree's `mkosi.tools.manifest` |
| `mkosi/summary.pin` | sha256 of the *normalized* `mkosi summary --json` over the whole config tree |

The `InRelease` digest has no automatic check today.

Why three pins: suite.pin covers the apt suite and tools.pin the
toolchain. Neither sees the *resolved* configuration inside the build.
A mkosi default change on a version bump, or an unremarked config edit,
moves the output bytes while every input file stays unchanged. The
summary pin closes that gap. The normalizer
(`extensions/tests/summary.py`, also the regeneration CLI) maps
repo-root-relative paths to `@REPO/<tail>` and other absolute host
paths to `@ABS`, nulls per-image `Seed`, `DelegateRanges` and
`ProxyExclude`, and sorts keys. `DelegateRanges` is uid-dependent in
mkosi 27.1 (root 0, non-root 3). mkosi 27.1 reads `ProxyExclude` from
the invoking shell's no_proxy/NO_PROXY. Without those rules the pin
depends on the host. A *new* volatile field fails the pin instead
of making it flaky. Comment-only edits do not move it.

The suite version has one further guard inside the build. It aborts
when the assembled sandbox apt source `Suites:` differs from `Release=`.
Packages can then never come from a suite other than the one the
version and the epoch name.

**A conscious bump** is the same mechanical sequence for every pin.
Accept the change, then update in the same commit: `Release=` in
`mkosi/mkosi.conf`, the sandbox `Suites:`, `mkosi/suite.pin`, all seven
`Seed=` values, `mkosi/summary.pin`, `mkosi/base-image.pin`, and each
manifest's `TARGET_BASE` if the major line moved.

## 6. Artifact shape

Every deployable is a GPT disk image with a single EROFS root and a
signed dm-verity pair:

| # | Partition name | Type GUID | Content |
|---|---|---|---|
| 1 | `root-x86-64` | `4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709` | the delta as EROFS |
| 2 | `root-x86-64-verity` | `2C7357ED-EBD2-46D9-AEC1-23D437EC2BF5` | the dm-verity hash tree |
| 3 | `root-x86-64-verity-sig` | `41092B05-9FC8-4523-994F-2DEF0408B176` | the PKCS#7 signature over the root hash |

The type GUIDs are the UAPI discoverable-partition values. The verity
pair's values are not guessable from the naming, so a shape assertion
must use these exact GUIDs.

Inside the root partition, each image carries exactly one
`usr/lib/extension-release.d/extension-release.<name>`. The file name
must match the image name (`extension-release.sci-20-compute`, …). A
bare filename is rejected. The file carries:

- `ID=gardenlinux` — the only binding systemd applies.
- `EXTENSION_RELOAD_MANAGER=1` on every image except `sci-10-base`, so
  the merge reloads the manager and makes the extension's units known.
  `sci-20-compute` ships no units. It carries the field for pattern
  consistency.
- `EXTENSION_RESTART_UNITS=` where a restart list exists. It is forward
  compatibility (section 9).
- `ARCHITECTURE=x86-64` (systemd naming, not Debian's `amd64`) and
  `SYSEXT_SCOPE=system`. The value comes from
  `Environment=SYSEXT_SCOPE=system` in each image's `[Build]`, because
  mkosi does not read the process environment.

`VERSION_ID` is deliberately absent (section 13). A wrong
`ARCHITECTURE` or a bare filename does not fail loudly. systemd skips
the extension silently. The artifact contract pins `ID` and
`SYSEXT_SCOPE` on every deployable.

**The `RemoveFiles=usr/lib/verity.d` guard.** The consumer's trust
anchor is `/usr/lib/verity.d/verity.crt`. mkosi does not install it
into extension images, and the published base tree carries no such
directory, so the deployables never ship the anchor. Every deployable
nevertheless carries `RemoveFiles=usr/lib/verity.d` as an explicit
guard. A future base change, or a different mkosi path, must not leak a
copy of the anchor into an extension. The anchor belongs to the host,
and a copy would go stale when it is rotated. Today the line is a
no-op: the directory is absent from all seven raws. `RemoveFiles=`
deletes what a glob matches, so a missing path is not an error.

## 7. Verity signing and the trust anchor

Integrity is enforced at activation, by the consumer. A `SHA256SUMS`
list is enforced by software a booting target has no reason to trust.
An embedded signature plus dm-verity lets `systemd-sysext` (through
`systemd-dissect`) refuse a tampered or substituted image, with no
trusted intermediary at deployment time. The signature travels inside
the image. There is no sidecar to lose or spoof.

Wiring, all in `mkosi/mkosi.conf` under `[Validation]`:

- `Verity=signed` configures the *top-level* image and is
  `SettingScope.local` in mkosi 27.1, so it is not inherited. The
  subimages sign because `Verity=auto` (the default) plus the
  **universal** `VerityKey=` and `VerityCertificate=` selects the
  signed repart definitions (`root` + `root-verity` + `root-verity-sig`).
- The committed anchor is `mkosi/verity.crt`, RSA-4096. A CI run
  replaces the working-tree copy with its run-local certificate and
  never commits it.
- The private key is machine-local, gitignored state at
  `.build/keys/sysext-verity.key` (mode 0600). A machine without it
  fails closed at build time, in the subimage build. The `Format=none`
  main config early-continues before mkosi's input checks, so the
  failure is not at config-parse time.
- `systemd-repart` does the signing, driven by mkosi. `mkosi genkey` is
  not usable here: it hardcodes RSA-2048, writes fixed `mkosi.key` /
  `mkosi.crt` names, and refuses to overwrite. The certificate came
  from an `openssl` invocation with `rsa:4096` and explicit output
  paths.

**This is a development key.** It is an experiment trust root, not a
production one. Key custody — generation ceremony, storage, rotation,
distribution of the public certificate — is an open decision. The key
custody is not hardened, and an operator is expected to rotate the key
into a hardened custodian before the `unhardened` qualifier disappears.
Every build machine and the landing tree must provision the key, or the
build stops.

**The verification path on GL 259.** The kernel dm-verity signature
check runs first and fails `-ENOKEY`. There is no key in the kernel key
ring, and the boot check uses `Firmware=linux`, so there is no UEFI db
either. systemd then falls back to userspace PKCS#7 verification against
`/usr/lib/verity.d/verity.crt` and succeeds. It activates by root hash
and reports the verity volume as verified. The signature is detached
PKCS#7 over the ASCII-hex root hash. The anchor is never shipped by an
extension. The boot runner copies the committed `mkosi/verity.crt` to
`usr/lib/verity.d/verity.crt` in the generated overlay. On a real host
the anchor must arrive through the base pipeline or configuration
management. A future UKI or Secure Boot deployment should load the
certificate into the key ring instead of relying on the userspace path.

**All-or-nothing merge semantics.** One refused or broken extension
aborts the whole merge. The host then has *no* extension merged,
not the healthy subset. That is upstream design in systemd v259
and current main, not a defect. Per-image skips exist only for
`-ENOMEDIUM`, forbidden content and an `extension-release` mismatch.
It is an availability consideration: there is no per-image containment,
and it is not something to patch locally.

**dm-verity is lazy.** dm-verity verifies only the blocks a consumer
reads. A tamper test must hit such a block. A one-byte flip at
offset 8192 is *not* such a block for a two-block EROFS: the byte sits
in zero padding that nothing reads, and the tampered image merges. A
flip in the superblock region (offset 1024) lands in a read block. The
refresh refuses it and aborts the merge. Anyone writing a tamper proof must aim at real
content.

**The consumer policy is the host's decision.** The default policy
still accepts `unprotected` images, so the downgrade hole stays open on
a host until it carries a tightened `sysext.conf`
(`[SysExt] ImagePolicy=root=signed`). Nothing in `delivery/` writes
`sysext.conf`, deliberately. That is a host-side fleet decision. The guest
verity scenario pins the refusal under a tightened policy, and the
all-or-nothing abort that comes with it.

**What is proven where.** The local boot runner demonstrates three
proofs against the prepared published base tree (section 10): a signed
set merging at boot, a read-block tamper refused, and an unsigned probe
refused under a tightened policy. No real host has exercised it yet.
Delivery to a host still rests on the delivery step's `SHA256SUMS` check.
It keeps resting on it until the anchor arrives through an independent
channel and the host policy is tightened.

## 8. Content and identity rules

**Single owner per path.** Every path in a deployed composition has
exactly one owning extension. The base delta covers the base: a path
the published tree owns never appears in an extension. Overlap between
extensions is *not* covered by construction and needs an explicit
assignment plus a build-time assertion. The canonical case is
`usr/lib/sysusers.d/libvirt-qemu.conf`. It lives in `sci-40-libvirt`
only, and `sci-51-virt-qemu` deletes the qemu driver package's
divergent copy with `RemoveFiles=`. The whole libvirt-owned path set
follows the same rule in both virt deltas (section 3). The build-tier
set-difference test is the assertion. Two extensions shipping the same
path do not merge. The overlay shadows one file silently,
`systemd-sysusers` processes only the visible one, and divergence
breaks NFS instance-directory and `/dev/kvm` access on exactly the
hosts that carry the other copy.

**Fixed identities**, shipped as `usr/lib/sysusers.d/` fragments in the
owning extension:

| Identity | ID | Owner | Reason |
|---|---|---|---|
| `openvswitch` | 476 | `sci-21-network` | group owner of the OVS runtime sockets |
| `neutron` | 42435 | `sci-21-network` | OVS agent → `/run/openvswitch/db.sock` |
| `openstack` | 42424 | `sci-20-compute` | legacy identity carried over from the feature |
| `nova` | 42436 | `sci-20-compute` | `libvirt` + `openvswitch`. `/var/lib/nova` owned `nova:libvirt-qemu`. Shell `/bin/bash` for live-migration SSH. The fixed uid is load-bearing for the shared instances NFS |
| `kvm-node-agent` | 42438 | `sci-20-compute` | `libvirt` group, owns `/etc/pki/{CA,libvirt,qemu}` |
| `libvirt` | 42437 | `sci-40-libvirt` | group on `/run/libvirt/libvirt-sock`. `libvirt-daemon-common` declares it dynamically, so the pin is required. Both fragments must agree |
| `libvirt-qemu` | 64055 | `sci-40-libvirt` | the VMM identity. `kvm` member (`/dev/kvm` is root:kvm 0660) |

`g kvm` belongs to the base image and stays dynamic. Fragments
reference it in membership lines only. No fragment pins a gid for it.

**Cross-fragment rule.** sysusers `m` lines fail when the referenced
group does not exist in the same pass. Any fragment referencing a
foreign group also declares it with the same fixed gid.
`sci-20-compute` declares `g openvswitch 476`, `g libvirt 42437` and
`g libvirt-qemu 64055` before its `m` lines. That is idempotent, and it
makes `sci-20-compute` self-sufficient in a composition without
`sci-21-network` or `sci-40-libvirt`. Sysusers never renumbers an
existing group, so a base that pre-creates `libvirt` at another gid
makes the pin unreachable. A future plain-base variant must not
pre-create it.

**State directories** that a sysext cannot carry ship as `tmpfiles.d`
fragments in the owning extension: `/var/lib/nova*`,
`/etc/pki/{CA,libvirt,qemu}`, `/var/lib/libvirt`, `/var/log/libvirt`,
`/var/lib/libvirt/{ch,qemu}`, `/var/log/libvirt/{ch,qemu}`,
`/etc/openvswitch` with
`L /etc/openvswitch/conf.db … /var/lib/openvswitch/conf.db`,
`/etc/iscsi`, `/var/lib/iscsi`. The conf.db rule uses type `L`, never
`L+`, so an existing real database is never replaced. The regular
boot-time tmpfiles pass reads the merged fragments. Fragments naming
extension users only succeed after those users exist. That is why the
activation shim applies them itself (section 9).

**Config placement.** `modprobe.d`, `modules-load.d`, systemd presets,
tmpfiles and sysusers ship under `usr/lib/...`, whose fallbacks systemd
and kmod already read. That keeps `/etc` writable and avoids confext.
Confext is not used: it mounts `/etc` read-only while merged, the
riskiest semantic, and oneshot units cover the need.

The one `/etc` file with no `/usr` fallback is libvirt's
`qemu.conf`. The qemu extension ships a template under
`usr/share/sci/etc/` and its tmpfiles fragment carries
`C /etc/libvirt/qemu.conf`, which the libvirt activation shim applies
after the merge and before `libvirtd` starts. Type `C` is copy-once:
the first activation creates the file, later activations leave
operator edits alone, and a policy change needs the file deleted or
the rule moved to `C+`.

**Payload fragment filenames are not identities.** The sci-prefixed
fragments keep their pre-rename stems on purpose:
`usr/lib/sysusers.d/sci-compute.conf`,
`usr/lib/sysusers.d/sci-network.conf`,
`usr/lib/tmpfiles.d/sci-compute.conf`,
`usr/lib/tmpfiles.d/sci-network.conf`,
`usr/lib/tmpfiles.d/sci-storage.conf`,
`usr/lib/tmpfiles.d/sci-libvirt.conf`,
`usr/lib/tmpfiles.d/sci-virt-ch.conf`,
`usr/lib/tmpfiles.d/sci-virt-qemu.conf` and
`usr/lib/modules-load.d/sci-network.conf`. They are content paths
inside the images, not extension identities, so they are intentionally
unchanged. Do not "complete" the rename there. An extension identity
lives in its `mkosi.images/<name>/` directory, its raw, its
`extension-release.<name>` file and its activation shim. The other
`sci-` names are not identities either and keep their spelling: the
`sci-delivery` delivery step and image, `sci-resident.conf`, the
`sci-boot-*` invocation prefixes, the `sysext-*` branch names, and the
base image's pre-existing `00-sci-libvirt.preset`.

**Kernel modules.** Extensions ship only `modprobe.d` /
`modules-load.d` fragments, never module binaries. The kvm modules are
in-tree and proven on the target kernel, and out-of-tree modules cannot
be shipped in a sysext. `sci-40-libvirt` ships the `kvm-intel` /
`kvm-amd` install rules and a `modules-load.d` list. `sci-21-network`
ships the OVS one. `sci-22-storage` ships the NFS client tuning.
`systemd-modules-load` finishes before the merge at boot, so the
shim's `modprobe` is the reliable path.

**CH processes run as root** under libvirtd. libvirt 11.10 has no
`ch.conf` and no `user`/`group` knob for the CH driver, so there is no
non-root path, and `nova` needs no `kvm` membership. If upstream ever
adds the knob, adopting it is a one-line `m nova kvm` in
`sci-20-compute`.

## 9. Activation model

Extension-shipped units cannot be boot-enabled. PID 1 computes the
whole boot transaction (the `default.target` chain, `Wants=`
enumeration) several seconds *before* the sysext merge.
`EXTENSION_RELOAD_MANAGER=1` makes the units known, but a
daemon-reload cannot inject `Wants=` into an already-queued
transaction. `/etc` symlinks to extension units fail too, because the
unit files do not exist before the merge. `systemctl preset-all` runs
at image build time only.

So activation is **delivery-time activation plus a delivery-installed
`/etc` shim**, and that is the permanent mechanism, not a stopgap. The
initrd-merge path is structurally unavailable for the immutable
target. `systemd-sysext(8)` states that initrd services cannot merge
extensions from `/sysroot/var/lib/extensions` when `/var` is a separate
partition, and a split `/var` is a defining property of the USI
variants.

The delivery step places the raws in `/var/lib/extensions`, runs
`systemd-sysext refresh`, and enables the shim. Each extension ships a
template at `usr/share/sci/activation/<ext>-activation.service`, named
for the extension (`sci-20-compute-activation.service`, …). The
basename minus `-activation.service` must equal the manifest entry.
It copies the template to `/etc/systemd/system/` and enables it
once. The shim works on stock GL because:

- it exists before the merge, so the boot transaction includes it.
- it runs `After=systemd-sysext.service` and carries
  `Wants=systemd-sysext.service`, so a failed merge still lets it fail
  loudly instead of being skipped.
- it uses only base-image binaries: `systemd-sysusers`,
  `systemd-tmpfiles`, `modprobe`, `systemctl`.

It runs sysusers, then `systemd-tmpfiles --create`, then starts
services **directly**. It never starts socket units, because systemd
259 refuses "Socket service already active". The services' own
`Wants=` co-start their sockets. Four extensions have shims
(`sci-20-compute`, `sci-21-network`, `sci-22-storage`,
`sci-40-libvirt`). `sci-10-base`, `sci-50-virt-ch` and
`sci-51-virt-qemu` need none: the flavour drivers plug into `libvirtd`.

`libvirtd` stays resident. `sci-resident.conf` clears the stock
`--timeout 120`, so the service does not read `inactive` while its
sockets keep serving. kvm-node-agent's `is-active` check would
otherwise report a stopped hypervisor.

**The update flow** (all bases):

1. Replace the raws in `/var/lib/extensions` with the new versions.
2. Run `systemctl daemon-reload` and `systemd-sysext refresh`.
3. Run `systemctl reload dbus.service`.
4. Run `systemctl restart` on the changed extensions' units (the lists
   live in the manifest), or reboot.

Step 3 exists because a merged extension can introduce D-Bus policy
and activation files under `usr/share/dbus-1/`, and the daemon running
since boot has not read them. `sci-50-virt-ch` is the real case: it
carries `systemd-container` (a hard dependency of the CH driver
package, with its `machine1` policy), so `systemd-machined.service`
cannot acquire `org.freedesktop.machine1` before the reload. Boot is
unaffected. dbus starts fresh after the merge and reads the merged
policy anyway.

`EXTENSION_RESTART_UNITS=` first appears in systemd 262 and restarts
only units that are already running. GL 2150.x ships 259, where the
field is silently ignored. It stays in the extension-release for
forward compatibility. The manifest's restart lists are what the delivery
step actually uses. A shim's `systemctl start` is a no-op for an
already-active unit, so it does not cover step 4 either.

Units that matter: `openvswitch-switch.service` (+
`ovs-record-hostname`), `libvirtd.service` (+ its three sockets),
`multipathd.service` and `iscsid.socket`. `multipathd.socket` is
deliberately not started. The daemon creates its own sockets, and on a
base that boot-enables `multipathd.service` the socket start fails
deterministically. That would put the shim in a failed state on every
boot.

## 10. Verification

### Host test strategy

The host suite is `extensions/tests/`, run from `.build/venv-test`
(`pytest==8.4.2`, PyYAML). The markers select three tiers: the quick
tier (`not vm and not build`), the expensive `build` tier (image
builds and the resolved-config/tools pins), and one `vm` item that
boots three VMs. The tests that rewrite the shared `.build` state also
carry `build_state_mutating`. Run the suite sequentially: one process
owns the `.build/` scratch and KVM, so two runs must not overlap. There
is no xdist, no collection hook and no `--selftest`. There is no
automatic skip either: a missing dependency fails the check that needs
it.

Each module's docstring states the contract it pins. Fixtures live under
pytest `tmp_path` or the case scratch, so the suite never mutates
tracked files.

**The acceptance pin.** The suite recomputes the resolved mkosi
configuration, normalizes it with `extensions/tests/summary.py`, and
compares the digest with `mkosi/summary.pin`. A conscious configuration
or normalizer change moves the pin in the same commit. A comment-only
edit does not move it.

**Negative proofs are part of the contract.** A negative proof feeds a
broken input and requires a refusal, because a fail-closed guard is a
contract too. An acceptance-only suite would stay green while that
guard silently opened.

**The signed-delta guard.** The base owns the kernel, so an extension
must carry no kernel and no verity anchor. The guard requires exactly
three signed partitions, `ID=gardenlinux`, `SYSEXT_SCOPE=system`, no
`usr/lib/verity.d` and no `usr/lib/modules/*/vmlinuz`. A kernel copy
inside an extension would shadow the base and bind the extension to an
unrelated ABI.

### Boot verification

Merge-at-boot is verifiable locally, without a shoot.
`mkosi/boot-test.sh` calls `extensions/tests/boot_runner.py`, which
generates a per-scenario config under
`.build/test-scratch-vmboot/scenarios/<scenario>/`.
`mkosi/vmtest/mkosi.extra/` is the overlay asset source (boot unit,
cloud-init/tgt masks, anchor). Keeping the check out of
`mkosi/mkosi.images/` avoids build-all and summary-pin coupling.

Each generated image is `Format=disk` with the prepared published base
tree as its root filesystem (`BaseTrees=`). The base tree's kernel is
booted directly (`Linux=`, `Firmware=linux`, no initrd, no bootloader).
`DiskType=nvme` because the GL kernel has `CONFIG_BLK_DEV_NVME=y` but
`CONFIG_VIRTIO_BLK=m`: a virtio-blk root without an initrd panics.
`KVM=yes` makes a missing `/dev/kvm` fail loudly instead of silently
falling back to TCG. The booted identity is the published base's own
`/etc/os-release` (`ID=gardenlinux`, `IMAGE_VERSION`). The runner
stages no os-release.

The overlay shapes the environment for emulation. An empty
`etc/cloud/cloud-init.disabled` disables cloud-init through its own
gate. Otherwise `cloud-init-network` retries the EC2 metadata endpoint
for 240 s. `etc/systemd/system/tgt.service -> /dev/null` masks the
flavor's iSCSI target daemon. It has no targets under QEMU, and its
`ExecStop` stalls shutdown. The runner adds the runtime distribution at
`/opt/sci-tests/dist`, a generated `run_guest.sh`, the
expected-scenario JSON and the committed verity certificate. The guest
unit runs after the merge attempt (`Wants=`, not `Requires=`), so a
refusal is still a valid negative result. On this base the verity
verification and refusal lines are journal-only, not hvc0. The guest
verity test takes a journal cursor before `systemd-sysext refresh` and
looks for the victim and the reason after it.

The runtime archive is a build input, not a build product. The
upstream `gardenlinux/tests/util` builder produces it and it is not
committed, so it must be rebuilt when that upstream revision moves.

The three scenarios are separate builds and boots:

1. **signed set** — the boot runner stages the delivery manifest's
   signed raws and the real `delivery/sci-delivery.sh` under `/opt/sci`.
   The guest runs the delivery step with `HOST_ROOT=/`, expects all seven
   extensions merged, asserts the seven numeric names, checks the fixed
   identities, the tmpfiles/OVS link rule and active services, and runs
   a second pass for idempotence. The staged payload is exercised
   directly, not through the delivery image or Kubernetes.
2. **read tamper** — the scenario places the signed raws in
   `/var/lib/extensions` and flips one byte in the victim
   `sci-20-compute`, at the root partition's superblock offset
   (`start + 1024`). That is a block the consumer reads. The refresh
   must fail, and no extension may merge.
3. **policy refusal** — the scenario builds an unsigned probe, places
   it in `/var/lib/extensions`, and tightens the policy to `[SysExt]
   ImagePolicy=root=signed`. The refresh must refuse the probe.

Each guest wrapper emits exactly one
`SCI-RESULT:<invocation>:<scenario>:<base64-payload>` line to the
console. The payload is JSON with the guest pytest return code and
base64 JUnit XML. `runtime_adapter.parse_guest_result` rejects a
missing or duplicate record, a mismatched invocation or scenario, a
non-zero rc, unnamed cases, or any failure/error/skip. A clean
poweroff alone is never trusted. `boot-test.sh` prints one final JSON
object keyed by scenario with the counts and guest test names. The `vm`
host item runs `--scenario all` and checks that JSON. The manifest
defaults to `delivery/extensions.manifest` (`MANIFEST=` or
`--manifest`). All state stays under `.build/test-scratch-vmboot/`, so
the shared build output is never written.

Not covered, by design:

- SAN/iSCSI hardware discovery. The guest checks the published base's
  binaries and services, not a fabric.
- The flavor's cloud-init and iSCSI target services. The overlay
  disables them, and the boot assertion is the sysext merge, not the
  flavor's cloud plumbing.
- aarch64 execution. The runtime archive contains both architectures,
  but only x86-64 boots here.

## 11. Delivery path

`delivery/` carries the delivery step, which reconciles a staged set
on an interval. The supported output is the signed release: every
manifest raw, every activation shim, `extensions.manifest`,
`verity-run.crt`, `SHA256SUMS` and `SHA256SUMS.sig`. The `publish` job
creates it as release `<Release>-g<short sha>` when the signing key is
configured. Without the key the release steps are skipped and no
release exists. The release carries no delivery program or scripts:
`sci-delivery.sh` stays in the test artifact. The boot test and
integration job prove the merge and activation. Fetch mode only runs
the same test vehicle. A consumer that reuses it owns selection,
staging and restart windows.

**Remote delivery.** `FETCH_URL` switches the step to fetch mode: it
downloads one build under `FETCH_DIR` (default `/var/lib/sci-sysext`),
staging at `$FETCH_DIR/staging`. It requires `FETCH_PUBKEY`, the PEM
public key that verifies `SHA256SUMS.sig`, and compares the optional
`FETCH_SUMS_DIGEST` with the sums. It reads the sums as its inventory,
downloads only the files whose staged hash differs, proves the complete
staged set with `sha256sum -c`, and prunes files the inventory no longer
lists. Each raw's dm-verity signature checks against the base image's
`/usr/lib/verity.d/verity.crt`, which no delivery path can install. Any
failure exits non-zero and writes nothing to the host. A converged pass
needs a staging directory that outlives the container. The shipped
image keeps `STAGING_DIR` and `FETCH_DIR` in the container layer.

**Tombstones.** A manifest may carry `REMOVE=<name>` lines. For each
name the delivery step deletes `$HOST_ROOT/var/lib/extensions/<name>.raw`,
`$HOST_ROOT/etc/systemd/system/<name>-activation.service` and its
`multi-user.target.wants` enable symlink, then runs the normal
activation flow. Removal is idempotent: a name that is
already absent is not a change. A `REMOVE` name that is also an entry,
a duplicate `REMOVE` name or a traversing name fails validation, so a
tombstone cannot unmerge what the same manifest also deploys, and it
cannot reach outside the two deployment trees. This is the only
deletion of a host extension the delivery step performs. Version retention
stays out of `/var/lib/extensions` on purpose (section 13).

**Base-version gate.** systemd-sysext binds an extension to the host by
`ID=` alone, so a 2150 build merges on any Garden Linux line and
silently mixes ABIs. The manifest therefore carries a required
`TARGET_BASE=<major>` directive, the Garden Linux major line the
extensions target (the committed manifest carries
`TARGET_BASE=2150`). Directives fail closed: an unknown directive is a
validation error, so a typo cannot leave the gate unset. After
manifest validation and before any file deployment, the delivery step reads
`$HOST_ROOT/etc/os-release`, takes the first dot-separated component of
`IMAGE_VERSION` (fallback `GARDENLINUX_VERSION`), and compares it with
`TARGET_BASE`. A mismatch, a missing field or an unreadable os-release
is a loud no-op: non-zero exit naming the host line and `TARGET_BASE`,
no raw, no shim, no marker, no tool call, and never a delete. The gate
runs on every pass, before a pass can decide there is nothing to do.

**Staged-raw integrity.** Right after manifest validation and *before*
the gate, every manifest entry must have a `SHA256SUMS` line, and every
staged raw must hash to it. A missing file, a missing line or a
mismatch is fatal and deploys nothing. Integrity runs first, so the
delivery step reports a corrupt staging set as such even on a mismatched
host. The md5 comparison inside the pass is a different check: it detects
deployed-state drift against the staged copy.

`delivery/build-image.sh` stages the raws and shims for a manifest,
stages `SHA256SUMS` (preferring mkosi's own if it covers every manifest
raw and still matches), verifies it with `sha256sum -c`, and tags the
image `sci-delivery:latest` only. The image is a test artifact, not a
published product. It carries no tag that names a producing commit.
`--stage-only` stops after staging and the `SHA256SUMS` verification:
no docker build, no tag. It exists for the `sysext-raws` artifact.
`delivery/Containerfile` pins its Debian base by index digest, not by
tag.

**CI integration.** The delivery pipeline is
`.github/workflows/sysext.yml`, which builds, signs and publishes.
`.github/workflows/test.yml` consumes its output. The workflow files are
the source of truth for their own wiring, and `test_workflows` pins that
structure. `test_prepare_sysext_hv` pins the helper script's behaviour.
In short: the producer's quick tier runs on every push to `sysext-*` and
on PRs (`paths-ignore` for docs and READMEs). Its `producer` job signs
with `secrets.SYSEXT_UNHARDENED_SIGNING_KEY` when the repository secret
exists and with a run-local CA otherwise — the key at
`.build/keys/sysext-verity.key` mode
600, the certificate overwriting the working-tree `mkosi/verity.crt`,
never committed — then builds the venv, ensures the base image, lifts
the runner's AppArmor restriction on unprivileged user namespaces
(mkosi's sandbox builds unprivileged), builds the seven raws and runs
the full suite inside the job before any artifact upload. The test
artifact is `sysext-raws` (the staging as plain files: every raw,
`SHA256SUMS` over all of them, the shims, the manifest, the run cert
and the delivery step), `actions/upload-artifact` pinned to v7.0.2. The
image and the toolchain are test-side derivations, so uploading them
would publish test scaffolding as if it were the product. The boot test
uses the full set, and fetch mode deploys the release's manifest.
The private key never travels in an artifact. The release carries only
its certificate. The third job, `integration` (needs
`producer`, skipped on PRs), calls `test.yml` with the raws artifact name.
It declares its own permissions, `contents: read` plus `actions: read`,
because a called workflow can only downgrade the token the caller
passes down, never elevate it. A fourth job, `publish` (`needs:
producer`, `contents: write`, skipped on PRs), derives the signed
release from the same artifact: it drops the delivery step, regenerates
the sums over the remaining files, signs them, and uploads that set as
release `<Release>-g<short sha>`. Without the signing secret the release
steps are skipped and no release exists.

`test.yml` accepts exactly one of `image_tag` (legacy) or
`sysext_artifact` (the raws artifact), and optionally reads
`artifact_run_id` (default: the current run). The `sysext-raws` artifact
is the integration test's input. It carries the signed extensions, the
manifest, the shims, `SHA256SUMS` and the run certificate, and the
release is derived from it. It restores the run certificate
(`.build/ci-sysext-hv/raws/verity-run.crt` over `mkosi/verity.crt`)
when the artifact carries one, bootstraps the toolchain with
`mkosi/build.sh --bootstrap` (sandbox assembly, pinned tools tree
verified against `mkosi/tools.pin`, published base), preflights it
with `.github/scripts/prepare-sysext-hv.sh --check`, and rebuilds
the delivery image with `delivery/build-image.sh --from-staging`:
the staged `SHA256SUMS` must check the shipped raws, and the image
carries only the unversioned tag because the test checkout need not
be the producing commit. The cert restore is
what makes the run self-consistent: `--prepare` copies the checkout's
`mkosi/verity.crt` into the HV overlay as the dm-verity trust anchor,
and a foreign anchor refuses every merge. A tolerant `else` keeps the
committed certificate when the artifact has no run cert. The manual
local flow signs with the machine-local dev key. Only its certificate
is committed. `--prepare` also grows the HV root disk with the tools
tree's `systemd-repart`. That binary loads its private
`libsystemd-shared` through an absolute host `RUNPATH`, so the script
points `LD_LIBRARY_PATH` back into the tools tree. A host with a newer
systemd does not carry the pinned library. The prepared
disks carry mode 666: the integration libvirt runs qemu as an
unprivileged user and sets `security_driver=none`, so no ownership
transfer happens at domain start and the disk itself must grant the
write access. The setup step also grants `o+x` on the runner home: the
runner images ship it as mode 750, and the qemu user must traverse it
to reach the workspace paths. The delivery `.ref` carries the
fully-qualified image name: the consumer's `ctr` resolves image names
literally, with no docker shortname normalization, so a short ref
cannot address the imported image. The HV root overlay pins `hv1` and
`hv2` in `/etc/hosts`: libvirt derives the p2p migration address from
the destination hostname, and the guests run no upstream DNS. The HV
root defaults to 7G on an 8G disk: the Cloud-Hypervisor stage writes
the full 2.2G Ubuntu raw into the guest, next to the staged qcow2 and
the extensions.

What is proven, and what is not: a pushed run proves the whole chain
on runner hardware — `workflow_call` execution, the same-run artifact
download, the single `sysext-raws` upload, the test-side bootstrap
and rebuild, and both live migrations. Local `act` runs cannot prove
the upload (act's artifact server rejects `upload-artifact` v7.0.2) or
`test_reproducible` (the nested sandbox bakes differing
`user.overlay.origin` xattrs). Run-local signing makes CI artifacts
test-only by construction.

## 12. Immutable systems

The mechanism lands on the immutable target without special-casing:

| Concern | Immutable (USI) mechanics |
|---|---|
| Extension storage | `/var/lib/extensions` — writable by requirement |
| Activation shim | `/etc/systemd/system/` — the `/var`-backed overlay, persists across reboots |
| Users/groups | sysusers writes `/etc/passwd` and `/etc/group` in the same overlay |
| State dirs | `/var/...` via tmpfiles — writable |
| Merging | a read-only EROFS `/usr` is sysext's native case |
| Base updates | a UKI A/B update leaves `/var` untouched: extensions, shim and users survive, ID-only binding re-merges, the shim re-runs after `systemd-sysext.service` |
| Kernel modules | the kernel lives in the UKI. kvm and openvswitch are in-tree |

The remaining gap in the USI line is a base-flavour question, not a
mechanism question. The transitional `sci_usi` image bakes the full SCI
stack (a libvirt backport plus pre-created identities), which is the
very problem this migration eliminates. The clean target is a plain
ignition-capable USI base.

## 13. Decisions and rationale

- **ID-only version binding, gated at delivery time.**
  `extension-release` carries `ID=gardenlinux` and no `VERSION_ID` /
  `SYSEXT_LEVEL`, because GL os-release sets neither, and the
  CloudProfile minor-update strategy drifts the base across 2150.x
  within a stable ABI. systemd cannot express the base line, so the
  manifest's `TARGET_BASE` does it in the delivery step. The upstream
  `SYSEXT_LEVEL` proposal (nova-issues#909) would make systemd do it
  natively. The gate is explicitly a bridge.
- **One build line per Garden Linux line.** Only 2150 needs care now.
  If another line ever needs these extensions, the answer is a separate
  build line — the mechanical release-suite re-pin of section 5 — not a
  conditional build. Until then a 1877 host is a loud, documented no-op
  at the gate.
- **The delivery step is the boot-test vehicle.** It exists to prove, in the
  boot test and the integration job, that a built set merges and
  activates. The supported output is the signed release. A consumer
  that reuses the step owns selection, staging persistence, key and
  anchor provisioning, and restart windows.
- **Reproducibility is an input contract, not a hope.** Version-keyed
  timestamps and per-image seeds, plus the three pins, make a rebuild
  byte-identical and a changed input a reviewable commit. A delivered
  raw can therefore be traced to its inputs and re-verified.
- **Integrity at activation, not only at delivery.** The embedded
  verity signature is what a booting target can check by itself. The
  delivery integrity check protects the payload before it lands.
  Neither replaces the other.
- **Test-first, per behaviour.** The pytest modules are the
  specification, and section 10 states the rules they enforce.
- **The test run is sequential.** One pytest process owns `.build/`
  scratch and KVM. There is no xdist and no collection hook. The
  build-marked reproducibility item rewrites the shared output, and the
  vm item owns the boot scratch, so two runs must not overlap.
- **Activation is delivery-time, permanently.** The initrd path cannot
  serve split-`/var` systems, so the `/etc` shim is the design, not a
  stopgap.
- **Deletion is explicit, never implicit.** The delivery step deletes a host
  extension only for a `REMOVE=<name>` line that the signed manifest
  carries. A merge directory cannot hold two versions of one extension:
  systemd-sysext merges every `*.raw`, so a leftover copy would merge
  beside the new one instead of staying dormant. Trimming an entry
  therefore unmerges nothing, and a removal is a decision an operator
  writes down and signs.
- **Version retention stays out of the merge directory.**
  `systemd-sysupdate` names files with the version and keeps two
  instances. Both copies would merge, and files that the newer version
  dropped would survive the update. sysupdate may fetch, but it must
  not own `/var/lib/extensions`.
- **Single owner per path.** Overlay shadowing is silent. One owner
  plus the artifact and guest contracts is the only way to keep
  identities and configs from diverging across hosts.
- **`/etc` content has narrow carriers.** A sysext merges `/usr` only.
  Most configuration has a `/usr/lib` fallback, and the one file that
  does not — libvirt's `qemu.conf` — is materialized at activation by a
  tmpfiles `C` rule from a template in the qemu extension. That carrier
  is copy-once and fits single files. Broader `/etc` content still needs
  a decision: confexts can carry it, but a merged confext mounts `/etc`
  read-only, the riskiest semantic for a hypervisor host. The 2150 base
  ships `systemd-confext` (259.7), so the runtime support exists.
  Whether confexts arrive as a separate artifact set or as manifest
  entries remains open.
- **Sequential composition over one big image.** Separating identities,
  networking, storage and libvirt means a change to one does not churn
  the others. `sci-20-compute` keeps the chart-consumed UID contract
  stable across virt flavours, and a composition can drop what it does
  not need.
