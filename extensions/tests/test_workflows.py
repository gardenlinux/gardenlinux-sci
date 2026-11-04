"""CI wiring pins: the composite test actions and the sysext/test workflows."""
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def load(path):
    """Parsed workflow or composite-action YAML; the only YAML accessor."""
    assert path.is_file(), f"{path.name} is missing: the file under test does not exist"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def triggers(data):
    """The 'on:' section; PyYAML resolves the bare top-level key 'on' to True."""
    return data["on"] if "on" in data else data[True]


def job(data, name):
    """The named job mapping of a parsed workflow."""
    jobs = data.get("jobs") or {}
    assert name in jobs, f"the workflow has no '{name}' job"
    assert isinstance(jobs[name], dict), f"the '{name}' job is not a mapping"
    return jobs[name]


def steps_of(section):
    """The 'steps:' list of a job or of a parsed composite action's 'runs' block."""
    steps = section.get("steps") or []
    assert isinstance(steps, list), "the 'steps:' section is not a list"
    return steps


def step_named(section, name, *, contains=False):
    """The one step named exactly `name`, or the one step containing it when `contains` is true."""
    def matches(s):
        actual = str(s.get("name", ""))
        return name in actual if contains else actual == name
    found = [s for s in steps_of(section) if matches(s)]
    assert len(found) == 1, f"exactly one step named '{name}' expected, found {len(found)}"
    return found[0]


def run_text(step):
    """The step's 'run:' script as written, without YAML indentation."""
    return step.get("run") or ""


