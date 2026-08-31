from __future__ import annotations

import pytest

from eval.benchmark_embeddings import _cosine, _enforce_top_3_gate, _model_artifact, fixture_corpus


def test_embedding_benchmark_fixture_has_top_three_safe_needles() -> None:
    corpus, questions = fixture_corpus()
    assert len(corpus) == 50
    assert len(questions) == 30
    assert all(gold in {document_id for document_id, _ in corpus} for _, gold in questions)


def test_cosine_handles_simple_and_zero_vectors() -> None:
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert _cosine([0.0], [1.0]) == 0.0


def test_embedding_benchmark_enforces_the_top_three_gate() -> None:
    _enforce_top_3_gate(1.0)

    with pytest.raises(RuntimeError, match="top-3 retrieval gate"):
        _enforce_top_3_gate(29 / 30)


def test_embedding_artifact_records_a_modelfile_revision() -> None:
    artifact = _model_artifact(
        "nomic-embed-text:v1.5",
        [
            {
                "name": "nomic-embed-text:v1.5",
                "details": {"quantization_level": "F16"},
            }
        ],
        {"modelfile": "FROM sha256-deadbeef"},
    )

    assert artifact["quantization"] == "F16"
    assert artifact["modelfile_sha256"] == (
        "84d87788d0b3799d80e312cb4b9422636dd76e0203a3a98ee6dfe34119f60c73"
    )
