from packages.memory.embeddings import EmbeddingModel


def test_embed_one_returns_384_floats():
    model = EmbeddingModel()
    vector = model.embed_one("hello world")
    assert len(vector) == 384
    assert all(isinstance(x, float) for x in vector)


def test_embed_many_matches_embed_one_per_item():
    model = EmbeddingModel()
    vectors = model.embed_many(["hello world", "goodbye world"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 384
    assert len(vectors[1]) == 384


def test_identical_text_yields_identical_embedding():
    model = EmbeddingModel()
    assert model.embed_one("same text") == model.embed_one("same text")


def test_different_text_yields_different_embedding():
    model = EmbeddingModel()
    assert model.embed_one("apple") != model.embed_one("astrophysics")
