# _tdxhost

Enables an SCI host to run Intel TDX confidential guests (KVM TDX).

## What it does

- `etc/modprobe.d/tdx.conf` — `options kvm_intel tdx=1`. The `kvm_intel`
  module parameter `enable_tdx` defaults to `false` (read-only, 0444), so TDX
  host must be opted in at module load.
- `etc/modules-load.d/tdx.conf` — load `kvm_intel` at boot so the opt-in takes
  effect immediately.

## Requirements (not provided by this feature)

This feature is runtime-only. It does **not** compile the kernel. The booted
kernel must already be built with host TDX support:

- `CONFIG_INTEL_TDX_HOST=y` and `CONFIG_KVM_INTEL_TDX=y`

In Garden Linux these live in the kernel package
(`gardenlinux/package-linux`, `config/config.tdx`). The amd64 flavour's
dependencies (`KVM_INTEL=m`, `X86_X2APIC=y`, `X86_MCE=y`, IOMMU) are already
satisfied. The included kernel-config test fails loudly if the kernel lacks
these options.

Hardware/firmware TDX enablement (BIOS, SEAMLDR loading the Intel TDX Module)
is out of scope for the image.
