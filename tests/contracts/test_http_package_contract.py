from copy import deepcopy

import pytest

from loom_v2.contracts.types import CapabilityPackageVersion, ResourceRef


def _resource_ref(resource_id: str, *, descriptor_digest: str | None = None) -> dict[str, object]:
    return ResourceRef(
        resource_id=resource_id,
        version_or_digest=descriptor_digest,
        identity_criterion="descriptor_digest" if descriptor_digest else "content_digest",
    ).model_dump(mode="json")


def _export(
    descriptor_id: str,
    descriptor_digest: str,
    io_digest: str,
    path: str,
    *,
    permissions: list[str] | None = None,
) -> dict[str, object]:
    return {
        "action_descriptor_ref": _resource_ref(
            descriptor_id, descriptor_digest=descriptor_digest
        ),
        "io_contract_ref": _resource_ref("grip://loom.local/workspaces/workspace-default/content/document/sha256/" + io_digest),
        "effect_class": "NetworkService",
        "permissions": permissions or [],
        "replay_safety": "DeclaredByPackage",
        "runtime_binding": {"path": path},
    }


def http_package_payload() -> dict[str, object]:
    return {
        "package_type": "service",
        "package_id": "pkg-http",
        "package_version": "v1",
        "package_closure_version_ref": "closure-1",
        "source_run_ref": "run-1",
        "source_closure_version_ref": "version-1",
        "execution": {"kind": "container:http", "version": "1"},
        "action_exports": [
            _export("grip://loom.local/workspaces/workspace-default/actions/http/a", "a" * 64, "c" * 64, "/v1/a")
        ],
        "body": {
            "image_ref": "registry.example/loom/service@sha256:" + "d" * 64,
            "container_port": 8080,
            "health_path": "/healthz",
        },
    }


@pytest.mark.parametrize(
    ("mutate",),
    [
        (lambda value: value["body"].update({"unknown": True}),),
        (lambda value: value["body"].update({"container_port": 0}),),
        (lambda value: value["body"].update({"health_path": "//host/path"}),),
        (
            lambda value: value["body"].update(
                {
                    "image_ref": "registry.example/loom/service:latest@sha256:"
                    + "d" * 64
                }
            ),
        ),
        (
            lambda value: value["action_exports"][0]["runtime_binding"].update(
                {"unknown": True}
            ),
        ),
        (
            lambda value: value["action_exports"][0].update(
                {"runtime_binding": {"path": "relative"}}
            ),
        ),
    ],
)
def test_http_contract_rejects_invalid_or_unknown_fields(mutate) -> None:
    payload = http_package_payload()
    mutate(payload)

    with pytest.raises(ValueError, match="package_contract_invalid"):
        CapabilityPackageVersion.model_validate(payload)


def test_http_contract_round_trips_full_manifest() -> None:
    package = CapabilityPackageVersion.model_validate(http_package_payload())

    restored = CapabilityPackageVersion.model_validate(package.model_dump(mode="json"))

    assert restored == package


def test_http_contract_accepts_how_to_use_content_reference() -> None:
    payload = http_package_payload()
    payload["how_to_use_ref"] = _resource_ref(
        "grip://loom.local/workspaces/workspace-default/content/pkg/how-to-use/sha256/"
        + "e" * 64
    )
    package = CapabilityPackageVersion.model_validate(payload)
    assert package.how_to_use_ref is not None
    assert package.how_to_use_ref.identity_criterion == "content_digest"


@pytest.mark.parametrize("interaction,browser,valid", [
    ({"mode": "redirect"}, True, True), ({"mode": "redirect"}, False, False),
    ({"mode": "device-code"}, False, True), ({"mode": "device-code"}, True, False),
    ({"mode": "credential-profile"}, False, True),
    ({"mode": "browser-cookie", "cookie_name": "grafana_session"}, True, True),
    ({"mode": "browser-cookie"}, True, False),
    ({"mode": "browser-cookie", "cookie_name": "bad;cookie"}, True, False),
    ({"mode": "custom-script", "entrypoint": "code.py"}, True, False),
])
def test_authentication_interaction_contract(interaction, browser, valid):
    payload = http_package_payload()
    auth = {"start_path": "/auth/start", "status_path": "/auth/status", "interaction": interaction}
    if browser:
        auth["browser_path"] = "/auth/browser"
    payload["body"]["authentication"] = auth
    if valid:
        CapabilityPackageVersion.model_validate(payload)
    else:
        with pytest.raises(ValueError, match="package_contract_invalid"):
            CapabilityPackageVersion.model_validate(payload)


def test_historical_authentication_idp_is_readable_without_rewriting_the_package():
    payload = http_package_payload()
    payload["body"]["authentication"] = {
        "start_path": "/auth/start", "status_path": "/auth/status",
        "browser_path": "/auth/browser",
        "idp": "https://login.example/tenant",
        "interaction": {"mode": "browser-cookie", "cookie_name": "sid"},
    }
    package = CapabilityPackageVersion.model_validate(payload)
    assert package.body["authentication"]["idp"] == "https://login.example/tenant"


