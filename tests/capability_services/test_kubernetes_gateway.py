import json
import sys
import time
from pathlib import Path

import pytest

from capability_services.kubernetes_http import server
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


@pytest.fixture(autouse=True)
def isolated_gateway_home(tmp_path, monkeypatch):
    monkeypatch.setenv("K8S_GATEWAY_HOME", str(tmp_path / "home"))


@pytest.fixture
def clock(monkeypatch):
    from types import SimpleNamespace
    value = [1000.0]
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: value[0]))
    return value


def _finish_probe(gateway):
    if gateway._auth_process is not None:
        gateway._auth_process.wait(timeout=3)
    return gateway.handle('/v1/auth/status', {})


@pytest.mark.parametrize('message', [
    'error: context deadline exceeded',
    'error: unable to connect to https://api.example: context canceled',
    'error: connection reset by peer',
    'error: credential helper temporarily failed',
])
def test_transient_failure_recovers_without_login_or_restart(tmp_path, clock, message):
    gateway = _gateway(tmp_path, '', stderr=message, returncode=1)
    try:
        failed = gateway.handle('/v1/auth/status', {})
        assert failed['status'] == 'auth_failed' and failed['retryable'] is True
        old = gateway._auth_process
        assert gateway._auth_next_retry_at == clock[0] + 2
        Path(gateway.kubectl).write_text("#!/bin/sh\nprintf 'yes\\n'\n")
        clock[0] += 1
        assert gateway.handle('/v1/auth/status', {}) == failed
        assert gateway._auth_process is old
        clock[0] += 1
        gateway.handle('/v1/auth/status', {})
        assert gateway._auth_process is not old
        assert _finish_probe(gateway) == {'status': 'authenticated'}
        assert gateway._auth_next_retry_at == clock[0] + 30
    finally:
        gateway.close()


def test_repeated_failures_back_off_and_status_polling_does_not_restart(tmp_path, clock):
    gateway = _gateway(tmp_path, '', stderr='error: connection refused', returncode=1)
    try:
        gateway.handle('/v1/auth/status', {})
        for delay in [2, 4, 8, 16, 32, 60, 60]:
            failed = _finish_probe(gateway)
            assert failed['retryable'] is True
            assert gateway._auth_next_retry_at == clock[0] + delay
            previous = gateway._auth_process
            for _ in range(5):
                assert gateway.handle('/v1/auth/status', {}) == failed
            assert gateway._auth_process is previous
            clock[0] += delay
            gateway.handle('/v1/auth/status', {})
    finally:
        gateway.close()


@pytest.mark.parametrize('output,message,reason', [
    ('no', '', 'permission_denied'),
    ('', 'Error from server (Forbidden): pods is forbidden', 'permission_denied'),
    ('', 'x509: certificate signed by unknown authority', 'configuration_error'),
    ('', 'error: exec plugin: invalid apiVersion', 'configuration_error'),
    ('', 'error: oauth2: invalid_client', 'configuration_error'),
])
def test_permission_and_configuration_errors_wait_for_explicit_recheck(tmp_path, clock, output, message, reason):
    gateway = _gateway(tmp_path, output, stderr=message, returncode=1)
    try:
        result = gateway.handle('/v1/auth/status', {})
        assert result == {'status': 'auth_failed', 'retryable': False, 'reason': reason}
        previous = gateway._auth_process
        clock[0] += 600
        assert gateway.handle('/v1/auth/status', {}) == result
        assert gateway._auth_process is previous
        Path(gateway.kubectl).write_text("#!/bin/sh\nprintf 'yes\\n'\n")
        assert gateway.handle('/v1/auth/start', {}) == {'status': 'authenticated'}
    finally:
        gateway.close()


@pytest.mark.parametrize('message', [
    'error: oauth2: invalid_grant: refresh token revoked',
    'error: You must be logged in to the server (Unauthorized)',
])
def test_rejected_credentials_require_login_without_automatic_new_challenges(tmp_path, clock, message):
    gateway = _gateway(tmp_path, '', stderr=message, returncode=1)
    try:
        result = gateway.handle('/v1/auth/status', {})
        assert result['status'] == 'auth_required' and 'verification_uri' not in result
        previous = gateway._auth_process
        clock[0] += 600
        assert gateway.handle('/v1/auth/status', {}) == result
        assert gateway._auth_process is previous
    finally:
        gateway.close()


@pytest.mark.parametrize('refresh', [False, True])
def test_stuck_probe_kills_process_group_then_recovers(tmp_path, clock, refresh):
    gateway = _gateway(tmp_path, 'yes')
    child_file = tmp_path / 'child-pid'
    try:
        if refresh:
            assert gateway.handle('/v1/auth/status', {})['status'] == 'authenticated'
            clock[0] += 31
        Path(gateway.kubectl).write_text(
            f'#!{sys.executable}\nimport subprocess,time,pathlib\n'
            f'child=subprocess.Popen([{sys.executable!r}, "-c", "import time;time.sleep(60)"])\n'
            f'pathlib.Path({str(child_file)!r}).write_text(str(child.pid))\n'
            'time.sleep(60)\n'
        )
        initial = gateway.handle('/v1/auth/status', {})
        assert initial['status'] == ('authenticated' if refresh else 'auth_required')
        deadline = time.monotonic() + 3
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert child_file.exists()
        stuck = gateway._auth_process
        clock[0] += gateway.timeout + 1
        assert gateway.handle('/v1/auth/status', {}) == {
            'status': 'auth_failed', 'retryable': True, 'reason': 'probe_timeout'}
        assert stuck.poll() is not None
        child_stat = Path('/proc') / child_file.read_text() / 'stat'
        deadline = time.monotonic() + 2
        while child_stat.exists() and child_stat.read_text().split(') ', 1)[1][0] != 'Z' and time.monotonic() < deadline:
            time.sleep(.01)
        assert not child_stat.exists() or child_stat.read_text().split(') ', 1)[1][0] == 'Z'
        Path(gateway.kubectl).write_text("#!/bin/sh\nprintf 'yes\\n'\n")
        clock[0] = gateway._auth_next_retry_at
        gateway.handle('/v1/auth/status', {})
        assert _finish_probe(gateway)['status'] == 'authenticated'
    finally:
        gateway.close()


