"""Behaviour of .github/scripts/prepare-sysext-hv.sh: the HV renderer and the disk/overlay preparation."""
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "prepare-sysext-hv.sh"
ROOT_GUID = "4f68bce3-e8cd-4db1-96e7-fbcaf984b709"
ROOT_UUID = "0d9f4c5e-6b2a-4e7d-9c3f-8a1e5d7c6b23"
MACS = {"HV1": "52:54:00:12:34:56", "HV2": "52:54:00:65:43:21"}
QEMU_NS = "{http://libvirt.org/schemas/domain/qemu/1.0}"
TABLE = {"partitiontable": {"label": "gpt", "device": "/dev/nvme0n1",
                            "partitions": [{"number": 1, "type": ROOT_GUID, "name": "ROOT", "uuid": ROOT_UUID}]}}


@pytest.fixture(autouse=True)
def script_present():
    assert SCRIPT.is_file(), f"{SCRIPT} missing: the sysext HV renderer does not exist"


def write_table(tmp_path):
    path = tmp_path / "table.json"
    path.write_text(json.dumps(TABLE), encoding="utf-8")
    return path


def run_cli(*args):
    return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("hv", tuple(MACS))
def test_rendered_hv_xml(hv, tmp_path):
    out, kernel, prefix = tmp_path / "out", tmp_path / "vmlinuz", tmp_path / "disks"
    res = run_cli("--render", str(write_table(tmp_path)), str(out), str(kernel), str(prefix))
    assert res.returncode == 0, res.stderr
    assert {(out / f"{n}.xml").is_file() for n in MACS} == {True}
    doc = ET.parse(out / f"{hv}.xml").getroot()
    assert doc.get("type") == "kvm"
    assert doc.findtext("name") == hv
    mem = doc.find("memory")
    assert (mem.text, mem.get("unit")) == ("6", "GiB")
    assert doc.find("cpu").get("mode") == "host-passthrough"
    os_ = doc.find("os")
    assert os_.get("firmware") is None
    assert os_.find("loader") is None
    assert os_.findtext("kernel") == str(kernel)
    cmdline = os_.findtext("cmdline")
    assert f"root=PARTUUID={ROOT_UUID}" in cmdline
    assert "console=ttyS0" in cmdline
    assert doc.find("devices/disk") is None
    iface = doc.find("devices/interface")
    assert iface.find("mac").get("address") == MACS[hv]
    assert iface.find("source").get("network") == "default"
    assert iface.find("model").get("type") == "virtio"
    args = [a.get("value") for a in doc.find(f"{QEMU_NS}commandline")]
    assert args == [
        "-blockdev", f"driver=qcow2,node-name=sciroot,file.driver=file,file.filename={prefix}/{hv}.qcow2",
        "-device", f"nvme,serial={hv},drive=sciroot,bootindex=1",
    ]


@pytest.mark.parametrize("case,keyword", [("malformed", "json"), ("missing-root", "root"),
                                          ("dup-root", "root"), ("bad-uuid", "uuid")])
def test_rejects_bad_table_before_output(case, keyword, tmp_path):
    path = tmp_path / "table.json"
    if case == "malformed":
        path.write_text("not json", encoding="utf-8")
    else:
        data = json.loads(json.dumps(TABLE))
        parts = data["partitiontable"]["partitions"]
        if case == "missing-root":
            data["partitiontable"]["partitions"] = [p for p in parts if p["type"] != ROOT_GUID]
        elif case == "dup-root":
            parts.append(dict(parts[-1], number=9))
        elif case == "bad-uuid":
            parts[-1]["uuid"] = "nope"
        path.write_text(json.dumps(data), encoding="utf-8")
    res = run_cli("--render", str(path), str(tmp_path / "out"), str(tmp_path / "vmlinuz"), str(tmp_path / "disks"))
    assert res.returncode != 0
    for name in MACS:
        assert not (tmp_path / "out" / f"{name}.xml").exists(), name
    assert keyword in (res.stdout + res.stderr).lower()


@pytest.mark.parametrize(
    "argv", [[], ["--frobnicate", "a", "b", "c", "d"], ["--render", "a", "b", "c"]],
    ids=["no-args", "unknown-option", "too-few-args"],
)
def test_cli_validation(argv):
    res = run_cli(*argv)
    assert res.returncode != 0, res.stdout
    assert "usage" in (res.stdout + res.stderr).lower() and "--render" in res.stdout + res.stderr


def test_prepare_runs_tools_tree_repart_with_its_own_libs():
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    grows = [i for i, l in enumerate(lines)
             if "systemd-repart" in l and "--offline=yes" in l]
    assert len(grows) == 1, (
        f"expected exactly one offline systemd-repart invocation (the HV root grow step), found {len(grows)}")
    window = lines[max(0, grows[0] - 3):grows[0] + 1]
    assert any("LD_LIBRARY_PATH=" in l and "TOOLS_TREE" in l
               and "usr/lib/x86_64-linux-gnu/systemd" in l for l in window), (
        "the grow invocation must set LD_LIBRARY_PATH to the tools tree's usr/lib/x86_64-linux-gnu/systemd")


def test_prepare_makes_disks_writable_by_the_qemu_user():
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    converts = [i for i, l in enumerate(lines) if "qemu-img convert" in l]
    assert len(converts) == 1, (
        f"expected exactly one qemu-img convert in prepare-sysext-hv.sh (the HV disk creation), found {len(converts)}")
    window = lines[converts[0]:converts[0] + 6]
    assert any("chmod 666" in l for l in window), (
        "the disk creation must chmod 666 each qcow2: qemu runs as an unprivileged user and cannot write a 644 disk")


def test_prepare_pins_the_hv_names_in_the_guest_hosts_file():
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    hosts = [i for i, l in enumerate(lines) if "overlay/etc/hosts" in l]
    assert hosts, (
        "the prepare overlay must write /etc/hosts into the HV root image")
    text = "\n".join(lines[hosts[0]:hosts[-1] + 3])
    assert "192.168.122.2 hv1" in text and "192.168.122.3 hv2" in text, (
        "the overlay /etc/hosts must pin '192.168.122.2 hv1' and '192.168.122.3 hv2' for the p2p migration")


def test_prepare_sizes_the_hv_root_for_the_chv_stage():
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    defaults = {}
    for l in lines:
        if l.startswith("HV_DISK_SIZE=") or l.startswith("HV_ROOT_SIZE="):
            name, _, value = l.partition("=")
            defaults[name] = value.partition(":-")[2].rstrip("}")
    assert defaults.get("HV_DISK_SIZE") == "8G" and defaults.get("HV_ROOT_SIZE") == "7G", (
        f"the HV disk/root defaults must be 8G/7G (found {defaults}) to fit the ~2.2G ubuntu.raw in the CHV stage")
