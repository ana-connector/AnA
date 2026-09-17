from fastembed import TextEmbedding


class EmbeddingModel:
    """Wraps fastembed (ONNX-based, no PyTorch). sentence-transformers was
    rejected — it fails to install on this machine's disk quota (see the
    roadmap doc, '임베딩 모델')."""

    DIMENSIONS = 384

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        self._model = TextEmbedding(model_name=model_name)

    def embed_one(self, text: str) -> list[float]:
        return next(self._model.embed([text])).tolist()

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(texts)]
