"""Contract for the mkosi/boot-test.sh scenario CLI and the three boot proofs.

The runner keeps its no-arg and --manifest behavior and gains
--scenario signed|tamper|policy|all (default all) and
--runtime <archive> (default .build/sysext-runtime/runtime.tar.gz).
An unknown scenario or a missing runtime archive must be refused before
any VM boot, image build, or shared .build state mutation. With
--scenario all the final stdout line is JSON with one entry per scenario:
the runtime-adapter counts and the guest test names.
"""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = "mkosi/boot-test.sh"
RUNTIME = ROOT / ".build/sysext-runtime/runtime.tar.gz"
SCRATCH = ROOT / ".build/test-scratch-vmboot"
REQUIRED = {"signed": "test_delivery_and_host_contract",
            "tamper": "test_verity_refusal", "policy": "test_verity_refusal"}


def run_cli(*args, timeout=5):
    return subprocess.run(["bash", RUNNER, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def snapshot(path: Path) -> dict:
    if not path.exists():
        return {}
    return {str(p.relative_to(path)): (p.stat().st_mtime_ns, p.stat().st_size) for p in path.rglob("*") if p.is_file()}


def refused_clean(args: tuple) -> subprocess.CompletedProcess:
    before = snapshot(SCRATCH)
    result = run_cli(*args)
    assert result.returncode != 0, f"{args} exited 0: {result.stdout}"
    assert snapshot(SCRATCH) == before, "the refusal mutated the shared boot scratch"
    return result


def test_cli_rejects_unknown_scenario():
    result = refused_clean(("--scenario", "bogus"))
    text = result.stdout + result.stderr
    assert "bogus" in text and "scenario" in text.lower(), f"no diagnostic naming the invalid scenario: {text}"


def test_cli_missing_runtime_refused(tmp_path: Path):
    missing = tmp_path / "no-such-runtime.tar.gz"
    result = refused_clean(("--scenario", "signed", "--runtime", str(missing)))
    text = result.stdout + result.stderr
    assert str(missing) in text, f"the refusal does not name the missing archive: {text}"
    assert "missing mkosi venv" not in text, f"the runtime check must precede the mkosi-venv check: {text}"


def _counts(entry):
    assert isinstance(entry, dict), f"scenario entry is not an object: {entry!r}"
    counts = entry.get("counts", entry)
    assert isinstance(counts, dict) and "tests" in counts, f"scenario entry lacks the counts: {entry!r}"
    return counts


@pytest.mark.vm
@pytest.mark.build_state_mutating
def test_three_boot_contract():
    assert RUNTIME.is_file(), f"runtime archive missing: {RUNTIME}"
    result = run_cli("--scenario", "all", "--runtime", str(RUNTIME), timeout=1800)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, "the runner printed no stdout"
    data = json.loads(lines[-1])
    assert set(data) == set(REQUIRED), f"the final JSON line carries the wrong scenarios: {sorted(data)}"
    for scenario, name in REQUIRED.items():
        assert name in data[scenario]["names"], f"{scenario}: guest test {name} is absent: {data[scenario]}"
        counts = _counts(data[scenario])
        assert int(counts["tests"]) > 0, f"{scenario}: no guest tests ran: {counts}"
        assert int(counts.get("failures", 0)) + int(counts.get("errors", 0)) == 0, f"{scenario}: guest failures: {counts}"
        assert int(counts.get("skipped", 0)) == 0, f"{scenario}: guest tests skipped: {counts}"


# boot_runner.names must refuse hostile entries before any scratch mutation.
def _manifest(tmp_path: Path, text: str):
    import boot_runner
    manifest = tmp_path / "extensions.manifest"
    manifest.write_text(text, encoding="utf-8")
    return boot_runner, manifest


@pytest.mark.parametrize("text", [
    pytest.param("../outside", id="traversal"),
    pytest.param("/abs/outside", id="absolute"),
    pytest.param("sci-20-compute\nsci-20-compute", id="duplicate"),
    pytest.param("# only a comment", id="empty"),
])
def test_names_rejects_hostile_entries(tmp_path: Path, text):
    boot_runner, manifest = _manifest(tmp_path, text + "\n")
    before = snapshot(SCRATCH)
    with pytest.raises(ValueError):
        boot_runner.names(manifest)
    assert snapshot(SCRATCH) == before, "names() touched the shared boot scratch"


def test_names_accepts_plain_entries(tmp_path: Path):
    boot_runner, manifest = _manifest(tmp_path, "sci-20-compute\nunsignedprobe\n")
    assert boot_runner.names(manifest) == ["sci-20-compute", "unsignedprobe"]


# The open getty prompt has no trailing newline; the record must start its own line.
def test_wrapper_result_record_starts_its_own_line(tmp_path: Path):
    import ast, sys
    import boot_runner, runtime_adapter
    code = boot_runner.WRAPPER.split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
    ast.parse(code)
    assert "SCI-RESULT:" in code and "/dev/console" in code
    (tmp_path / "junit.xml").write_text("<testsuites><testsuite tests='1'>"
                                       "<testcase name='test_verity_refusal'/></testsuite></testsuites>")
    (tmp_path / "emitter.py").write_text(code + "\n")
    console = tmp_path / "console"
    console.write_bytes(b"localhost login: ")
    driver = tmp_path / "driver.py"
    driver.write_text("import os,sys\nconsole,xml,emitter=sys.argv[1:4]\n"
                      "fd=os.open(console,os.O_WRONLY|os.O_APPEND);real=os.open\n"
                      "os.open=lambda p,*a,**k: fd if p=='/dev/console' else real(p,*a,**k)\n"
                      "sys.argv=[emitter,'0','sci-boot-tamper-f1','tamper',xml]\n"
                      "exec(compile(open(emitter).read(),'emitter','exec'),{'__name__':'__main__'})\n")
    result = subprocess.run([sys.executable, str(driver), str(console), str(tmp_path / "junit.xml"),
                             str(tmp_path / "emitter.py")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    text = console.read_bytes().decode()
    assert text[: text.index("SCI-RESULT:")].endswith("\n"), f"record glued to the prompt: {text[-60:]!r}"
    counts = runtime_adapter.parse_guest_result(text, "sci-boot-tamper-f1", "tamper")
    assert counts["tests"] == 1 and counts["names"] == ["test_verity_refusal"]
