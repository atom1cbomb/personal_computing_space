import json
import sys
import time
from pathlib import Path

import pytest

from capability_services.kubernetes_http.server import KubernetesGateway


@pytest.mark.parametrize("path", ["/v1/resources/list", "/v1/resources/get"])
@pytest.mark.parametrize("resource,api_version", [
    ("vcjobs", "batch.volcano.sh/v1alpha1"),
    ("jobs", "batch/v1"),
])
def test_job_queries_resolve_the_correct_api_group(tmp_path: Path, monkeypatch, path, resource, api_version):
    gateway = _gateway(tmp_path, "yes")
    monkeypatch.setattr(gateway, "_require_auth", lambda: None)
    # Model the cluster's resource discovery: vcjob/vj are short names,
    # while the plural vcjobs is not a kubectl resource name.
    Path(gateway.kubectl).write_text(
        f"#!{sys.executable}\n"
        "import json,sys\n"
        "args=sys.argv[1:]\n"
        "resource=args[args.index('get')+1]\n"
        "groups={'jobs':'batch/v1','jobs.batch.volcano.sh':'batch.volcano.sh/v1alpha1','vcjob':'batch.volcano.sh/v1alpha1'}\n"
        "if resource not in groups: sys.exit(1)\n"
        "print(json.dumps({'apiVersion':groups[resource], 'kind':'JobList'}))\n",
        encoding="utf-8",
    )

    result = gateway.handle(path, {"resource": resource, "name": "example"})

    assert result["status"] == "ok"
    assert result["resource"]["apiVersion"] == api_version


