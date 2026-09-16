from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError

import pytest

from manual_ingestion.enrichment import CaptionRequest
from manual_ingestion.models import ElementType
from manual_ingestion.providers.ollama import (
    ACCEPTED_OLLAMA_VERSION,
    OllamaCaptionProvider,
    OllamaConfig,
    OllamaProviderError,
)


TEST_MODEL_DIGEST = "2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd"


def test_ollama_provider_uses_vision_and_structured_output(
    tmp_path: Path,
) -> None:
    asset = tmp_path / "image.png"
    asset.write_bytes(b"image-bytes")
    captured: list[tuple[str, dict[str, object] | None]] = []

    def fake_urlopen(request, timeout):
        assert timeout == 7
        payload = json.loads(request.data) if request.data else None
        captured.append((request.full_url, payload))
        if request.full_url.endswith("/api/version"):
            return FakeResponse({"version": ACCEPTED_OLLAMA_VERSION})
        if request.full_url.endswith("/api/tags"):
            return FakeResponse(
                {
                    "models": [
                        {"name": "qwen3.5:4b", "digest": TEST_MODEL_DIGEST}
                    ]
                }
            )
        return FakeResponse(
            {
                "model": "qwen3.5:4b",
                "done": True,
                "done_reason": "stop",
                "response": """Tipo tecnico: UI/HMI
Oggetto: pannello operatore
Elementi visibili: display e tasto avvio
Relazioni/funzione: il tasto e sotto il display
Valori/avvertenze: non applicabile
Rilevanza RAG: dove si trova il tasto avvio?
Incertezze: nessuna evidente""",
            }
        )

    provider = OllamaCaptionProvider(
        OllamaConfig(model="qwen3.5:4b", timeout_seconds=7),
        http_open=fake_urlopen,
    )

    response = provider.caption(
        CaptionRequest(
            element_id="image-1",
            element_type=ElementType.IMAGE,
            prompt="Prompt tecnico",
            asset_path=asset,
        )
    )

    generate_url, payload = captured[2]
    assert generate_url.endswith("/api/generate")
    assert payload is not None
    assert payload["stream"] is False
    assert payload["think"] is False
    assert "format" not in payload
    assert payload["images"]
    assert response.text.startswith("Tipo tecnico: UI/HMI")
    assert response.done_reason == "stop"
    assert response.runtime_version == ACCEPTED_OLLAMA_VERSION
    assert response.params["temperature"] == 0.0


def test_ollama_provider_rejects_incomplete_response(
) -> None:
    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/api/version"):
            return FakeResponse({"version": ACCEPTED_OLLAMA_VERSION})
        if request.full_url.endswith("/api/tags"):
            return FakeResponse(
                {
                    "models": [
                        {"name": "qwen3.5:4b", "digest": TEST_MODEL_DIGEST}
                    ]
                }
            )
        return FakeResponse(
            {
                "model": "qwen3.5:4b",
                "done": False,
                "done_reason": "length",
                "response": "Tipo tecnico: UI/HMI",
            }
        )

    with pytest.raises(OllamaProviderError, match="incomplete"):
        OllamaCaptionProvider(http_open=fake_urlopen).caption(
            CaptionRequest(
                element_id="table-1",
                element_type=ElementType.TABLE,
                prompt="Prompt tecnico",
                asset_path=None,
            )
        )


def test_ollama_connection_error_is_actionable() -> None:
    def fake_urlopen(request, timeout):
        raise URLError("connection refused")

    with pytest.raises(OllamaProviderError, match="Start Ollama"):
        OllamaCaptionProvider(http_open=fake_urlopen).runtime_version()


def test_ollama_preflight_requires_exact_model_tag() -> None:
    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/api/version"):
            return FakeResponse({"version": ACCEPTED_OLLAMA_VERSION})
        return FakeResponse(
            {
                "models": [
                    {"name": "qwen3.5:4b-mlx", "digest": TEST_MODEL_DIGEST}
                ]
            }
        )

    provider = OllamaCaptionProvider(
        OllamaConfig(model="qwen3.5:4b"),
        http_open=fake_urlopen,
    )

    with pytest.raises(OllamaProviderError, match="exact configured.*not installed"):
        provider.preflight()


