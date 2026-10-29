import pytest
from plugins.kernel_configs import KernelConfigs
from plugins.parse_file import ParseFile


@pytest.mark.feature("_tdxhost")
@pytest.mark.arch("amd64", reason="TDX is an Intel amd64 feature")
def test_kernel_configs_tdx_host(
    parse_file: ParseFile, kernel_configs: KernelConfigs
):
    """Kernel must be built with TDX host support compiled in."""
    for config in kernel_configs.get_installed():
        parsed_config = parse_file.parse(config.path, format="keyval")
        assert (
            parsed_config["CONFIG_INTEL_TDX_HOST"] == "y"
        ), f"CONFIG_INTEL_TDX_HOST not set to 'y' in {config.path}"
        assert (
            parsed_config["CONFIG_KVM_INTEL_TDX"] == "y"
        ), f"CONFIG_KVM_INTEL_TDX not set to 'y' in {config.path}"


@pytest.mark.feature("_tdxhost")
@pytest.mark.arch("amd64", reason="TDX is an Intel amd64 feature")
def test_kvm_intel_tdx_opt_in(shell):
    """kvm_intel must be told to enable TDX at load (enable_tdx defaults off)."""
    conf = shell(
        "cat /etc/modprobe.d/tdx.conf", capture_output=True
    ).stdout
    assert "options kvm_intel tdx=1" in conf
