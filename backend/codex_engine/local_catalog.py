"""Pinned, verified public assets. Downloads occur only on an explicit user action."""
from dataclasses import dataclass

RUNTIME_TAG = "b11429"
RUNTIME_URL = "https://github.com/ggml-org/llama.cpp/releases/download/b11429/llama-b11429-bin-win-cpu-x64.zip"
RUNTIME_SHA256 = "1283323272b04cd07905816a597a0da810918102de958f4ff6f7bbaa70ed2efe"
DEFAULT_MODEL = "qwen2.5-1.5b-instruct-q4_k_m"


@dataclass(frozen=True)
class ModelAsset:
    id: str
    label: str
    filename: str
    url: str
    sha256: str
    size: int


MODEL = ModelAsset(
    DEFAULT_MODEL, "Qwen 2.5 1.5B Instruct (Q4_K_M, about 1.1 GB)",
    "qwen2.5-1.5b-instruct-q4_k_m.gguf",
    "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf",
    "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e", 1117320736,
)
CATALOG = {MODEL.id: MODEL}