def test_ollama_preflight_is_cached_across_captions() -> None:
    calls: list[str] = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if request.full_url.endswith("/api/version"):
            return FakeResponse({"version": ACCEPTED_OLLAMA_VERSION})
        if request.full_url.endswith("/api/tags"):
            return FakeResponse(
                {
                    "models": [
                        {"model": "qwen3.5:4b", "digest": TEST_MODEL_DIGEST.upper()}
                    ]
                }
            )
        return FakeResponse(
            {
                "model": "qwen3.5:4b",
                "done": True,
                "response": "Tipo tecnico: diagramma",
            }
        )

    provider = OllamaCaptionProvider(http_open=fake_urlopen)
    request = CaptionRequest(
        element_id="image-1",
        element_type=ElementType.IMAGE,
        prompt="Prompt tecnico",
        asset_path=None,
    )

    first = provider.preflight()
    second = provider.preflight()
    provider.caption(request)
    provider.caption(request)

    assert first == second == {
        "version": ACCEPTED_OLLAMA_VERSION,
        "model": "qwen3.5:4b",
        "model_digest": TEST_MODEL_DIGEST,
    }
    assert sum(url.endswith("/api/version") for url in calls) == 1
    assert sum(url.endswith("/api/tags") for url in calls) == 1
    assert sum(url.endswith("/api/generate") for url in calls) == 2


@pytest.mark.parametrize(
    "digest",
    [None, "", "a" * 63, "g" * 64, f"sha256:{'a' * 64}"],
)
def test_ollama_preflight_requires_full_digest_on_exact_tag_object(
    digest: object,
) -> None:
    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/api/version"):
            return FakeResponse({"version": ACCEPTED_OLLAMA_VERSION})
        return FakeResponse(
            {
                "models": [
                    {"name": "qwen3.5:4b", "digest": digest},
                    {"name": "qwen3.5:4b-mlx", "digest": TEST_MODEL_DIGEST},
                ]
            }
        )

    with pytest.raises(OllamaProviderError, match="valid full SHA-256 digest"):
        OllamaCaptionProvider(http_open=fake_urlopen).preflight()


def test_ollama_preflight_rejects_duplicate_exact_tag_objects() -> None:
    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/api/version"):
            return FakeResponse({"version": ACCEPTED_OLLAMA_VERSION})
        return FakeResponse(
            {
                "models": [
                    {"name": "qwen3.5:4b", "digest": TEST_MODEL_DIGEST},
                    {"model": "qwen3.5:4b", "digest": TEST_MODEL_DIGEST},
                ]
            }
        )

    with pytest.raises(OllamaProviderError, match="multiple objects"):
        OllamaCaptionProvider(http_open=fake_urlopen).preflight()


def test_ollama_public_config_contains_only_reproducible_non_secret_settings() -> None:
    provider = OllamaCaptionProvider(
        OllamaConfig(model="qwen3.5:4b", base_url="http://127.0.0.1:11434")
    )

    public = provider.public_config()

    assert public["model"] == "qwen3.5:4b"
    assert public["temperature"] == 0.0
    assert set(public) == {
        "model",
        "base_url",
        "timeout_seconds",
        "temperature",
        "num_predict",
        "repeat_penalty",
        "num_ctx",
        "thinking",
    }


def test_ollama_config_rejects_alternative_model() -> None:
    with pytest.raises(ValueError, match="accepted 'qwen3.5:4b' setup"):
        OllamaConfig(model="other:latest")


def test_ollama_preflight_rejects_unaccepted_runtime_version() -> None:
    def fake_urlopen(request, timeout):
        return FakeResponse({"version": "0.31.3"})

    with pytest.raises(OllamaProviderError, match="runtime does not match"):
        OllamaCaptionProvider(http_open=fake_urlopen).preflight()


def test_ollama_preflight_rejects_different_valid_model_digest() -> None:
    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/api/version"):
            return FakeResponse({"version": ACCEPTED_OLLAMA_VERSION})
        return FakeResponse(
            {"models": [{"name": "qwen3.5:4b", "digest": "a" * 64}]}
        )

    with pytest.raises(OllamaProviderError, match="different digest"):
        OllamaCaptionProvider(http_open=fake_urlopen).preflight()


def test_ollama_url_validation() -> None:
    provider = OllamaCaptionProvider(OllamaConfig(base_url="http://localhost:11434/api/"))
    assert provider.base_url == "http://localhost:11434"

    with pytest.raises(ValueError, match="absolute http"):
        OllamaConfig(base_url="localhost:11434")


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")