def test_device_challenge_has_separate_deadline_and_never_restarts_on_poll(tmp_path, clock):
    gateway = _gateway(tmp_path, 'yes')
    Path(gateway.kubectl).write_text("#!/bin/sh\nprintf '%s\\n' 'Open https://login.example/device and enter ABCD-EFGH' >&2\nsleep 60\n")
    try:
        first = gateway.handle('/v1/auth/start', {})
        assert first['user_code'] == 'ABCD-EFGH'
        process = gateway._auth_process
        clock[0] += gateway.timeout + 1
        assert gateway.handle('/v1/auth/status', {}) == first
        assert process.poll() is None
        clock[0] += 301
        expired = gateway.handle('/v1/auth/status', {})
        assert expired == {'status': 'auth_required', 'poll_after_seconds': 5}
        assert process.poll() is not None
        clock[0] += 600
        assert gateway.handle('/v1/auth/status', {}) == expired
        assert gateway._auth_process is process
        second = gateway.handle('/v1/auth/start', {})
        assert second['user_code'] == 'ABCD-EFGH'
        assert gateway._auth_process is not process
    finally:
        gateway.close()


def test_concurrent_health_and_service_checks_share_one_retry(tmp_path, clock):
    from concurrent.futures import ThreadPoolExecutor
    gateway = _gateway(tmp_path, '', stderr='error: connection refused', returncode=1)
    starts = tmp_path / 'starts'
    release = tmp_path / 'release-retry'
    try:
        assert gateway.handle('/v1/auth/status', {})['status'] == 'auth_failed'
        Path(gateway.kubectl).write_text(
            f'#!{sys.executable}\nimport pathlib,time\n'
            f'with open({str(starts)!r}, "a") as f: f.write("started\\n")\n'
            f'while not pathlib.Path({str(release)!r}).exists(): time.sleep(.01)\n'
            'print("yes")\n'
        )
        clock[0] = gateway._auth_next_retry_at
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: gateway.handle('/v1/auth/status', {}), range(8)))
        assert all(r['status'] == 'auth_failed' for r in results)
        release.touch()
        assert _finish_probe(gateway)['status'] == 'authenticated'
        assert starts.read_text().splitlines() == ['started']
    finally:
        gateway.close()


def test_probe_diagnostics_never_log_provider_secrets(tmp_path, caplog):
    secret = 'private-refresh-token-test-value'
    gateway = _gateway(tmp_path, '', stderr='error: connection refused Bearer '+secret, returncode=1)
    try:
        result = gateway.handle('/v1/auth/status', {})
        assert result['reason'] == 'transport_error'
        assert 'retry_in=2.0' in caplog.text
        assert secret not in caplog.text and secret not in json.dumps(result)
    finally:
        gateway.close()


def test_missing_plugin_reports_configuration_failure_and_can_be_rechecked(tmp_path, clock):
    gateway = _gateway(tmp_path, 'yes')
    Path(gateway.kubectl).unlink()
    try:
        result = gateway.handle('/v1/auth/status', {})
        assert result == {'status': 'auth_failed', 'retryable': False, 'reason': 'configuration_error'}
        clock[0] += 600
        assert gateway.handle('/v1/auth/status', {}) == result
        Path(gateway.kubectl).write_text("#!/bin/sh\nprintf 'yes\\n'\n")
        Path(gateway.kubectl).chmod(0o755)
        assert gateway.handle('/v1/auth/start', {})['status'] == 'authenticated'
    finally:
        gateway.close()


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
        gateway._auth_next_retry_at = time.monotonic() - 1
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

    assert result == {"status": "auth_failed", "retryable": True, "reason": "transport_error"}
    gateway.close()


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
        gateway._auth_next_retry_at = time.monotonic() - 1
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
        gateway._auth_next_retry_at = time.monotonic() - 1
        assert gateway.handle('/v1/auth/status', {})['status'] == 'authenticated'
        gateway._auth_deadline = time.monotonic() - 1
        assert gateway.handle('/v1/auth/status', {}) == {'status': 'auth_failed', 'retryable': True, 'reason': 'probe_timeout'}
        assert gateway.handle('/v1/auth/start', {})['status'] == 'auth_required'
        assert gateway._auth_pending_status == 'auth_required'
    finally:
        gateway.close()


def test_denied_permission_with_zero_exit_is_not_a_cached_success(tmp_path):
    gateway = _gateway(tmp_path, 'no')
    try:
        assert gateway.handle('/v1/auth/status', {})['status'] == 'auth_failed'
        process = gateway._auth_process
        assert gateway._auth_next_retry_at is None
        assert gateway.handle('/v1/auth/status', {})['status'] == 'auth_failed'
        assert gateway._auth_process is process
    finally:
        gateway.close()
