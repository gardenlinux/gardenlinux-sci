"""Golden and CLI contract for the retained summary normalizer."""

import json
import subprocess
import sys
from pathlib import Path

import summary

HERE = Path(__file__).resolve().parent
SAMPLE = HERE / "data/summary-sample.json"
GOLDEN = HERE / "data/summary-sample.normalized.json"
REPO = "/repo"
GOLDEN_SHA = "db139583aba7cb540a3f371d458a45cc9267f5449b352d69de38b8e0a50178de"


def test_golden_normalize_and_digest():
    assert summary.normalize_summary(SAMPLE, REPO) == GOLDEN.read_bytes()
    assert summary.normalized_digest(SAMPLE, REPO) == GOLDEN_SHA


def test_cli_parity():
    out = subprocess.run([sys.executable, str(HERE / "summary.py"), str(SAMPLE), REPO],
                         capture_output=True, check=True)
    assert out.stdout == GOLDEN.read_bytes()


def test_changed_release_changes_normalization(tmp_path):
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    data["Images"][0]["Release"] = "2150.9.1"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(data), encoding="utf-8")
    assert summary.normalize_summary(changed, REPO) != GOLDEN.read_bytes()


def test_delegate_ranges_uid_default_does_not_move_digest(tmp_path):
    # mkosi resolves DelegateRanges from uid (root=0, non-root=3); the pin must not move.
    root_run = json.loads(SAMPLE.read_text(encoding="utf-8"))
    for image in root_run["Images"]:
        image["DelegateRanges"] = 0
    nonroot_run = json.loads(SAMPLE.read_text(encoding="utf-8"))
    for image in nonroot_run["Images"]:
        image["DelegateRanges"] = 3
    root_file = tmp_path / "root-run.json"
    root_file.write_text(json.dumps(root_run), encoding="utf-8")
    nonroot_file = tmp_path / "nonroot-run.json"
    nonroot_file.write_text(json.dumps(nonroot_run), encoding="utf-8")
    assert summary.normalized_digest(root_file, REPO) == \
        summary.normalized_digest(nonroot_file, REPO), (
        "DelegateRanges is a uid-dependent mkosi default (root=0, non-root=3); "
        "the pin must not depend on who ran mkosi"
    )


def test_proxy_exclude_env_default_does_not_move_digest(tmp_path):
    # mkosi emits no_proxy/NO_PROXY as ProxyExclude; the pin must not depend on the shell.
    no_proxy_run = json.loads(SAMPLE.read_text(encoding="utf-8"))
    for image in no_proxy_run["Images"]:
        image["ProxyExclude"] = ["127.0.0.1", "localhost", "::1"]
    clean_run = json.loads(SAMPLE.read_text(encoding="utf-8"))
    no_proxy_file = tmp_path / "no-proxy-run.json"
    no_proxy_file.write_text(json.dumps(no_proxy_run), encoding="utf-8")
    clean_file = tmp_path / "clean-run.json"
    clean_file.write_text(json.dumps(clean_run), encoding="utf-8")
    assert summary.normalized_digest(clean_file, REPO) == \
        summary.normalized_digest(no_proxy_file, REPO), (
        "ProxyExclude is an environment-derived mkosi default (read from "
        "no_proxy/NO_PROXY); the pin must not depend on the invoking "
        "shell's proxy variables, or contributors with no_proxy set (and "
        "the development harness here) fail the pin test for reasons unrelated "
        "to the tree"
    )
