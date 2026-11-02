"""Ark provider contracts, multimodal forwarding and private authentication."""
import base64
import json
from pathlib import Path

import httpx
import pytest

from capability_services.ark_http import server
from capability_services.ark_http.server import ArkGateway, ArkGatewayError, BASE_URLS


KEY = "afp_test-private-key"
IMAGE = "data:image/png;base64,aW1hZ2U="
ARGUMENTS = {
    "chat-completions": {
        "model": "user-selected-vision-model",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Describe these inputs"},
            {"type": "image_url", "image_url": {"url": IMAGE}},
            {"type": "video_url", "video_url": {"url": "https://media.example/input.mp4", "fps": 1}},
            {"type": "input_audio", "input_audio": {"data": "YXVkaW8=", "format": "wav"}},
        ]}],
        "thinking": {"type": "disabled"},
        "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    },
    "responses-create": {
        "model": "user-selected-vision-model",
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": "Read the document and image"},
            {"type": "input_image", "image_url": IMAGE},
            {"type": "input_file", "file_url": "https://media.example/report.pdf"},
            {"type": "input_video", "video_url": "https://media.example/input.mp4"},
        ]}],
        "previous_response_id": "resp_previous", "reasoning": {"effort": "minimal"},
    },
    "responses-get": {"response_id": "resp_example", "include": ["output_text"]},
    "responses-delete": {"response_id": "resp_example"},
    "images-generate": {
        "model": "doubao-seedream-5.0-pro", "prompt": "参考图片设计一张海报",
        "image": [IMAGE, "https://media.example/reference.png"],
        "size": "2K", "response_format": "b64_json", "watermark": True,
        "sequential_image_generation": "auto", "sequential_image_generation_options": {"max_images": 2},
    },
    "embeddings-multimodal": {
        "model": "doubao-embedding-vision",
        "input": [{"type": "text", "text": "a sunset"}, {"type": "image_url", "image_url": {"url": IMAGE}}],
        "dimensions": 1024,
    },
    "videos-create": {
        "model": "doubao-seedance-2.0",
        "content": [{"type": "text", "text": "Animate the reference"},
                    {"type": "image_url", "image_url": {"url": IMAGE}, "role": "first_frame"}],
        "duration": 5, "generate_audio": True, "ratio": "16:9",
    },
    "videos-get": {"task_id": "cgt-example"},
    "videos-list": {"page_num": 1, "page_size": 10,
                    "filter": {"status": "succeeded", "task_ids": ["cgt-example", "cgt-other"]}},
    "videos-delete": {"task_id": "cgt-example"},
    "audio-speech": {"req_params": {"text": "你好", "speaker": "zh_female_vv_uranus_bigtts",
                                    "audio_params": {"format": "mp3", "sample_rate": 24000}}},
}


def provider(request: httpx.Request) -> httpx.Response:
    if request.url.host == "openspeech.bytedance.com":
        assert request.headers["X-Api-Key"] == KEY
        assert request.headers["X-Api-Resource-Id"] == "seed-tts-2.0"
        assert "Authorization" not in request.headers
        frames = [{"code": 0, "data": base64.b64encode(b"first").decode()},
                  {"code": 0, "data": base64.b64encode(b"second").decode()},
                  {"code": 20000000, "usage": {"text_words": 2}}]
        return httpx.Response(200, content="\n".join(json.dumps(f) for f in frames))
    assert request.headers["Authorization"] == "Bearer " + KEY
    path = request.url.path.removeprefix("/api/plan/v3").removeprefix("/api/v3")
    if request.method == "DELETE":
        return httpx.Response(204)
    if path == "/chat/completions":
        return httpx.Response(200, json={"id": "chat-example", "choices": [{"message": {
            "role": "assistant", "content": "An image and a video", "tool_calls": []}}],
            "usage": {"total_tokens": 24}})
    if path.startswith("/responses"):
        return httpx.Response(200, json={"id": "resp_example", "status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "A report"}]}],
            "usage": {"total_tokens": 30}})
    if path == "/images/generations":
        return httpx.Response(200, json={"created": 1793410000, "data": [
            {"b64_json": "aW1hZ2U=", "size": "2048x2048"}, {"url": "https://media.example/generated.png"}],
            "usage": {"generated_images": 2}})
    if path == "/embeddings/multimodal":
        return httpx.Response(200, json={"data": {"object": "embedding", "embedding": [0.1, 0.2]},
                                        "usage": {"total_tokens": 15}})
    if path == "/contents/generations/tasks":
        result = {"id": "cgt-example"} if request.method == "POST" else {
            "items": [{"id": "cgt-example", "status": "succeeded"}], "total": 1}
        return httpx.Response(200, json=result)
    if path == "/contents/generations/tasks/cgt-example":
        return httpx.Response(200, json={"id": "cgt-example", "status": "succeeded",
            "content": {"video_url": "https://media.example/output.mp4"}, "error": None})
    pytest.fail(f"unexpected provider request: {request.method} {request.url}")