@pytest.mark.parametrize("idp", ["http://login.example", "https://login.example/a?b=c", "https://login.example/a\\nb"])
def test_authentication_interaction_rejects_unsafe_idp(idp):
    payload = http_package_payload()
    payload["body"]["authentication"] = {
        "start_path": "/auth/start", "status_path": "/auth/status",
        "idp": idp,
        "interaction": {"mode": "redirect"},
    }
    with pytest.raises(ValueError, match="package_contract_invalid"):
        CapabilityPackageVersion.model_validate(payload)


def test_historical_browser_client_is_readable_without_rewriting_the_package():
    payload = http_package_payload()
    payload["body"]["authentication"] = {"start_path": "/auth/start", "status_path": "/auth/status",
        "browser_path": "/auth/browser", "client": {"kind": "local-browser-bridge", "protocol_version": "1",
                                                     "entrypoint": "client/auth.py"}}
    package = CapabilityPackageVersion.model_validate(payload)
    assert package.body == payload["body"]


def test_historical_http_package_credential_profile_remains_readable() -> None:
    payload = http_package_payload()
    payload["body"]["credential_profile"] = "k8s-prod_1"

    package = CapabilityPackageVersion.model_validate(payload)

    assert package.body["credential_profile"] == "k8s-prod_1"


def test_http_contract_accepts_bounded_egress_network_profile() -> None:
    payload = http_package_payload()
    payload["body"]["network_profile"] = "egress"

    package = CapabilityPackageVersion.model_validate(payload)

    assert package.body["network_profile"] == "egress"


@pytest.mark.parametrize("profile", ["../secret", "/tmp/x", "bad name", ""])
def test_http_contract_rejects_invalid_credential_profile(profile: str) -> None:
    payload = http_package_payload()
    payload["body"]["credential_profile"] = profile

    with pytest.raises(ValueError, match="package_contract_invalid"):
        CapabilityPackageVersion.model_validate(payload)


def test_duplicate_descriptor_export_is_rejected() -> None:
    payload = http_package_payload()
    payload["action_exports"].append(
        _export("grip://loom.local/workspaces/workspace-default/actions/http/a", "a" * 64, "e" * 64, "/v1/b")
    )

    with pytest.raises(ValueError, match="action_export_duplicate"):
        CapabilityPackageVersion.model_validate(payload)


def test_export_and_permission_reordering_does_not_change_digest() -> None:
    left_payload = http_package_payload()
    left_payload["action_exports"] = [
        _export(
            "grip://loom.local/workspaces/workspace-default/actions/http/b",
            "b" * 64,
            "e" * 64,
            "/v1/b",
            permissions=["network", "read"],
        ),
        _export(
            "grip://loom.local/workspaces/workspace-default/actions/http/a",
            "a" * 64,
            "c" * 64,
            "/v1/a",
            permissions=["read", "network"],
        ),
    ]
    right_payload = deepcopy(left_payload)
    right_payload["action_exports"].reverse()
    for action_export in right_payload["action_exports"]:
        action_export["permissions"].reverse()
        action_export["action_descriptor_ref"]["access_binding"] = {
            "endpoint": "elsewhere"
        }
        action_export["action_descriptor_ref"]["provenance"] = [
            {"source": "promotion"}
        ]

    left = CapabilityPackageVersion.model_validate(left_payload)
    right = CapabilityPackageVersion.model_validate(right_payload)

    assert right.package_digest == left.package_digest


def test_content_backed_descriptor_digest_participates_in_package_identity() -> None:
    left_payload = http_package_payload()
    descriptor_ref = left_payload["action_exports"][0][
        "action_descriptor_ref"
    ]
    descriptor_ref["resource_id"] = "grip://loom.local/workspaces/workspace-default/content/document/sha256/" + "f" * 64
    right_payload = deepcopy(left_payload)
    right_payload["action_exports"][0]["action_descriptor_ref"][
        "version_or_digest"
    ] = "e" * 64

    left = CapabilityPackageVersion.model_validate(left_payload)
    right = CapabilityPackageVersion.model_validate(right_payload)

    assert right.package_digest != left.package_digest


async def test_new_package_publication_rejects_package_level_credentials():
    from loom_v2.observer.repository import ObserverRepository
    repo = ObserverRepository()
    payload = http_package_payload()
    payload['body']['credential_profile'] = 'k8s-prod'
    # Stored immutable manifests can still be decoded, but cannot be published anew.
    historical = CapabilityPackageVersion.model_validate(payload)
    assert historical.body['credential_profile'] == 'k8s-prod'
    with pytest.raises(ValueError, match='credential_profile_requires_activation'):
        await repo.register_workspace_package('workspace-default', payload)
