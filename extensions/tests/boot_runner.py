#!/usr/bin/env python3
"""Host helper for mkosi/boot-test.sh: stage the runtime, build one isolated
image per scenario, boot it, and return the runtime adapter's counts."""
import argparse, hashlib, json, os, re, shutil, signal, subprocess, sys, uuid
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import runtime_adapter  # noqa: E402
TYPES = ("4f68bce3-e8cd-4db1-96e7-fbcaf984b709", "2c7357ed-ebd2-46d9-aec1-23d437ec2bf5",
         "41092b05-9fc8-4523-994f-2def0408b176")
MODULES = {"signed": "test_sysext.py", "tamper": "test_verity.py", "policy": "test_verity.py"}
VICTIMS = {"tamper": "sci-20-compute", "policy": "unsignedprobe"}
WRAPPER = r'''#!/bin/sh
set -u
dist=/opt/sci-tests/dist; arch=$(uname -m); log=/run/sci-pytest.log; xml=/run/sci-junit.xml
scenario=${SCI_SCENARIO:-}; invocation=${SCI_INVOCATION:-}
[ -d /run/systemd/system ] && [ -x "$dist/run_tests" ] && [ -f /opt/sci/expected.json ] || { printf 'SCI-GUEST: runtime contract missing\n' >/dev/console; exit 1; }
case "$scenario" in signed) module=test_sysext.py;; tamper|policy) module=test_verity.py;; *) printf 'SCI-GUEST: invalid scenario %s\n' "$scenario" >/dev/console; exit 1;; esac
rc=0
SCI_GUEST=1 SCI_SCENARIO="$scenario" SCI_EXPECTED_JSON=/opt/sci/expected.json TEST_NG_EXIT_0=0 PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 SYSTEMD_LOG_TARGET=journal \
"$dist/run_tests" --system-booted --allow-system-modifications -p pytest_metadata.plugin --show-capture=no -o junit_logging=no --junitxml="$xml" "integration/sci/$module" >"$log" 2>&1 || rc=$?
"$dist/runtime/$arch/bin/python" - "$rc" "$invocation" "$scenario" "$xml" <<'PY'
import base64,json,os,sys,xml.etree.ElementTree as ET
rc,inv,scen=int(sys.argv[1]),sys.argv[2],sys.argv[3]
try: root=ET.parse(sys.argv[4]).getroot()
except Exception: junit=b""
else:
    [p.remove(c) for p in root.iter() for c in list(p) if c.tag in ("system-out","system-err")]
    for e in root.iter():
        if e.tag in ("failure","error"):
            if e.get("message"): e.set("message", e.get("message")[:4096])
            if e.text: e.text=e.text[:4096]
    junit=ET.tostring(root,encoding="utf-8")
payload={"rc":rc,"junit":base64.b64encode(junit).decode()}
line="\nSCI-RESULT:%s:%s:%s\n"%(inv,scen,base64.b64encode(json.dumps(payload).encode()).decode())
fd=os.open("/dev/console",os.O_WRONLY); os.write(fd,line.encode()); os.close(fd)
PY
if [ "$rc" -ne 0 ]; then printf 'SCI-GUEST-DIAG: begin\n' >/dev/console; tail -n 100 "$log" >/dev/console 2>/dev/null || true; printf 'SCI-GUEST-DIAG: end\n' >/dev/console; fi
exit 0
'''
def die(message): raise SystemExit(f"boot-runner: error: {message}")
def log(message): print(f"boot-runner: {message}", file=sys.stderr, flush=True)
def rm(path):
    if path.is_symlink() or path.is_file(): path.unlink()
    elif path.exists(): shutil.rmtree(path)
def copy_file(src, dst, mode=None):
    dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dst)
    if mode is not None: dst.chmod(mode)
def names(path):
    out, seen = [], set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"): continue
        directive = re.fullmatch(r"([A-Z][A-Z0-9_]*)=\S*", line)
        if directive:
            if directive.group(1) != "TARGET_BASE":
                raise ValueError(f"invalid manifest: unknown directive {directive.group(1)!r} at line {lineno}: {path}")
            continue
        name = line.split()[0]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise ValueError(f"invalid manifest: invalid name {name!r} at line {lineno}: {path}")
        if name in seen:
            raise ValueError(f"invalid manifest: duplicate entry {name!r} at line {lineno}: {path}")
        seen.add(name); out.append(name)
    if not out: raise ValueError(f"invalid manifest: no entries: {path}")
    return out
def identity(base):
    values = {key: value.strip('"') for key, sep, value
              in (line.partition("=") for line in (base / "etc/os-release").read_text().splitlines())
              if sep and not key.startswith("#")}
    return {"id": values["ID"], "image_version": values["IMAGE_VERSION"]}