def run_lines(step):
    """Stripped, non-empty, non-comment lines of the step's 'run:' script."""
    return [l.strip() for l in run_text(step).splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def with_values(step):
    """The step's 'with:' mapping, or an empty mapping."""
    return step.get("with") or {}


def step_index(steps, pred):
    """Index of the first step whose run block has a line for which `pred` is true."""
    return next((i for i, s in enumerate(steps) if any(pred(l) for l in run_lines(s))), -1)


ACTION = REPO_ROOT / ".github" / "actions" / "test" / "integration" / "setup" / "action.yml"
STEP = "Boot the sysext HyperVisors and run the delivery container"


def test_setup_grants_qemu_traversal_through_the_runner_home():
    lines = run_text(step_named(load(ACTION)["runs"], STEP)).splitlines()
    starts = [i for i, l in enumerate(lines) if "virsh start" in l]
    assert starts, "the sysext setup step no longer starts the HV domains"
    chmods = [i for i, l in enumerate(lines) if "chmod o+x" in l and "$HOME" in l]
    assert chmods and chmods[0] < starts[0], (
        "the sysext setup step must 'sudo chmod o+x $HOME' before the first 'virsh start'")


def guest_script():
    """Text of the guest script the sysext step pipes into 'bash -s' via ssh."""
    lines = run_text(step_named(load(ACTION)["runs"], STEP)).splitlines()
    a = next((i for i, l in enumerate(lines) if l.endswith("bash -s <<'EOF'")), None)
    assert a is not None, "the sysext step no longer pipes the guest script via \"bash -s <<'EOF'\""
    b = next((i for i in range(a + 1, len(lines)) if lines[i] == "EOF"), None)
    assert b is not None, "the closing 'EOF' of the sysext heredoc is missing"
    return "\n".join(lines[a + 1:b])


def test_sysext_setup_heredoc_runs_post_delivery_commands(tmp_path):
    script = guest_script()
    run_lines_ = [l for l in script.splitlines() if re.match(r"\s*ctr .* run\b", l)]
    assert len(run_lines_) == 1, (
        f"the sysext step must start exactly one 'ctr ... run' container, found {len(run_lines_)}")
    sb = tmp_path / "sb"
    (sb / "root" / ".ssh").mkdir(parents=True)
    # Only the absolute paths the script writes to are relocated; stdin semantics untouched.
    for p in ("/root/", "/opt/", "/var/log/libvirt/", "/var/lib/libvirt/"):
        script = script.replace(p, str(sb) + p)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    # 'ctr run' (foreground) reads stdin; 'ctr images import <file>' does not. 'systemd-sysext' is stubbed.
    stub = {
        "head": "#!/bin/sh\necho registry.local/sci/delivery:ci-test\n",
        "systemctl": "#!/bin/sh\nexit 0\n",
        "systemd-sysext": "#!/bin/sh\nexit 0\n",
        "chmod": "#!/bin/sh\nexit 0\n",
        "virsh": f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {sb / 'virsh.log'}\nexit 0\n",
        "ctr": (f"#!/bin/sh\nprintf '%s\\n' \"$3\" >> {sb / 'ctr.log'}\n"
                f"[ \"$3\" = run ] && cat > /dev/null\nexit 0\n"),
    }
    for name, text in stub.items():
        (stubs / name).write_text(text, encoding="utf-8")
        (stubs / name).chmod(0o755)
    env = {"PATH": f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
    res = subprocess.run(["bash", "-s"], input=script, text=True, capture_output=True,
                         cwd=str(sb), env=env, timeout=60)
    assert res.returncode == 0, f"the guest script exited {res.returncode}: {res.stderr.strip()[:120]}"
    ctr = (sb / "ctr.log").read_text(encoding="utf-8").split()
    assert ctr, "the 'ctr' stub was never invoked: the test cannot exercise the defect"
    assert "run" in ctr, "the 'ctr ... run' delivery invocation never executed"
    log = (sb / "virsh.log").read_text(encoding="utf-8") if (sb / "virsh.log").is_file() else ""
    missing = [c for c in ("pool-define-as default", "pool-start default", "net-define",
                           "net-start default", "net-autostart default") if c not in log]
    assert not missing, f"post-delivery commands were skipped; missing virsh calls: {missing}"
    assert (sb / "root" / ".ssh" / "config").is_file(), (
        "/root/.ssh/config was never written: the lines after 'ctr ... run' were consumed as container stdin")


QEMU_ACTION = REPO_ROOT / ".github" / "actions" / "test" / "integration" / "test" / "qemu" / "action.yml"
QEMU_STEP = "Copy VM defintions and artifacts to the HyperVisors"
QEMU_DISK_DESTS = {
    "ubuntu.qcow2": "/var/lib/libvirt/images/ubuntu.qcow2",
    "ubuntu-cloud-init-ds.iso": "/var/lib/libvirt/images/ubuntu-cloud-init-ds.iso",
}


def action_run_block(action, step):
    """The step's 'run:' script from the composite action `action`."""
    block = run_text(step_named(load(action)["runs"], step))
    assert block.strip(), f"the '{step}' run block is empty in {action.name}"
    return block


def qemu_copy_block():
    """Text of the 'Copy VM defintions...' run block of the QEMU test action."""
    return action_run_block(QEMU_ACTION, QEMU_STEP)


def scp_stages(run, image, hv, dest):
    """scp lines of the run block that copy `image` to the `dest` path on `hv`."""
    return [l for l in run.splitlines()
            if l.strip().startswith("scp") and image in l and hv in l and dest in l]


def test_qemu_migration_stages_disk_on_both_hvs():
    section = load(QEMU_ACTION)["runs"]
    steps = steps_of(section)
    copy_i = steps.index(step_named(section, QEMU_STEP))
    # Substring on purpose: the step name itself carries the upstream typo "Mirgrate".
    migrate_i = steps.index(step_named(section, "Mirgrate", contains=True))
    assert copy_i < migrate_i, (
        "the 'Copy VM defintions' step must occur before the migration step: the disk must exist at the destination")
    run = qemu_copy_block()
    for image, dest in QEMU_DISK_DESTS.items():
        missing = [hv for hv in ("$IP_HV1", "$IP_HV2") if not scp_stages(run, image, hv, dest)]
        assert not missing, (
            f"the QEMU copy step must stage {image} at {dest} on both HVs; missing on: {', '.join(missing)}")


CHV_ACTION = REPO_ROOT / ".github" / "actions" / "test" / "integration" / "test" / "cloudhypervisor" / "action.yml"
CHV_VM_XML = REPO_ROOT / ".github" / "actions" / "test" / "integration" / "test" / "cloudhypervisor" / "vm.xml"
MIGRATION_ACTIONS = [(QEMU_ACTION, "QEMU"), (CHV_ACTION, "cloudhypervisor")]
WAIT_STEP = "Wait for VM to be ready"
VERIFY_STEP = "Verify VM status on HyperVisor 2"


def loop_window(run, step, action):
    """MAX_ITER and sleep seconds of the SSH retry loop in the step run block."""
    # Last match: a decoy earlier in the block must not shadow the loop's values.
    m_iter = re.findall(r"MAX_ITER=(\d+)", run)
    m_sleep = re.findall(r"sleep (\d+)", run)
    assert m_iter and m_sleep, (
        f"cannot find 'MAX_ITER=<n>' and 'sleep <n>' in the '{step}' run block: no plain retry loop")
    return int(m_iter[-1]), int(m_sleep[-1])


def test_chv_vm_defines_at_least_two_vcpus():
    doc = ET.parse(CHV_VM_XML).getroot()
    vcpu = doc.find("vcpu")
    assert vcpu is not None, f"{CHV_VM_XML.name}: no <vcpu> element: cannot check the vCPU count"
    count = int(vcpu.text.strip())
    assert count >= 2, (
        f"{CHV_VM_XML.name}: <vcpu> is {count}: the guest needs at least 2 vCPUs to boot to SSH")


def test_wait_for_vm_step_allows_at_least_60_minutes():
    for action, label in MIGRATION_ACTIONS:
        run = action_run_block(action, WAIT_STEP)
        iters, sleep_s = loop_window(run, WAIT_STEP, action)
        assert iters * sleep_s >= 3600, (
            f"{label}: '{WAIT_STEP}' waits {iters * sleep_s} s (MAX_ITER={iters} x sleep {sleep_s}), "
            "below the 3600 s window")


def test_verify_vm_step_allows_at_least_15_minutes():
    for action, label in MIGRATION_ACTIONS:
        run = action_run_block(action, VERIFY_STEP)
        iters, sleep_s = loop_window(run, VERIFY_STEP, action)
        assert iters * sleep_s >= 900, (
            f"{label}: '{VERIFY_STEP}' waits {iters * sleep_s} s (MAX_ITER={iters} x sleep {sleep_s}), "
            "below the 900 s window")


SYSEXT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "sysext.yml"
ACTRC = REPO_ROOT / ".actrc"
CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
QUICK_MARKERS = "-m 'not vm and not build'"
QUICK_COMMAND = ".build/venv-test/bin/python -m pytest extensions/tests/ -q " + QUICK_MARKERS


def assert_quick_suite_contract(data):
    """Quick-tier contract of the sysext workflow, on the parsed YAML."""
    on = triggers(data)
    assert isinstance(on, dict), (
        "the sysext workflow has no 'on:' trigger section: push and pull_request must self-trigger")
    push = on.get("push")
    assert isinstance(push, dict), (
        "the push trigger is missing: the branch must self-trigger before it lands on the default branch")
    branches = push.get("branches")
    assert set(branches or []) == {"sysext-virt", "sysext-*"}, (
        f"the push trigger must fire only on branches sysext-virt and sysext-* (found: {branches})")
    assert "pull_request" in on, (
        "the pull_request trigger is missing: PRs must also run the quick tier")

    steps = steps_of(job(data, "quick-suite"))
    code = [l for s in steps for l in run_lines(s)]
    assert any("python3 -m venv .build/venv-test" in l for l in code), (
        "the quick-suite job must create its test venv: 'python3 -m venv .build/venv-test'")
    assert any(l == QUICK_COMMAND for l in code), (
        f"the quick-suite job must run exactly {QUICK_COMMAND!r} with the quick markers")
    assert any(".build/venv-test/bin/pip" in l and "pytest==8.4.2" in l for l in code), (
        "the quick-suite job must pin pytest==8.4.2 in its pip line")


def test_sysext_workflow_runs_quick_suite_on_github():
    assert_quick_suite_contract(load(SYSEXT_WORKFLOW))


def test_actrc_stays_machine_local():
    res = subprocess.run(["git", "ls-files", ".actrc"], capture_output=True, text=True,
                         cwd=str(REPO_ROOT))
    assert res.returncode == 0, (
        f"'git ls-files .actrc' failed with exit {res.returncode}: {res.stderr.strip()[:100]}")
    assert not res.stdout.strip(), (
        f"{ACTRC} is tracked: .actrc is workstation-local config and must not be committed")
    res = subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", "check-ignore", ".actrc"],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert res.returncode == 0, (
        "the repo .gitignore must list .actrc so the local file cannot be committed accidentally")


UPLOAD_SHA = "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9"
BUILDER_IMAGE = "ghcr.io/gardenlinux/builder:f7cf91087f7580c6c3aefb26af6cee7df89a9938"
FULL_SUITE_INVOCATION = ".build/venv-test/bin/python -m pytest extensions/tests/ -q"
MKOSI_SPEC = "mkosi @ git+https://github.com/systemd/mkosi.git@v27.1"


def test_producer_job_shape():
    producer = job(load(SYSEXT_WORKFLOW), "producer")
    assert producer.get("runs-on") == "ubuntu-24.04", (
        f"the producer job must keep 'runs-on: ubuntu-24.04' (found: {producer.get('runs-on')!r})")
    needs = producer.get("needs")
    assert needs in ("quick-suite", ["quick-suite"]), (
        f"the producer job must declare exactly 'needs: quick-suite' (found: {needs!r})")
    assert producer.get("if") == "github.event_name != 'pull_request'", (
        f"the producer job must set \"if: github.event_name != 'pull_request'\" (found: {producer.get('if')!r})")
    assert steps_of(producer), "the producer job has no 'steps:' section: nothing would build"


def test_producer_pins_its_checkout_and_toolchain():
    steps = steps_of(job(load(SYSEXT_WORKFLOW), "producer"))
    checkout = [s for s in steps if s.get("uses") == f"actions/checkout@{CHECKOUT_SHA}"]
    assert len(checkout) == 1, (
        f"exactly one producer step must pin 'uses: actions/checkout@{CHECKOUT_SHA}'; found {len(checkout)}")
    submodules = with_values(checkout[0]).get("submodules")
    assert submodules in ("recursive", True), (
        f"the producer's checkout step must enable submodules (found: {submodules!r})")
    assert any(MKOSI_SPEC in l for s in steps for l in run_lines(s)), (
        f"no producer step installs the exact spec '{MKOSI_SPEC}'")


def test_producer_steps_run_in_a_safe_order():
    steps = steps_of(job(load(SYSEXT_WORKFLOW), "producer"))
    ensure_i = step_index(steps, lambda l: "bash mkosi/base-image.sh ensure" in l)
    build_i = step_index(steps, lambda l: "bash mkosi/build.sh" in l)
    delivery_i = step_index(steps, lambda l: "bash delivery/build-image.sh" in l)
    userns_i = step_index(steps, lambda l: "apparmor_restrict_unprivileged_userns" in l)
    assert ensure_i >= 0, (
        "no producer step runs 'bash mkosi/base-image.sh ensure': build.sh demands the base tree")
    assert build_i >= 0, "no producer step runs 'bash mkosi/build.sh'"
    assert userns_i >= 0, "no producer step sets kernel.apparmor_restrict_unprivileged_userns=0"
    assert userns_i < build_i, "the user-namespace relaxation must run before the mkosi build"
    assert any("/dev/kvm" in l and "chmod" in l
               for s in steps for l in run_lines(s)), (
        "no producer step makes /dev/kvm readable and writable by the runner user")
    assert delivery_i >= 0, (
        "no producer step runs 'bash delivery/build-image.sh': nothing would stage the raws artifact")
    assert ensure_i < build_i, "the base-image ensure step must occur before the mkosi build"
    assert build_i < delivery_i, "the mkosi build step must occur before the raws staging"
    assert not any("MANIFEST=" in l for l in run_lines(steps[delivery_i])), (
        "the delivery build step must not override MANIFEST: the repo manifest lists every deployable")
    assert any(f"docker pull {BUILDER_IMAGE}" in l
               for s in steps for l in run_lines(s)), (
        f"no producer step runs 'docker pull {BUILDER_IMAGE}': the build tiers pin that image")


def test_producer_signs_with_a_run_local_ca():
    steps = steps_of(job(load(SYSEXT_WORKFLOW), "producer"))
    ca_i = step_index(steps, lambda l: "openssl req -x509" in l)
    assert ca_i >= 0, (
        "no producer step runs 'openssl req -x509': the producer must generate its own signing CA per run")
    ca_run = run_lines(steps[ca_i])
    assert (any(".build/keys/sysext-verity.key" in l for l in ca_run)
            and any("mkosi/verity.crt" in l for l in ca_run)
            and any("chmod 600 .build/keys/sysext-verity.key" in l for l in ca_run)
            and any("install -d -m 0700 .build/keys" in l for l in ca_run)), (
        "the run-local CA step must write '.build/keys/sysext-verity.key' and 'mkosi/verity.crt', "
        "with modes 0700/600")
    build_i = step_index(steps, lambda l: "bash mkosi/build.sh" in l)
    assert build_i >= 0, "no producer step runs 'bash mkosi/build.sh'"
    assert ca_i < build_i, (
        "the run-local CA step must occur before the mkosi build: build.sh signs the raws with the run key")


def test_producer_runs_the_full_suite_before_uploading():
    steps = steps_of(job(load(SYSEXT_WORKFLOW), "producer"))
    suite_i = step_index(steps, lambda l: l == FULL_SUITE_INVOCATION)
    assert suite_i >= 0, (
        f"no producer step runs exactly '{FULL_SUITE_INVOCATION}': the full suite must run in this job")
    for l in run_lines(steps[suite_i]):
        rest = l.replace(FULL_SUITE_INVOCATION, " ")
        assert not re.search(r"(\A|\s)-m(\s|$)", rest), (
            f"the full-suite step must not select markers with '-m' (found {l!r})")
    uploads = [i for i, s in enumerate(steps) if s.get("uses") == UPLOAD_SHA]
    assert len(uploads) == 1, (
        f"the producer job must have exactly one upload step pinned at 'uses: {UPLOAD_SHA}'; found {len(uploads)}")
    assert suite_i < uploads[0], (
        "the full-suite step must run before the first upload: nothing is uploaded before it proves the build")
    withs = [with_values(steps[i]) for i in uploads]
    names = {x.get("name") for x in withs}
    assert names == {"sysext-raws"}, (
        f"the upload step must be named exactly 'sysext-raws'; found: {sorted(str(n) for n in names)}")
    for x in withs:
        assert x.get("if-no-files-found") == "error", (
            f"every upload step must set 'if-no-files-found: error' (found: {x.get('if-no-files-found')!r})")


def test_integration_invokes_the_consumer():
    data = load(SYSEXT_WORKFLOW)
    uploads = [with_values(s) for s in steps_of(job(data, "producer"))
               if s.get("uses") == UPLOAD_SHA]
    assert len(uploads) == 1, (
        f"the producer job must have exactly one upload step pinned at 'uses: {UPLOAD_SHA}'; found {len(uploads)}")
    delivery_name = uploads[0].get("name")
    integ = job(data, "integration")
    needs = integ.get("needs")
    assert needs in ("producer", ["producer"]), (
        f"the integration job must declare 'needs: producer' (found: {needs!r})")
    assert integ.get("uses") == "./.github/workflows/test.yml", (
        f"the integration job must call 'uses: ./.github/workflows/test.yml' (found: {integ.get('uses')!r})")
    with_ = with_values(integ)
    assert with_.get("sysext_artifact") == delivery_name, (
        f"the integration job must pass 'sysext_artifact: {delivery_name}'; found: {with_.get('sysext_artifact')!r}")
    assert "tools_artifact" not in with_, (
        "the integration job must not pass 'tools_artifact': the toolchain is bootstrapped on the consumer")
    assert "!= 'pull_request'" in str(integ.get("if", "")), (
        "the integration job must set an 'if:' that excludes pull_request: the producer does not build on PRs")
    perms = integ.get("permissions")
    assert perms == {"contents": "read", "actions": "read"}, (
        f"the integration job must grant exactly 'contents: read' and 'actions: read' (found: {perms!r})")


RAWS_STAGING = ".build/delivery/staging-raws"
RAWS_ARTIFACT = "sysext-raws"


def test_sysext_workflow_publishes_raws_artifact():
    steps = steps_of(job(load(SYSEXT_WORKFLOW), "producer"))
    stage_i = step_index(steps, lambda l: "bash delivery/build-image.sh --stage-only" in l)
    assert stage_i >= 0, (
        "no producer step runs 'bash delivery/build-image.sh --stage-only': the raws artifact needs the staging")
    run = run_lines(steps[stage_i])
    assert any(f"STAGING_OUT={RAWS_STAGING}" in l for l in run), (
        f"the raws staging step must set 'STAGING_OUT={RAWS_STAGING}': the upload publishes that directory")
    assert any(l.startswith("cp ")
               and "mkosi/verity.crt" in l and "verity-run.crt" in l
               for l in run), (
        "the raws staging step must copy the run cert into the staging directory as 'verity-run.crt'")
    suite_i = step_index(steps, lambda l: l == FULL_SUITE_INVOCATION)
    assert suite_i >= 0 and suite_i < stage_i, (
        "the raws staging step must run after the full suite: nothing ships before the suite proves it")
    raws_uploads = [s for s in steps if s.get("uses") == UPLOAD_SHA
                    and with_values(s).get("name") == RAWS_ARTIFACT]
    assert len(raws_uploads) == 1, (
        f"exactly one upload step must publish the '{RAWS_ARTIFACT}' artifact (found {len(raws_uploads)})")
    with_ = with_values(raws_uploads[0])
    assert str(with_.get("path", "")).rstrip("/") == RAWS_STAGING, (
        f"the {RAWS_ARTIFACT} upload must point at '{RAWS_STAGING}/' (found: {with_.get('path')!r})")
    assert with_.get("if-no-files-found") == "error", (
        "the raws upload must set 'if-no-files-found: error': a missing file must fail the job")
    assert str(with_.get("retention-days")) == "7", (
        "the raws upload must keep the 7-day retention of the sibling artifacts")


SIGNING_SECRET = "secrets.SYSEXT_UNHARDENED_SIGNING_KEY"


def test_producer_uses_the_stable_signing_key_when_present():
    steps = steps_of(job(load(SYSEXT_WORKFLOW), "producer"))

    def step_text(step):
        env = step.get("env") or {}
        return run_text(step) + "\n" + "\n".join(str(v) for v in env.values())

    assert any(SIGNING_SECRET in step_text(s) for s in steps), (
        f"no producer step's env or run references {SIGNING_SECRET}: the stable signing key is never used")
    ca = [s for s in steps if "openssl req -x509" in run_text(s)]
    assert ca and any("-newkey" in run_text(s) for s in ca), (
        "the run-local fallback ('openssl req -x509' with '-newkey') is gone: the run would break when the secret is absent")


def test_publish_job_publishes_a_signed_release():
    data = load(SYSEXT_WORKFLOW)
    publish = job(data, "publish")
    needs = publish.get("needs")
    assert needs in ("producer", ["producer"]), (
        f"the publish job must declare exactly 'needs: producer' (found: {needs!r})")
    assert "pull_request" in str(publish.get("if", "")), (
        f"the publish job must not run on pull requests: its 'if:' must mention pull_request (found: {publish.get('if')!r})")
    perms = publish.get("permissions")
    assert perms == {"contents": "write"}, (
        f"the publish job must grant exactly 'contents: write' (found: {perms!r})")
    steps = steps_of(publish)
    downloads = [s for s in steps if with_values(s).get("name") == "sysext-raws"]
    assert len(downloads) == 1, (
        f"exactly one publish step must download the 'sysext-raws' artifact with with.name (found {len(downloads)})")
    sign_i = step_index(steps, lambda l: "openssl dgst -sha256 -sign" in l)
    assert sign_i >= 0, (
        "no publish step signs with 'openssl dgst -sha256 -sign': the release would ship an unverified SHA256SUMS")
    assert any("SHA256SUMS.sig" in l for l in run_lines(steps[sign_i])), (
        "the signing step must produce 'SHA256SUMS.sig'")
    release_i = step_index(steps, lambda l: "gh release" in l)
    assert release_i >= 0, "no publish step runs 'gh release': nothing creates or updates the GitHub release"
    release_run = "\n".join(run_lines(steps[release_i]))
    assert "GITHUB_SHA" in release_run, (
        "the release step must build the tag from the release version and the short commit and reference GITHUB_SHA")
    uploads = [s for s in steps if any("gh release upload" in l for l in run_lines(s))]
    assert len(uploads) == 1, (
        f"exactly one publish step must run 'gh release upload' (found {len(uploads)})")
    up = "\n".join(run_lines(uploads[0]))
    assert "SHA256SUMS.sig" in up, (
        "the upload command must include 'SHA256SUMS.sig': the consumer verifies the download against it")
    assert "*" in up or ".raw" in up, (
        "the upload command must include the staged files: a wildcard or the staged file names")
    vehicle = ".build/ci-sysext-hv/publish/sci-delivery.sh"
    step = step_named(publish, "Regenerate the published inventory")
    run = run_lines(step)
    workdir = str(step.get("working-directory") or ".")

    def repo_target(arg):
        """Repo-relative target of an rm path argument."""
        if "$GITHUB_WORKSPACE" in arg or "${GITHUB_WORKSPACE}" in arg:
            tail = arg.replace("${GITHUB_WORKSPACE}", "").replace("$GITHUB_WORKSPACE", "")
            return os.path.normpath(tail.lstrip("/"))
        return os.path.normpath(os.path.join(workdir, arg))

    removes = [i for i, l in enumerate(run) if re.match(r"rm\s+", l) and "sci-delivery.sh" in l]
    assert removes, (
        f"the inventory step must 'rm' the delivery step: the release carries data, not the vehicle")
    paths = [a for a in run[removes[0]].split()[1:] if not a.startswith("-")]
    assert os.path.normpath(vehicle) in {repo_target(a) for a in paths}, (
        f"the rm path must resolve to {vehicle} from working-directory {workdir}; found {paths}")
    assert any(re.search(r"test\s+!\s+-e\s+sci-delivery\.sh", l) for l in run), (
        "the inventory step must guard 'test ! -e sci-delivery.sh': a surviving vehicle must fail the job")
    find_i = next((i for i, l in enumerate(run) if l.startswith("find ") and "-printf" in l), -1)
    sums_i = next((i for i, l in enumerate(run)
                   if "xargs sha256sum" in l and "SHA256SUMS" in l), -1)
    assert find_i >= 0 and sums_i > find_i, (
        "the inventory step no longer regenerates 'SHA256SUMS' with a 'find ... | xargs sha256sum' pipeline")
    assert removes[0] < find_i, (
        "the delivery step must be removed before the inventory regenerates: SHA256SUMS must not list it")


def test_publish_targets_the_built_commit():
    steps = steps_of(job(load(SYSEXT_WORKFLOW), "publish"))
    release_i = step_index(steps, lambda l: "gh release" in l)
    assert release_i >= 0, "no publish step runs 'gh release': nothing creates the GitHub release"
    creates = [l for l in run_lines(steps[release_i]) if "gh release create" in l]
    assert creates, "no publish step runs 'gh release create': the release tag is never created"
    create = "\n".join(creates)
    assert "--target" in create, (
        "the 'gh release create' call must pass '--target': without it the git tag lands on the "
        f"default branch instead of the built commit: {creates}")
    assert re.search(r"--target\s+\"?\$\{?GITHUB_SHA", create), (
        "the '--target' of 'gh release create' must reference GITHUB_SHA (the built commit): "
        f"{creates}")


def test_publish_is_skipped_without_the_signing_secret():
    publish = job(load(SYSEXT_WORKFLOW), "publish")
    steps = steps_of(publish)
    job_text = run_text(publish) + "\n" + str(publish.get("if", ""))
    step_text = "\n".join(
        run_text(s) + "\n" + str(s.get("if", "")) + "\n" + "\n".join(str(v) for v in (s.get("env") or {}).values())
        for s in steps)
    assert "SYSEXT_UNHARDENED_SIGNING_KEY" in job_text or "SYSEXT_UNHARDENED_SIGNING_KEY" in step_text, (
        "the publish job's run and if text never mention SYSEXT_UNHARDENED_SIGNING_KEY: a missing secret cannot be detected")
    if_text = "\n".join([str(publish.get("if", ""))] + [str(s.get("if", "")) for s in steps])
    assert re.search(r"==\s*['\"]?true", if_text), (
        "no publish job or step 'if' compares an output to the string 'true': a missing secret would fail the job instead of skipping it")


TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
RUN_CERT = ".build/ci-sysext-hv/raws/verity-run.crt"
CHECKOUT_CERT = "mkosi/verity.crt"


def test_sysext_workflow_invokes_the_consumer():
    data = load(SYSEXT_WORKFLOW)
    job_ids = list(data.get("jobs") or {})
    assert job_ids == ["quick-suite", "producer", "integration", "publish"], (
        f"the job id list must be exactly ['quick-suite', 'producer', 'integration', 'publish'] in that order; found: {job_ids}")
    integ = job(data, "integration")
    assert integ.get("uses") == "./.github/workflows/test.yml", (
        "the integration job must set 'uses: ./.github/workflows/test.yml'")
    needs = integ.get("needs")
    assert needs in ("producer", ["producer"]), (
        f"the integration job must declare 'needs: producer' (found: {needs!r})")
    assert "!= 'pull_request'" in str(integ.get("if", "")), (
        "the integration job must set an 'if:' that excludes pull_request")
    vals = with_values(integ)
    assert vals.get("sysext_artifact") == "sysext-raws", (
        f"the integration job must pass 'sysext_artifact: sysext-raws' (found: {vals.get('sysext_artifact')!r})")
    assert "tools_artifact" not in vals, (
        "the integration job must not pass 'tools_artifact': the toolchain is bootstrapped on the consumer")
    perms = integ.get("permissions") or {}
    grants = sorted(f"{k}: {v}" for k, v in perms.items())
    assert perms == {"contents": "read", "actions": "read"}, (
        f"the integration job must grant exactly 'actions: read' and 'contents: read' (found: {grants})")


def test_consumer_restores_the_run_certificate():
    section = job(load(TEST_WORKFLOW), "test")
    steps = steps_of(section)
    restorers = [i for i, s in enumerate(steps)
                 if RUN_CERT in run_text(s) and CHECKOUT_CERT in run_text(s)]
    assert len(restorers) == 1, (
        f"exactly one step must copy '{RUN_CERT}' over '{CHECKOUT_CERT}'; found {len(restorers)}")
    at = restorers[0]
    step = steps[at]
    assert "inputs.sysext_artifact" in str(step.get("if", "")), (
        "the cert-restore step must be gated on inputs.sysext_artifact")
    run = run_lines(step)
    copies = [l for l in run
              if re.match(r"cp(\s|$)", l) and RUN_CERT in l and CHECKOUT_CERT in l
              and l.index(RUN_CERT) < l.index(CHECKOUT_CERT)]
    assert copies, (
        f"the restore step must copy '{RUN_CERT}' onto '{CHECKOUT_CERT}' in that direction")
    else_at = next((i for i, l in enumerate(run) if l == "else"), -1)
    echo = [l for l in run[else_at + 1:] if l.startswith("echo")]
    assert else_at >= 0 and echo, "the restore step must keep a tolerant 'else' branch with an echo"
    download_i = steps.index(step_named(section, "Download the sysext raws artifact"))
    preflight_i = steps.index(step_named(section, "Preflight the accepted toolchain"))
    assert download_i < at, (
        "the cert-restore step must appear after 'Download the sysext raws artifact'")
    assert at < preflight_i, (
        "the cert-restore step must appear before 'Preflight the accepted toolchain'")


def test_consumer_relaxes_the_userns_restriction():
    section = job(load(TEST_WORKFLOW), "test")
    steps = steps_of(section)
    step = step_named(section, "Relax the runner's user-namespace restriction")
    at = steps.index(step)
    assert any("apparmor_restrict_unprivileged_userns=0" in l for l in run_lines(step)), (
        "the consumer's relaxation must set kernel.apparmor_restrict_unprivileged_userns=0 for mkosi")
    assert "inputs.sysext_artifact" in str(step.get("if", "")), (
        "the relaxation step must be gated on inputs.sysext_artifact")
    prepare_i = steps.index(step_named(section, "Prepare the sysext HyperVisor base"))
    bootstrap_i = steps.index(step_named(section, "Bootstrap the toolchain"))
    assert at < bootstrap_i, "the relaxation must run before 'Bootstrap the toolchain'"
    assert at < prepare_i, "the relaxation must run before 'Prepare the sysext HyperVisor base'"


def test_consumer_bootstraps_the_toolchain():
    section = job(load(TEST_WORKFLOW), "test")
    steps = steps_of(section)
    step = step_named(section, "Bootstrap the toolchain")
    at = steps.index(step)
    run = run_lines(step)
    assert any(l == "bash mkosi/build.sh --bootstrap" for l in run), (
        "the bootstrap step must run exactly 'bash mkosi/build.sh --bootstrap'")
    assert any("python3 -m venv .build/mkosi/venv" in l for l in run), (
        "the bootstrap step must create the mkosi venv: 'python3 -m venv .build/mkosi/venv'")
    assert any("mkosi.git@v27.1" in l for l in run), (
        "the bootstrap step must install the pinned mkosi (v27.1)")
    assert "inputs.sysext_artifact" in str(step.get("if", "")), (
        "the bootstrap step must be gated on inputs.sysext_artifact")
    preflight_i = steps.index(step_named(section, "Preflight the accepted toolchain"))
    assert at < preflight_i, "the bootstrap step must run before 'Preflight the accepted toolchain'"


def test_consumer_rebuilds_the_delivery_image_from_the_raws():
    section = job(load(TEST_WORKFLOW), "test")
    steps = steps_of(section)
    step = step_named(section, "Rebuild the delivery image from the raws")
    at = steps.index(step)
    run = run_lines(step)
    assert any("delivery/build-image.sh --from-staging" in l
               and ".build/ci-sysext-hv/raws" in l for l in run), (
        "the rebuild step must run 'delivery/build-image.sh --from-staging .build/ci-sysext-hv/raws'")
    assert any(l.startswith("docker save") and "sci-delivery.tar" in l for l in run), (
        "the rebuild step must 'docker save' the image to '.build/ci-sysext-hv/artifact/sci-delivery.tar'")
    refs = [l for l in run if "sci-delivery.ref" in l]
    assert refs and any("docker.io/library/sci-delivery:latest" in l for l in refs), (
        "the rebuild step must write the fully-qualified 'docker.io/library/sci-delivery:latest' "
        "to sci-delivery.ref")
    assert "inputs.sysext_artifact" in str(step.get("if", "")), (
        "the rebuild step must be gated on inputs.sysext_artifact")
    prepare_i = steps.index(step_named(section, "Prepare the sysext HyperVisor base"))
    assert at < prepare_i, "the rebuild step must run before 'Prepare the sysext HyperVisor base'"
