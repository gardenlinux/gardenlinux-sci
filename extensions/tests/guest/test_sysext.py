"""Run through the staged Garden Linux runtime, never on the build host."""
import grp
import json
import os
from pathlib import Path
import pwd
import subprocess

EXTENSIONS = {"sci-10-base", "sci-20-compute", "sci-21-network", "sci-22-storage",
              "sci-40-libvirt", "sci-50-virt-ch", "sci-51-virt-qemu"}


def run(*args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, timeout=120, **kwargs)


def guest_expected(scenarios):
    # Check all preconditions before any command can change the guest.
    guest = os.environ.get("SCI_GUEST")
    assert guest == "1", "SCI_GUEST=1 is required"
    path = os.environ.get("SCI_EXPECTED_JSON")
    assert path == "/opt/sci/expected.json", "SCI_EXPECTED_JSON must name the staged contract"
    expected = json.loads(Path(path).read_text())
    assert expected["scenario"] in scenarios
    assert len(expected["extensions"]) == 7
    assert set(expected["extensions"]) == EXTENSIONS
    assert expected["base"] == {"id": "gardenlinux", "image_version": "2150.9.0"}
    actual = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
                  if "=" in line and not line.startswith("#"))
    assert actual["ID"].strip('"') == expected["base"]["id"]
    assert actual["IMAGE_VERSION"].strip('"') == expected["base"]["image_version"]
    return expected


def merged_extensions():
    status = run("systemd-sysext", "status", "--json=short")
    assert status.returncode == 0, status.stderr
    rows = json.loads(status.stdout)
    assert isinstance(rows, list) and rows, status.stdout
    merged = set()
    for row in rows:
        assert "hierarchy" in row and "extensions" in row, row
        value = row["extensions"]
        names = value if isinstance(value, list) else value.split()
        merged.update(name for name in names if name != "none")
    return merged


