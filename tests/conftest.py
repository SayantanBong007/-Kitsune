"""
Test fixtures — run fully offline.

The real embedding/reranker models are replaced with a deterministic
bag-of-words embedder so tests are fast and need no model download.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import numpy as np
import pytest

# `import kitsune` needs the folder that CONTAINS the kitsune package on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class FakeEmbedder:
    """Hashes words into a 384-dim normalized vector (similar words → similar vectors)."""

    DIM = 384

    def _one(self, text: str) -> np.ndarray:
        vec = np.zeros(self.DIM, dtype=np.float32)
        for word in re.findall(r"[a-z]+", text.lower()):
            vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.DIM] += 1.0
        norm = np.linalg.norm(vec)
        return vec / norm if norm else vec

    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
        if isinstance(texts, str):
            return self._one(texts)
        return np.stack([self._one(t) for t in texts])

    def get_sentence_embedding_dimension(self):
        return self.DIM


@pytest.fixture(autouse=True)
def offline_models(monkeypatch, tmp_path):
    from kitsune.embedder import embedder, reranker

    monkeypatch.setattr(embedder, "_model", FakeEmbedder())
    monkeypatch.setattr(reranker, "_reranker", False)  # reranker "unavailable"
    for var in ("QDRANT_URL", "QDRANT_LOCAL_PATH", "NEO4J_URI"):
        monkeypatch.delenv(var, raising=False)
    # Unreachable Neo4j → NetworkX fallback without waiting on a real server
    monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:1")
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
