#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0

"""A text over the model's token limit is shortened; it must not leave the whole session without embeddings."""

import logging

import pytest
from dem import embedding

LIMIT = 100  # characters, standing in for the model's token limit


class _ApiError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _too_long(index: int) -> _ApiError:
    return _ApiError(
        f"Error code: 400 - {{'error': {{'message': \"litellm.BadRequestError: AzureException BadRequestError - "
        f"Invalid 'input[{index}]': maximum input length is 8192 tokens.\\n\\nLiteLLM: model group failed\"}}}}"
    )


@pytest.fixture
def make_embedder(monkeypatch, mocker):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key")

    def make(create=None):
        embedder = embedding.OpenAIEmbedder("some-model", dimensions=None)
        embedder.client = mocker.Mock()
        calls = []
        embedded = []  # the texts of the requests that were accepted

        def default_create(input, model, **kwargs):
            calls.append(list(input))
            for i, text in enumerate(input):
                if len(text) > LIMIT:
                    raise _too_long(i)
            embedded.extend(input)

            class _Item:
                def __init__(self, text):
                    self.embedding = [float(len(text))] * 4

            class _Response:
                data = [_Item(t) for t in input]

            return _Response()

        embedder.client.embeddings.create.side_effect = create or default_create
        embedder.calls = calls
        embedder.embedded = embedded
        return embedder

    return make


def test_an_oversized_text_is_shortened_and_the_others_are_untouched(make_embedder, caplog):
    embedder = make_embedder()
    batch = ["short", "x" * 450, "also short"]

    with caplog.at_level(logging.WARNING):
        vectors = embedder.encode(batch)

    assert vectors.shape == (3, 4)  # one vector per text, in order: the session is embedded
    assert vectors[0][0] == 5 and vectors[2][0] == 10  # untouched
    assert vectors[1][0] == 56  # 450 -> 225 -> 112 -> 56 characters
    assert "1 text(s) of a batch of 3" in caplog.text and "1: 450 -> 56" in caplog.text


def test_several_oversized_texts_in_one_batch(make_embedder):
    embedder = make_embedder()
    batch = ["x" * 300, "ok", "y" * 1000]

    vectors = embedder.encode(batch)

    assert vectors.shape == (3, 4)
    assert vectors[0][0] <= LIMIT and vectors[2][0] <= LIMIT
    assert vectors[1][0] == 2  # untouched


def test_a_batch_that_fits_is_sent_once_and_unchanged(make_embedder, caplog):
    embedder = make_embedder()

    with caplog.at_level(logging.WARNING):
        embedder.encode(["a", "b"])

    assert embedder.calls == [["a", "b"]]
    assert "truncated" not in caplog.text


def test_other_bad_requests_are_not_retried_as_truncations(make_embedder):
    def create(input, model, **kwargs):
        raise _ApiError("Invalid 'input[0]': must not be empty")

    embedder = make_embedder(create)

    with pytest.raises(_ApiError):
        embedder.encode(["a"])
    assert embedder.client.embeddings.create.call_count == 1


def test_other_status_codes_are_not_retried(make_embedder):
    def create(input, model, **kwargs):
        raise _ApiError("Invalid 'input[0]': maximum input length is 8192 tokens", status_code=429)

    embedder = make_embedder(create)

    with pytest.raises(_ApiError):
        embedder.encode(["x" * 500])
    assert embedder.client.embeddings.create.call_count == 1


def test_the_index_in_the_error_is_not_trusted(make_embedder):
    # Behind some gateways the error names a text that is fine, and keeps naming it: here always input[0]
    def create(input, model, **kwargs):
        if any(len(text) > LIMIT for text in input):
            raise _too_long(0)

        class _Item:
            def __init__(self, text):
                self.embedding = [float(len(text))] * 4

        class _Response:
            data = [_Item(t) for t in input]

        return _Response()

    embedder = make_embedder(create)

    vectors = embedder.encode(["one", "two", "x" * 450, "three"])

    assert vectors.shape == (4, 4)
    assert [vectors[0][0], vectors[1][0], vectors[3][0]] == [3, 3, 5]  # the others are untouched
    assert 0 < vectors[2][0] <= LIMIT


def test_every_text_is_embedded_once_and_in_order(make_embedder):
    embedder = make_embedder()
    batch = ["a", "b" * 450, "c", "d", "e" * 300, "f"]

    vectors = embedder.encode(batch)

    assert vectors.shape == (6, 4)
    assert len(embedder.embedded) == len(batch)  # no text was embedded twice
    # the short ones are exactly where they were, the long ones are in their place, shortened
    assert [int(vectors[i][0]) for i in (0, 2, 3, 5)] == [1, 1, 1, 1]
    assert 0 < vectors[1][0] <= LIMIT and 0 < vectors[4][0] <= LIMIT


def test_it_gives_up_when_nothing_is_left_to_cut(make_embedder):
    def create(input, model, **kwargs):
        raise _too_long(0)  # whatever the text, the API still refuses it

    embedder = make_embedder(create)

    with pytest.raises(_ApiError):
        embedder.encode(["x" * 40])
    # tried at 40, 20, 10, 5, 2 and 1 characters, then it stops: finite, not a loop
    assert embedder.client.embeddings.create.call_count == 6


def _plain_embedder(make_embedder):
    """An embedder whose fake API returns one vector per input, recording each request."""
    return make_embedder()


def test_a_big_session_is_split_into_several_requests_with_one_vector_per_text(make_embedder):
    embedder = make_embedder()
    embedder._MAX_BATCH_CHARS = 100  # a small budget for the test
    texts = [f"{i:02d}" + "x" * 38 for i in range(10)]  # 10 texts of 40 characters

    vectors = embedder.encode(texts)

    assert vectors.shape == (10, 4)  # exactly one vector per text
    assert [len(call) for call in embedder.calls] == [2, 2, 2, 2, 2]  # 2 x 40 characters fit in 100
    assert [t for call in embedder.calls for t in call] == texts  # same texts, same order
    assert [int(v[0]) for v in vectors] == [40] * 10


def test_a_text_over_the_budget_is_sent_alone_not_dropped(make_embedder):
    embedder = make_embedder()
    embedder._MAX_BATCH_CHARS = 100
    # a text of 90 chars (within the fake API's limit) among small ones, with a budget of 100
    texts = ["a" * 30, "b" * 90, "c" * 30]

    vectors = embedder.encode(texts)

    assert vectors.shape == (3, 4)
    assert embedder.calls == [["a" * 30], ["b" * 90], ["c" * 30]]


def test_the_text_count_is_capped_per_request(make_embedder):
    embedder = make_embedder()
    embedder._MAX_BATCH_TEXTS = 3
    texts = [str(i) for i in range(8)]

    embedder.encode(texts)

    assert [len(call) for call in embedder.calls] == [3, 3, 2]


def test_a_session_that_fits_goes_in_one_request(make_embedder):
    embedder = make_embedder()
    embedder.encode(["a", "b", "c"])
    assert embedder.calls == [["a", "b", "c"]]


def test_batching_and_truncation_work_together(make_embedder):
    embedder = make_embedder()
    embedder._MAX_BATCH_CHARS = 300
    texts = ["short", "x" * 450, "also short", "y" * 80]

    vectors = embedder.encode(texts)

    assert vectors.shape == (4, 4)  # still one vector per text
    assert vectors[0][0] == 5 and vectors[2][0] == 10 and vectors[3][0] == 80
    assert vectors[1][0] <= LIMIT  # the oversized one was shortened, not dropped