def signed_shape(raw):
    parts = json.loads(subprocess.run(["sfdisk", "--json", str(raw)], check=True,
                                      capture_output=True, text=True).stdout)["partitiontable"]["partitions"]
    if [p["type"].lower() for p in parts] != list(TYPES):
        die(f"{raw} is not a 3-partition signed sysext: {[p['type'] for p in parts]}")
    return parts
def write_conf(directory, release, base, tools, sandbox, cache, output, extras, kernel=None, probe=None):
    text = (f"[Distribution]\nDistribution=debian\nRelease={release}\n\n[Build]\nToolsTree={tools}\n"
            f"SandboxTrees={sandbox}\nCacheDirectory={cache}\nWithNetwork=no\n\n[Output]\n")
    if probe:
        text += (f"Format=sysext\nOverlay=yes\nOutput=unsignedprobe\nOutputDirectory={output}\n\n"
                 f"[Content]\nBaseTrees={base}\nPackages=\nExtraTrees={probe}:/\n")
    else:
        text += f"Format=disk\nOutput=sysext-vmtest\nOutputDirectory={output}\n\n[Content]\nBaseTrees={base}\nPackages=\n"
        text += "".join(f"ExtraTrees={src}:{target}\n" for src, target in extras)
        text += f"Bootable=no\n\n[Runtime]\nLinux={kernel}\nFirmware=linux\nDiskType=nvme\nKVM=yes\n"
    (directory / "mkosi.conf").write_text(text, encoding="utf-8")
def build_probe(mkosi, directory, release, base, tools, sandbox, cache, output):
    rm(directory)
    release_file = directory / "extra/usr/lib/extension-release.d/extension-release.unsignedprobe"
    release_file.parent.mkdir(parents=True)
    release_file.write_text("ID=gardenlinux\nSYSEXT_SCOPE=system\n", encoding="utf-8")
    write_conf(directory, release, base, tools, sandbox, cache, output, [], probe=directory / "extra")
    spawn([str(mkosi), "-f", "build"], directory, directory / "build.log", 900)
    raw = output / "unsignedprobe.raw"
    if not raw.is_file(): die(f"unsignedprobe build produced no {raw}")
    return raw
def stage_delivery(target, raw_names, model):
    target.mkdir(parents=True, exist_ok=True)
    sums = []
    for name in raw_names:
        source = model["raws"] / f"{name}.raw"
        signed_shape(source)
        copy_file(source, target / f"{name}.raw")
        sums.append(f"{hashlib.sha256(source.read_bytes()).hexdigest()}  {name}.raw")
    (target / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    copy_file(model["manifest"], target / "extensions.manifest")
    for name in raw_names:
        shim = model["shims"] / name / f"mkosi.extra/usr/share/sci/activation/{name}-activation.service"
        if shim.is_file(): copy_file(shim, target / shim.name)
    copy_file(model["agent"], target / "sci-delivery.sh", 0o755)
def prepare(directory, scenario, raw_names, base, model, tools_extra, cert):
    rm(directory)
    overlay = directory / "overlay"
    for rel in ("etc/cloud/cloud-init.disabled", "etc/systemd/system/vm-boot-test.service"):
        copy_file(tools_extra / rel, overlay / rel)
    wants = overlay / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True); os.symlink("../vm-boot-test.service", wants / "vm-boot-test.service")
    os.symlink("/dev/null", overlay / "etc/systemd/system/tgt.service")
    copy_file(cert, overlay / "usr/lib/verity.d/verity.crt")
    invocation = f"sci-boot-{scenario}-{uuid.uuid4().hex}"
    dropin = overlay / "etc/systemd/system/vm-boot-test.service.d/scenario.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_text(f"[Service]\nEnvironment=SCI_SCENARIO={scenario}\nEnvironment=SCI_INVOCATION={invocation}\n", encoding="utf-8")
    wrapper = overlay / "opt/sci-tests/run_guest.sh"
    wrapper.parent.mkdir(parents=True); wrapper.write_text(WRAPPER, encoding="utf-8"); wrapper.chmod(0o755)
    expected = {"scenario": scenario, "extensions": raw_names, "base": base}
    if scenario in VICTIMS: expected["victim"] = VICTIMS[scenario]
    expected_path = overlay / "opt/sci/expected.json"
    expected_path.parent.mkdir(parents=True, exist_ok=True)
    expected_path.write_text(json.dumps(expected) + "\n", encoding="utf-8")
    (overlay / "var/lib/extensions").mkdir(parents=True)
    if scenario == "signed":
        stage_delivery(overlay / "opt/sci", raw_names, model)
    elif scenario == "tamper":
        for name in raw_names:
            signed_shape(model["raws"] / f"{name}.raw")
            copy_file(model["raws"] / f"{name}.raw", overlay / "var/lib/extensions" / f"{name}.raw")
        target = overlay / "var/lib/extensions/sci-20-compute.raw"
        parts = signed_shape(target)
        with target.open("r+b") as handle:
            handle.seek(parts[0]["start"] * 512 + 1024)
            byte = handle.read(1); handle.seek(-1, os.SEEK_CUR); handle.write(bytes([byte[0] ^ 0xFF]))
    elif scenario == "policy":
        copy_file(model["probe"], overlay / "var/lib/extensions/unsignedprobe.raw")
        (overlay / "etc/systemd/sysext.conf").write_text("[SysExt]\nImagePolicy=root=signed\n", encoding="utf-8")
    return overlay, invocation
