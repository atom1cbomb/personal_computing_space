import json
import sys
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

    result = gateway.handle("/v1/auth/status", {})

    assert result["status"] == "auth_required"
    assert result["verification_uri"] == "https://login.example/device"
    assert result["user_code"] == "ABCD-EFGH"
    assert "token" not in json.dumps(result).lower()


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
