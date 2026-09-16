"""Caption provider adapters used by the common enrichment stage."""

from .ollama import OllamaCaptionProvider, OllamaConfig, OllamaProviderError

__all__ = ["OllamaCaptionProvider", "OllamaConfig", "OllamaProviderError"]