def test_delivery_and_host_contract():
    guest_expected({"signed"})
    destination = Path("/var/lib/extensions")
    assert not destination.exists() or not list(destination.iterdir())
    base = {p: Path(p).read_bytes() for p in ("/etc/os-release", "/usr/lib/os-release")}
    staging = Path("/opt/sci")
    for name in EXTENSIONS:
        assert (staging / f"{name}.raw").is_file()
    for name in ("sci-20-compute", "sci-21-network", "sci-22-storage", "sci-40-libvirt"):
        assert (staging / f"{name}-activation.service").is_file()
    for name in ("sci-delivery.sh", "extensions.manifest", "SHA256SUMS"):
        assert (staging / name).is_file()
    env = dict(os.environ, HOST_ROOT="/", STAGING_DIR=str(staging), INTERVAL="0", NSENTER="0")
    first = run("bash", str(staging / "sci-delivery.sh"), env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert merged_extensions() == EXTENSIONS
    qemu_conf = Path("/etc/libvirt/qemu.conf")
    assert qemu_conf.is_file() and not qemu_conf.is_symlink(), qemu_conf
    qemu_text = qemu_conf.read_text()
    assert "remote_display_port_min = 32200" in qemu_text
    assert "remote_display_port_max = 32299" in qemu_text
    assert {p: Path(p).read_bytes() for p in base} == base
    # (uid, home, shell) as the sysusers fragments declare; libvirt-qemu's '-' is the default.
    identities = {
        "nova": (42436, "/var/lib/nova", "/bin/bash"),
        "openstack": (42424, "/var/lib/openstack", "/usr/sbin/nologin"),
        "neutron": (42435, "/var/lib/neutron", "/usr/sbin/nologin"),
        "kvm-node-agent": (42438, "/var/lib/kvm-node-agent", "/usr/sbin/nologin"),
        "openvswitch": (476, "/var/lib/openvswitch", "/usr/sbin/nologin"),
        "libvirt-qemu": (64055, "/var/lib/libvirt", "/usr/sbin/nologin"),
    }
    for name, (uid, home, shell) in identities.items():
        account = pwd.getpwnam(name)
        assert (account.pw_uid, account.pw_dir, account.pw_shell) == (uid, home, shell), name
    assert grp.getgrnam("libvirt").gr_gid == 42437
    assert grp.getgrnam("libvirt-qemu").gr_gid == 64055
    for user, groups in {"nova": {"libvirt", "openvswitch"}, "neutron": {"openvswitch"},
                         "kvm-node-agent": {"libvirt"}, "libvirt-qemu": {"kvm"},
                         "root": {"openvswitch"}}.items():
        account = pwd.getpwnam(user)
        actual = {grp.getgrgid(g).gr_name for g in os.getgrouplist(user, account.pw_gid)}
        assert groups <= actual, (user, actual)
    dirs = [(f"/var/lib/nova{suffix}", mode, 42436, 64055)
            for suffix, mode in (("", 0o750), ("/instances", 0o750), ("/.ssh", 0o600), ("/mnt", 0o750))]
    dirs += [(f"/etc/pki/{name}", 0o755, 42438, pwd.getpwnam("kvm-node-agent").pw_gid)
             for name in ("CA", "libvirt", "qemu")]
    dirs += [(p, 0o755, 0, 0) for p in ("/var/lib/libvirt", "/var/log/libvirt",
              "/var/lib/libvirt/qemu", "/var/log/libvirt/qemu",
              "/etc/openvswitch", "/var/lib/openvswitch", "/var/log/openvswitch")]
    for path, mode, uid, gid in dirs:
        stat = Path(path).stat()
        assert Path(path).is_dir()
        assert (stat.st_mode & 0o7777, stat.st_uid, stat.st_gid) == (mode, uid, gid), path
    for name in ("sci-20-compute", "sci-21-network", "sci-22-storage", "sci-40-libvirt"):
        unit = f"{name}-activation.service"
        enabled = run("systemctl", "is-enabled", unit)
        assert enabled.returncode == 0 and enabled.stdout.strip() == "enabled"
        state = run("systemctl", "show", unit, "--property=ActiveState,SubState,Result")
        assert state.returncode == 0, state.stderr
        assert set(state.stdout.splitlines()) == {"ActiveState=active", "SubState=exited", "Result=success"}
    for unit in ("ovsdb-server.service", "ovs-vswitchd.service", "libvirtd.service"):
        state = run("systemctl", "show", unit, "--property=ActiveState,SubState,MainPID")
        assert state.returncode == 0, state.stderr
        fields = dict(line.split("=", 1) for line in state.stdout.splitlines())
        assert fields["ActiveState"] == "active" and fields["SubState"] == "running", fields
        assert int(fields["MainPID"]) > 0, fields
    for command, fragment in (("qemu-system-x86_64", "QEMU"), ("conntrack", "conntrack")):
        version = run(command, "--version")
        assert version.returncode == 0 and fragment in version.stdout + version.stderr
    # The base owns the storage stack; the delta only activates it.
    for path in ("/usr/sbin/multipathd", "/usr/sbin/iscsid"):
        assert Path(path).is_file(), path
    for unit in ("multipathd.service", "iscsid.socket"):
        state = run("systemctl", "show", unit, "--property=ActiveState")
        assert state.returncode == 0, state.stderr
        assert state.stdout.strip() == "ActiveState=active", (unit, state.stdout)
    # Exercise the delivered L rule on isolated roots with and without a real DB.
    conf = Path("/usr/lib/tmpfiles.d/sci-network.conf")
    assert conf.is_file()
    scratch = Path("/opt/sci/tmpfiles-contract")
    assert not scratch.exists(), "runtime must provide a fresh scratch root"
    for existing in (True, False):
        root = scratch / str(existing)
        db = root / "etc/openvswitch/conf.db"
        db.parent.mkdir(parents=True)
        if existing:
            db.write_bytes(b"existing OVS database\n")
        result = run("systemd-tmpfiles", f"--root={root}", "--create", str(conf))
        assert result.returncode == 0, result.stderr
        if existing:
            assert not db.is_symlink() and db.read_bytes() == b"existing OVS database\n"
        else:
            assert db.is_symlink() and os.readlink(db) == "/var/lib/openvswitch/conf.db"
    # The qemu C rule copies once: absent destinations are materialized, edits survive.
    qemu_fragment = Path("/usr/lib/tmpfiles.d/sci-virt-qemu.conf")
    qemu_payload = Path("/usr/share/sci/etc/libvirt/qemu.conf")
    assert qemu_fragment.is_file(), qemu_fragment
    assert qemu_payload.is_file(), qemu_payload
    for existing in (True, False):
        root = scratch / f"qemu-{existing}"
        source = root / "usr/share/sci/etc/libvirt/qemu.conf"
        source.parent.mkdir(parents=True)
        source.write_bytes(qemu_payload.read_bytes())
        target = root / "etc/libvirt/qemu.conf"
        if existing:
            target.parent.mkdir(parents=True)
            target.write_bytes(b"operator override\n")
        result = run("systemd-tmpfiles", f"--root={root}", "--create", str(qemu_fragment))
        assert result.returncode == 0, result.stderr
        if existing:
            assert not target.is_symlink() and target.read_bytes() == b"operator override\n"
        else:
            assert target.is_file() and not target.is_symlink()
            copied = target.read_text()
            assert "remote_display_port_min = 32200" in copied
            assert "remote_display_port_max = 32299" in copied
    files = list(destination.glob("*.raw")) + [Path("/etc/passwd"), Path("/etc/group"),
                                               Path("/etc/libvirt/qemu.conf")]
    files += [Path(f"/etc/systemd/system/{name}-activation.service")
              for name in ("sci-20-compute", "sci-21-network", "sci-22-storage", "sci-40-libvirt")]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
    second = run("bash", str(staging / "sci-delivery.sh"), env=env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files} == before
    assert merged_extensions() == EXTENSIONS
    assert {p: Path(p).read_bytes() for p in base} == base
