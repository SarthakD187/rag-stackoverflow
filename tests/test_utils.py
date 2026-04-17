import pytest

from shared.utils import chunk_text, cosine_similarity


def test_chunk_text_expected_overlap_and_count() -> None:
    text = "abcdefghijklmnopqrstuvwxyz"
    chunks = chunk_text(text=text, chunk_chars=10, overlap=2)

    assert chunks == ["abcdefghij", "ijklmnopqr", "qrstuvwxyz"]


def test_cosine_similarity_identity_and_orthogonal() -> None:
    identical = cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
    orthogonal = cosine_similarity([1.0, 0.0], [0.0, 1.0])

    assert identical == pytest.approx(1.0)
    assert orthogonal == pytest.approx(0.0)