def gateway(tmp_path, upstream=provider, config=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "api_key").write_text(KEY + "\n")
    instance = tmp_path / "instance.json"
    if config is not None:
        instance.write_text(json.dumps(config))
    return ArkGateway(instance_path=instance, credential_root=tmp_path,
                      transport=httpx.MockTransport(upstream))


@pytest.mark.parametrize("mode", ["agent-plan", "standard"])
def test_multimodal_requests_preserve_native_parts_and_selected_model(tmp_path, mode):
    calls = []

    def upstream(request):
        calls.append(request)
        return provider(request)

    gw = gateway(tmp_path, upstream, {"api_mode": mode})
    for action in ("chat-completions", "responses-create", "images-generate", "embeddings-multimodal", "videos-create"):
        before = json.dumps(ARGUMENTS[action])
        result = gw.call(action, ARGUMENTS[action])
        assert json.loads(calls[-1].content) == ARGUMENTS[action]
        assert str(calls[-1].url).startswith(BASE_URLS[mode] + "/")
        assert json.dumps(ARGUMENTS[action]) == before
        assert KEY not in json.dumps(result)
    assert len(calls) == 5  # No verification generation or transparent retry.


def test_video_and_response_queries_encode_filters_without_request_bodies(tmp_path):
    calls = []

    def upstream(request):
        calls.append(request)
        return provider(request)

    gw = gateway(tmp_path, upstream)
    assert gw.call("videos-get", ARGUMENTS["videos-get"])["result"]["content"]["video_url"]
    assert calls[-1].method == "GET" and calls[-1].content == b""
    assert calls[-1].url.path.endswith("/tasks/cgt-example")
    gw.call("videos-list", ARGUMENTS["videos-list"])
    assert calls[-1].url.params.get_list("filter.task_ids") == ["cgt-example", "cgt-other"]
    assert calls[-1].url.params["filter.status"] == "succeeded"
    assert calls[-1].url.params["page_size"] == "10"
    assert calls[-1].content == b""
    assert gw.call("videos-delete", ARGUMENTS["videos-delete"])["result"] == {}
    assert calls[-1].method == "DELETE" and calls[-1].content == b""
    gw.call("responses-get", ARGUMENTS["responses-get"])
    assert calls[-1].url.params.get_list("include") == ["output_text"]
    assert calls[-1].url.path.endswith("/responses/resp_example")
    assert gw.call("responses-delete", ARGUMENTS["responses-delete"])["result"] == {}
    assert calls[-1].method == "DELETE"


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed", "cancelled", "expired"])
def test_video_polling_retains_provider_state_and_failure_details(tmp_path, status):
    task = {"id": "cgt-example", "status": status, "error":
            {"code": "GenerationFailed", "message": "Content could not be generated"} if status == "failed" else None}
    gw = gateway(tmp_path, lambda _: httpx.Response(200, json=task))
    assert gw.call("videos-get", {"task_id": "cgt-example"})["result"] == task


def test_speech_decodes_chunks_before_combining_and_returns_usage(tmp_path):
    result = gateway(tmp_path).call("audio-speech", ARGUMENTS["audio-speech"])["result"]
    assert base64.b64decode(result["audio_base64"]) == b"firstsecond"
    assert result["audio_format"] == "mp3"
    assert result["usage"] == {"text_words": 2}


