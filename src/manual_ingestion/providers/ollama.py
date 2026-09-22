"""Local Ollama vision adapter; caption text is validated after generation."""

from __future__ import annotations

import base64
import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..enrichment import CaptionRequest, CaptionResponse


_FULL_MODEL_DIGEST = re.compile(r"[0-9a-fA-F]{64}")
OLLAMA_PROVIDER_NAME = "ollama"
ACCEPTED_OLLAMA_MODEL = "qwen3.5:4b"
ACCEPTED_OLLAMA_MODEL_DIGEST = (
    "2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd"
)
# Reference release of the published examples; any release is accepted at runtime.
ACCEPTED_OLLAMA_VERSION = "0.34.0"
# Append-only prompt versions accepted when validating saved bundles, with the
# Ollama release each was introduced on. The runtime itself is recorded but not
# enforced (see ollama_runtime_accepted): Ollama updates itself.
OLLAMA_RUNTIME_BY_PROMPT_VERSION = {
    "technical-caption-v2": "0.31.2",
    "technical-caption-v3": "0.34.0",
    "technical-caption-v4": "0.34.0",
}
ACCEPTED_OLLAMA_OUTPUT_PARAMS: dict[str, object] = {
    "temperature": 0.0,
    "num_predict": 512,
    "repeat_penalty": 1.15,
    "num_ctx": 4096,
    "thinking": False,
}


def ollama_runtime_accepted(version: object) -> bool:
    """Any released Ollama version is accepted; it is recorded, never enforced.

    Reproducibility rests on the pinned model digest, prompt version and output
    parameters. The exact runtime stays in provenance for traceability.
    """
    if not isinstance(version, str):
        return False
    parts = version.split(".")
    return len(parts) >= 2 and all(part.isdigit() for part in parts)


class OllamaProviderError(RuntimeError):
    """Raised when the local Ollama service cannot produce a valid caption."""


@dataclass(frozen=True)
class OllamaConfig:
    model: str = ACCEPTED_OLLAMA_MODEL
    base_url: str = "http://127.0.0.1:11434"
    timeout_seconds: int = 180
    temperature: float = 0.0
    num_predict: int = 512
    repeat_penalty: float = 1.15
    num_ctx: int = 4096

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("Ollama model cannot be empty")
        if self.model != ACCEPTED_OLLAMA_MODEL:
            raise ValueError(
                f"Ollama model must be the accepted {ACCEPTED_OLLAMA_MODEL!r} setup"
            )
        if self.timeout_seconds <= 0:
            raise ValueError("Ollama timeout_seconds must be positive")
        if self.num_predict <= 0 or self.num_ctx <= 0:
            raise ValueError("Ollama token limits must be positive")
        _normalize_base_url(self.base_url)