def spawn(command, cwd, path, timeout):
    with path.open("wb") as output:
        process = subprocess.Popen(command, cwd=str(cwd), stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try: process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait()
            die(f"{' '.join(command)} timed out after {timeout}s; see {path}")
        return subprocess.CompletedProcess(command, process.returncode)
def main(argv):
    if argv[:1] == ["--preflight"]:
        parser = argparse.ArgumentParser()
        parser.add_argument("--manifest", required=True, type=Path)
        args = parser.parse_args(argv[1:])
        try: names(args.manifest)
        except ValueError as exc:
            print(f"boot-runner: error: {exc}", file=sys.stderr)
            return 2
        return 0
    parser = argparse.ArgumentParser()
    for flag, kind in (("--scenario", str), ("--manifest", Path), ("--runtime", Path),
                       ("--repo-root", Path), ("--scratch", Path), ("--dist", Path),
                       ("--base", Path), ("--kernel", Path), ("--tools", Path),
                       ("--sandbox", Path), ("--cert", Path), ("--cache", Path), ("--mkosi", Path)):
        parser.add_argument(flag, required=True, type=kind)
    args = parser.parse_args(argv)
    if args.scenario not in (*MODULES, "all"): die(f"unknown scenario {args.scenario!r}")
    if not args.runtime.is_file(): die(f"missing runtime archive: {args.runtime}")
    repo, manifest = args.repo_root, args.manifest.resolve()
    try: raw_names = names(manifest)
    except ValueError as exc: die(str(exc))
    base = identity(args.base)
    release = re.search(r"^Release=(.*)$", (repo / "mkosi/mkosi.conf").read_text(encoding="utf-8"), re.M).group(1).strip()
    rm(args.dist)
    runtime_adapter.stage_distribution(args.runtime, args.dist, repo, repo / "extensions/tests/guest")
    model = {"manifest": manifest, "raws": repo / ".build/mkosi/output",
             "shims": repo / "mkosi/mkosi.images", "agent": repo / "delivery/sci-delivery.sh", "probe": None}
    results = {}
    for scenario in (MODULES if args.scenario == "all" else (args.scenario,)):
        directory = args.scratch / "scenarios" / scenario
        if scenario == "policy":
            probe_dir = args.scratch / "unsignedprobe"
            model["probe"] = build_probe(args.mkosi, probe_dir, release, args.base, args.tools,
                                         args.sandbox, args.cache, probe_dir / "image")
        overlay, invocation = prepare(directory, scenario, raw_names, base, model,
                                      repo / "mkosi/vmtest/mkosi.extra", args.cert)
        write_conf(directory, release, args.base, args.tools, args.sandbox, args.cache,
                   directory / "image", [(overlay, "/"), (args.dist, "/opt/sci-tests/dist")], args.kernel)
        log(f"{scenario}: building {directory / 'mkosi.conf'}")
        if spawn([str(args.mkosi), "-f", "build"], directory, directory / "build.log", 1800).returncode:
            die(f"{scenario}: image build failed; see {directory / 'build.log'}")
        log(f"{scenario}: booting")
        started = spawn([str(args.mkosi), "qemu"], directory, directory / "console.log", 900)
        text = (directory / "console.log").read_text(encoding="utf-8", errors="replace")
        try:
            counts = runtime_adapter.parse_guest_result(text, invocation, scenario)
        except ValueError as exc:
            log(f"{scenario}: FAIL: {exc}; qemu rc={started.returncode}; see {directory / 'console.log'}")
            results[scenario] = {"rc": 1, "tests": 0, "failures": 0, "errors": 1, "skipped": 0,
                                 "names": [], "reason": str(exc)}
            continue
        if started.returncode:
            die(f"{scenario}: mkosi qemu exited {started.returncode}; see {directory / 'console.log'}")
        (directory / "result.json").write_text(json.dumps(counts) + "\n", encoding="utf-8")
        results[scenario] = counts
        log(f"{scenario}: {counts['tests']} tests ok: {', '.join(counts['names'])}")
    print(json.dumps(results, separators=(",", ":")))
    return 0 if all(entry["rc"] == 0 for entry in results.values()) else 1
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