@pytest.mark.parametrize("wire,code", [
    (b'{"code":0,"data":"Zmlyc3Q="}\n', "provider_speech_incomplete"),
    (b'{"code":0,"data":"Zmlyc3Q="}\n{"code":45000000,"message":"private"}', "provider_speech_failed"),
    (b'{"code":20000000}', "provider_speech_incomplete"),
    (b'{"code":0,"data":"bad-base64"}\n{"code":20000000}', "provider_response_invalid"),
    (b'not json', "provider_response_invalid"),
])
def test_speech_partial_or_failed_stream_is_not_success(tmp_path, wire, code):
    gw = gateway(tmp_path, lambda _: httpx.Response(200, content=wire))
    with pytest.raises(ArkGatewayError, match=code):
        gw.call("audio-speech", ARGUMENTS["audio-speech"])


def test_standard_mode_cannot_send_standard_key_to_speech(tmp_path):
    gw = gateway(tmp_path, lambda _: pytest.fail("key must stay in Ark"), {"api_mode": "standard"})
    with pytest.raises(ArkGatewayError, match="action_requires_agent_plan"):
        gw.call("audio-speech", ARGUMENTS["audio-speech"])


def test_authentication_revalidates_mounted_key_without_generating_or_exposing_data(tmp_path):
    calls = []

    def upstream(request):
        calls.append(request)
        return provider(request)

    gw = gateway(tmp_path, upstream)
    for path in ("/v1/auth/start", "/v1/auth/status"):
        assert gw.handle(path, {}) == {"status": "authenticated"}
        assert calls[-1].method == "GET"
        assert calls[-1].url.path == "/api/plan/v3/contents/generations/tasks"
        assert calls[-1].url.params["page_size"] == "1"
        assert calls[-1].content == b""
    (tmp_path / "api_key").unlink()
    assert gw.auth_status() == {"status": "auth_required"}
    (tmp_path / "api_key").write_text(KEY)
    assert gw.auth_status() == {"status": "authenticated"}


@pytest.mark.parametrize("status,auth,code", [
    (401, "auth_required", "provider_auth_failed"),
    (403, "auth_failed", "provider_forbidden"),
    (404, "auth_failed", "provider_not_found"),
    (429, "auth_failed", "provider_rate_limited"),
    (500, "auth_failed", "provider_unavailable"),
    (400, "auth_failed", "provider_request_failed"),
    (307, "auth_failed", "provider_request_failed"),
])
def test_provider_errors_are_sanitized_and_never_retried_or_redirected(tmp_path, status, auth, code):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(status, text=f"private prompt and secret {KEY}",
                              headers={"Location": "https://untrusted.example/", "Retry-After": "1"})

    gw = gateway(tmp_path, upstream)
    with pytest.raises(ArkGatewayError, match=code) as caught:
        gw.call("images-generate", ARGUMENTS["images-generate"])
    assert KEY not in str(caught.value)
    assert len(calls) == 1
    assert gw.auth_status() == {"status": auth}
    assert len(calls) == 2
    assert all(request.url.host == "ark.cn-beijing.volces.com" for request in calls)


@pytest.mark.parametrize("error,code", [(httpx.ConnectError("private"), "provider_unreachable"),
                                       (httpx.ReadTimeout("private"), "provider_timeout")])
def test_transport_failures_are_not_replayed(tmp_path, error, code):
    calls = []

    def upstream(request):
        calls.append(request)
        raise error

    with pytest.raises(ArkGatewayError, match=code):
        gateway(tmp_path, upstream).call("videos-create", ARGUMENTS["videos-create"])
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["", "bad\nkey", "\u79d8\u94a5", "x" * 4097])
def test_invalid_keys_do_not_reach_provider(tmp_path, value):
    gw = gateway(tmp_path, lambda _: pytest.fail("must reject credential"))
    (tmp_path / "api_key").write_text(value)
    assert gw.auth_status() == {"status": "auth_failed"}


@pytest.mark.parametrize("config", [
    {"api_key": "private"}, {"base_url": "https://untrusted.example"},
    {"api_mode": "coding-plan"}, {"timeout_seconds": 0}, {"timeout_seconds": True}, [],
])
def test_configuration_rejects_secrets_custom_endpoints_and_invalid_modes(tmp_path, config):
    with pytest.raises(ArkGatewayError, match="instance_config_invalid"):
        gateway(tmp_path, config=config)