class OllamaCaptionProvider:
    """Call Ollama's local ``/api/generate`` endpoint without a client dependency."""

    def __init__(
        self,
        config: OllamaConfig | None = None,
        *,
        http_open: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config or OllamaConfig()
        self.base_url = _normalize_base_url(self.config.base_url)
        self._http_open = http_open or urllib.request.urlopen
        self._runtime_version: str | None = None
        self._preflight_runtime: dict[str, str] | None = None

    def preflight(self) -> dict[str, str]:
        """Verify Ollama and the exact configured model once per provider.

        The runtime version is recorded, not enforced: Ollama updates itself.
        """

        if self._preflight_runtime is not None:
            return dict(self._preflight_runtime)

        version = self.runtime_version()
        data = self._request_json("GET", "/api/tags")
        models = data.get("models")
        if not isinstance(models, list):
            raise OllamaProviderError("Ollama /api/tags returned no models array")

        installed_tags: set[str] = set()
        matching_models: list[dict[str, Any]] = []
        for index, model in enumerate(models):
            if not isinstance(model, dict):
                raise OllamaProviderError(
                    f"Ollama /api/tags model at index {index} is not an object"
                )
            tags = {
                value
                for key in ("name", "model")
                if isinstance((value := model.get(key)), str) and value
            }
            if not tags:
                raise OllamaProviderError(
                    f"Ollama /api/tags model at index {index} has no valid tag"
                )
            installed_tags.update(tags)
            if self.config.model in tags:
                matching_models.append(model)

        if not matching_models:
            available = ", ".join(sorted(installed_tags)) or "none"
            raise OllamaProviderError(
                "The exact configured Ollama model tag "
                f"'{self.config.model}' is not installed (available: {available}). "
                f"Install it with: ollama pull {self.config.model}"
            )
        if len(matching_models) != 1:
            raise OllamaProviderError(
                "Ollama /api/tags returned multiple objects for the exact configured "
                f"model tag '{self.config.model}'"
            )

        digest = matching_models[0].get("digest")
        if not isinstance(digest, str) or _FULL_MODEL_DIGEST.fullmatch(digest) is None:
            raise OllamaProviderError(
                "Ollama /api/tags returned no valid full SHA-256 digest for the exact "
                f"configured model tag '{self.config.model}'"
            )
        canonical_digest = digest.lower()
        if canonical_digest != ACCEPTED_OLLAMA_MODEL_DIGEST:
            raise OllamaProviderError(
                "The exact configured Ollama model tag has a different digest from "
                "the accepted enrichment setup"
            )

        self._preflight_runtime = {
            "version": version,
            "model": self.config.model,
            "model_digest": canonical_digest,
        }
        return dict(self._preflight_runtime)

    def public_config(self) -> dict[str, Any]:
        """Return only non-secret, output-affecting settings for run provenance."""

        return {
            "model": self.config.model,
            "base_url": self.base_url,
            "timeout_seconds": self.config.timeout_seconds,
            "temperature": self.config.temperature,
            "num_predict": self.config.num_predict,
            "repeat_penalty": self.config.repeat_penalty,
            "num_ctx": self.config.num_ctx,
            "thinking": False,
        }

    def caption(self, request: CaptionRequest) -> CaptionResponse:
        runtime = self.preflight()
        payload: dict[str, Any] = {
            "model": self.config.model,
            "prompt": request.prompt,
            "stream": False,
            "think": False,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.num_predict,
                "repeat_penalty": self.config.repeat_penalty,
                "num_ctx": self.config.num_ctx,
            },
        }
        if request.asset_path is not None:
            payload["images"] = [_encode_image(request.asset_path)]

        data = self._request_json("POST", "/api/generate", payload)
        if data.get("done") is not True:
            raise OllamaProviderError("Ollama returned an incomplete non-streaming response")

        raw_caption = data.get("response")
        if not isinstance(raw_caption, str) or not raw_caption.strip():
            raise OllamaProviderError("Ollama returned an empty caption")

        response_model = data.get("model")
        if response_model is not None and response_model != self.config.model:
            raise OllamaProviderError(
                "Ollama generated the caption with an unexpected model tag: "
                f"expected '{self.config.model}', received {response_model!r}"
            )

        return CaptionResponse(
            text=_clean_caption(raw_caption),
            provider="ollama",
            model=self.config.model,
            params={
                "temperature": self.config.temperature,
                "num_predict": self.config.num_predict,
                "repeat_penalty": self.config.repeat_penalty,
                "num_ctx": self.config.num_ctx,
                "thinking": False,
            },
            runtime_version=runtime["version"],
            done_reason=str(data.get("done_reason") or ""),
        )

    def runtime_version(self) -> str:
        if self._runtime_version is not None:
            return self._runtime_version
        data = self._request_json("GET", "/api/version")
        version = data.get("version")
        if isinstance(version, str) and version.strip():
            self._runtime_version = version.strip()
            return self._runtime_version
        raise OllamaProviderError("Ollama /api/version returned no valid version string")

    def _request_json(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{endpoint}",
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with self._http_open(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise OllamaProviderError(
                f"Ollama {endpoint} failed with HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise OllamaProviderError(
                f"Cannot reach local Ollama at {self.base_url}: {exc}. "
                "Start Ollama and verify the configured model is installed."
            ) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OllamaProviderError(f"Ollama {endpoint} returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise OllamaProviderError(f"Ollama {endpoint} returned a non-object response")
        return data


def _clean_caption(value: str) -> str:
    lines = [" ".join(line.strip().split()) for line in value.strip().splitlines()]
    return "\n".join(line for line in lines if line)


def _encode_image(path: Path) -> str:
    try:
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        raise OllamaProviderError(f"Cannot read caption asset '{path}': {exc}") from exc


def _normalize_base_url(value: str) -> str:
    clean = value.strip().rstrip("/")
    if clean.endswith("/api"):
        clean = clean[:-4]
    parsed = urlparse(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Ollama base_url must be an absolute http(s) URL")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("Ollama base_url cannot include a path, query, or fragment")
    return clean