def _gateway(
    tmp_path: Path,
    output: str,
    *,
    stderr: str = "",
    returncode: int = 0,
) -> KubernetesGateway:
    (tmp_path / "kubeconfig").write_text("fake", encoding="utf-8")
    (tmp_path / "policy.json").write_text(
        json.dumps({"namespace": "dev-example", "context": "ctx"}),
        encoding="utf-8",
    )
    fake = tmp_path / "kubectl"
    fake.write_text(
        "#!/bin/sh\n"
        f"printf '%s' {output!r}\n"
        f"printf '%s' {stderr!r} >&2\n"
        f"exit {returncode}\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return KubernetesGateway(credential_root=tmp_path, kubectl=str(fake))


def test_auth_challenge_is_public_data_only(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        "",
        stderr="Open https://login.example/device and enter ABCD-EFGH",
        returncode=1,
    )

    result = gateway._challenge("", "Open https://login.example/device and enter ABCD-EFGH")

    assert result["status"] == "auth_required"
    assert result["verification_uri"] == "https://login.example/device"
    assert result["user_code"] == "ABCD-EFGH"
    assert "token" not in json.dumps(result).lower()


def test_exited_device_attempt_does_not_return_a_dead_login_link(tmp_path):
    gateway = _gateway(tmp_path, "", stderr="Open https://login.example/device and enter ABCD-EFGH", returncode=1)
    try:
        gateway.handle("/v1/auth/start", {})
        gateway._auth_process.wait(timeout=2)
        assert gateway.handle("/v1/auth/status", {}) == {"status": "auth_required", "poll_after_seconds": 5}
        old = gateway._auth_process
        gateway.handle("/v1/auth/start", {})
        assert gateway._auth_process is not old
    finally:
        gateway.close()


def test_success_is_reprobed_and_expired_credentials_start_a_new_device_flow(tmp_path):
    gateway = _gateway(tmp_path, "yes")
    try:
        assert gateway.handle("/v1/auth/status", {})["status"] == "authenticated"
        old = gateway._auth_process
        Path(gateway.kubectl).write_text("#!/bin/sh\nprintf '%s\\n' 'Open https://login.example/device and enter NEWC-ODE1' >&2\nsleep 30\n")
        gateway._auth_completed_at = time.monotonic() - 31
        for _ in range(50):
            result = gateway.handle("/v1/auth/status", {})
            if result.get("verification_uri"):
                break
            time.sleep(.02)
        assert result["status"] == "auth_required"
        assert result["user_code"] == "NEWC-ODE1"
        assert gateway._auth_process is not old
        assert gateway._auth_process.poll() is None
    finally:
        gateway.close()


def test_api_server_url_is_not_mistaken_for_login_challenge(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        "",
        stderr="error: unable to connect to https://k8s.example/api: context canceled",
        returncode=1,
    )

    result = gateway.handle("/v1/auth/status", {})

    assert result == {"status": "auth_failed", "retryable": False}


def test_auth_start_and_status_share_one_login_process(tmp_path: Path) -> None:
    (tmp_path / "kubeconfig").write_text("fake", encoding="utf-8")
    (tmp_path / "policy.json").write_text(
        json.dumps({"namespace": "dev-example"}), encoding="utf-8"
    )
    fake = tmp_path / "kubectl"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'Open https://login.example/device and enter ABCD-EFGH' >&2\n"
        "sleep 1\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    gateway = KubernetesGateway(credential_root=tmp_path, kubectl=str(fake))

    first = gateway.handle("/v1/auth/start", {})
    second = gateway.handle("/v1/auth/status", {})

    assert first["verification_uri"] == second["verification_uri"]
    assert first["user_code"] == second["user_code"]
    gateway.close()


def test_submit_rejects_non_volcano_or_wrong_namespace(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, "yes")
    with pytest.raises(Exception):
        gateway.handle("/v1/jobs/submit", {"manifest": {"kind": "Pod"}})
    with pytest.raises(Exception):
        gateway.handle(
            "/v1/jobs/submit",
            {
                "manifest": {
                    "apiVersion": "batch.volcano.sh/v1alpha1",
                    "kind": "Job",
                    "metadata": {"name": "job", "namespace": "other"},
                }
            },
        )


@pytest.mark.parametrize('output,returncode,expected', [('yes', 0, 'authenticated'), ('no', 1, 'auth_failed')])
def test_periodic_refresh_preserves_readiness_until_verified_result(tmp_path, output, returncode, expected):
    gateway = _gateway(tmp_path, 'yes')
    release = tmp_path / 'release'
    try:
        assert gateway.handle('/v1/auth/status', {})['status'] == 'authenticated'
        Path(gateway.kubectl).write_text(
            f'#!{sys.executable}\nimport pathlib,time,sys\n'
            f'while not pathlib.Path({str(release)!r}).exists(): time.sleep(.02)\n'
            f'print({output!r});sys.exit({returncode})\n'
        )
        gateway._auth_completed_at = time.monotonic() - 31
        assert gateway.handle('/v1/auth/status', {})['status'] == 'authenticated'
        process = gateway._auth_process
        assert process.poll() is None
        assert gateway.handle('/v1/auth/status', {})['status'] == 'authenticated'
        release.touch()
        process.wait(timeout=3)
        assert gateway.handle('/v1/auth/status', {})['status'] == expected
    finally:
        gateway.close()


def test_refresh_has_bounded_grace_and_explicit_login_drops_cached_success(tmp_path):
    gateway = _gateway(tmp_path, 'yes')
    try:
        assert gateway.handle('/v1/auth/status', {})['status'] == 'authenticated'
        Path(gateway.kubectl).write_text('#!/bin/sh\nsleep 30\n')
        gateway._auth_completed_at = time.monotonic() - 31
        assert gateway.handle('/v1/auth/status', {})['status'] == 'authenticated'
        gateway._revalidation_deadline = time.monotonic() - 1
        assert gateway.handle('/v1/auth/status', {}) == {'status': 'auth_failed', 'retryable': True}
        assert gateway.handle('/v1/auth/start', {})['status'] == 'auth_required'
        assert gateway._revalidation_deadline is None
    finally:
        gateway.close()


def test_denied_permission_with_zero_exit_is_not_a_cached_success(tmp_path):
    gateway = _gateway(tmp_path, 'no')
    try:
        assert gateway.handle('/v1/auth/status', {})['status'] == 'auth_failed'
        process = gateway._auth_process
        gateway._auth_completed_at = time.monotonic() - 31
        assert gateway.handle('/v1/auth/status', {})['status'] == 'auth_failed'
        assert gateway._auth_process is process
    finally:
        gateway.close()