@pytest.mark.parametrize("action,args", [
    ("chat-completions", {"model": "vision", "messages": [], "stream": True}),
    ("chat-completions", {"model": "   ", "messages": [{}]}),
    ("responses-create", {"model": "vision", "input": "hello", "stream": True}),
    ("images-generate", {"model": "image", "prompt": "picture", "stream": True}),
    ("images-generate", {"model": "image"}),
    ("images-generate", {"model": "image", "prompt": " "}),
    ("videos-get", {"task_id": "../responses"}),
    ("responses-delete", {"response_id": "a?api_key=private"}),
    ("videos-list", {"page_size": 501}),
    ("videos-list", {"filter": {"url": "https://untrusted.example"}}),
    ("audio-speech", {"req_params": {"text": "hi", "speaker": "voice"}}),
    ("embeddings-multimodal", {"model": "embed", "input": []}),
    ("images-generate", {"model": "image", "prompt": "hi", "api_key": "private"}),
])
def test_invalid_business_input_is_rejected_before_provider_call(tmp_path, action, args):
    gw = gateway(tmp_path, lambda _: pytest.fail("must validate before calling"))
    with pytest.raises(ArkGatewayError, match="invalid_input"):
        gw.call(action, args)


def test_layer_decomposition_allows_image_without_prompt(tmp_path):
    gw = gateway(tmp_path)
    assert gw.call("images-generate", {"model": "image", "image": IMAGE, "layer_decomposition": True})["result"]["data"]


@pytest.mark.parametrize("body,code", [
    (b"[]", "provider_response_invalid"),
    (b"<html>login</html>", "provider_invalid_json"),
    (b'{"error":{"message":"private"}}', "provider_request_failed"),
    (b'{"wrong": true}', "provider_response_invalid"),
    (b'{"data":[]}', "provider_response_invalid"),
    (b'{"data":[{"error":{"message":"private"}}]}', "provider_generation_failed"),
])
def test_malformed_or_unsuccessful_generation_is_not_success(tmp_path, body, code):
    with pytest.raises(ArkGatewayError, match=code):
        gateway(tmp_path, lambda _: httpx.Response(200, content=body)).call("images-generate", ARGUMENTS["images-generate"])


def test_group_generation_preserves_partial_image_errors(tmp_path):
    result = {"data": [{"url": "https://media.example/ok.png"}, {"error": {"code": "Rejected"}}]}
    assert gateway(tmp_path, lambda _: httpx.Response(200, json=result)).call(
        "images-generate", ARGUMENTS["images-generate"])["result"] == result


def test_auth_probe_requires_a_real_provider_list_response(tmp_path):
    assert gateway(tmp_path, lambda _: httpx.Response(200, json={"ok": True})).auth_status() == {"status": "auth_failed"}


def test_body_and_response_size_limits(tmp_path, monkeypatch):
    gw = gateway(tmp_path)
    monkeypatch.setattr(server, "MAX_BODY", 8)
    with pytest.raises(ArkGatewayError, match="request_too_large"):
        gw.call("chat-completions", ARGUMENTS["chat-completions"])
    monkeypatch.setattr(server, "MAX_BODY", 1024 * 1024)
    monkeypatch.setattr(server, "MAX_RESPONSE", 8)
    with pytest.raises(ArkGatewayError, match="provider_response_too_large"):
        gw.call("images-generate", ARGUMENTS["images-generate"])


def test_instances_use_separate_keys_and_keys_can_rotate(tmp_path):
    seen = []

    def upstream(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"items": []})

    one = gateway(tmp_path / "one", upstream)
    two = gateway(tmp_path / "two", upstream)
    (tmp_path / "two/api_key").write_text("another-account")
    assert one.auth_status()["status"] == two.auth_status()["status"] == "authenticated"
    (tmp_path / "one/api_key").write_text("rotated-key")
    one.auth_status()
    assert seen == [f"Bearer {KEY}", "Bearer another-account", "Bearer rotated-key"]


def test_runtime_mounted_configuration_accepts_transport_target_metadata(tmp_path):
    gw = gateway(tmp_path, config={"target_resource_ref": None, "api_mode": "standard"})
    assert gw.base_url == BASE_URLS["standard"]
