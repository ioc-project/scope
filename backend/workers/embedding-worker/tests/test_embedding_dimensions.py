#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0

"""The size of the vectors requested from the embedding API is a parameter, 1024 by default."""

import logging

import click
import pytest
from dem import embedding
from embedding_worker import cli
from embedding_worker.wrapper import embedding_wrapper


class _ApiError(Exception):
    """Stands in for the OpenAI client's BadRequestError (what matters is its ``status_code``)."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _response(batch, size):
    class _Item:
        def __init__(self):
            self.embedding = [0.1] * size

    class _Response:
        data = [_Item() for _ in batch]

    return _Response()


@pytest.fixture
def embedder_factory(monkeypatch, mocker):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key")

    def make(dimensions=None, create=None):
        embedder = embedding.OpenAIEmbedder("some-model", dimensions=dimensions)
        embedder.client = mocker.Mock()
        embedder.client.embeddings.create.side_effect = create or (lambda input, model, **kw: _response(input, 4))
        return embedder

    return make


def test_dimensions_are_sent_to_the_api(embedder_factory):
    embedder = embedder_factory(dimensions=1024)

    embedder.encode(["a", "b"])

    kwargs = embedder.client.embeddings.create.call_args.kwargs
    assert kwargs["dimensions"] == 1024
    assert kwargs["model"] == "some-model"


@pytest.mark.parametrize("dimensions", [None, 0])
def test_no_dimensions_means_the_models_own_size(embedder_factory, dimensions):
    embedder = embedder_factory(dimensions=dimensions)

    embedder.encode(["a"])

    assert "dimensions" not in embedder.client.embeddings.create.call_args.kwargs


def test_a_model_that_refuses_dimensions_falls_back_to_its_own_size(embedder_factory, caplog):
    calls = []

    def create(input, model, **kwargs):
        calls.append(kwargs)
        if "dimensions" in kwargs:
            raise _ApiError("This model does not support specifying dimensions", 400)
        return _response(input, 6)

    embedder = embedder_factory(dimensions=1024, create=create)

    with caplog.at_level(logging.WARNING):
        vectors = embedder.encode(["a", "b"])

    assert vectors.shape == (2, 6)
    assert calls == [{"dimensions": 1024}, {}]
    assert embedder.dimensions is None
    assert "does not accept dimensions=1024" in caplog.text
    # later batches go straight through, without asking again
    embedder.encode(["c"])
    assert calls[-1] == {}
    assert len(calls) == 3


def test_a_bad_input_does_not_turn_dimensions_off(embedder_factory):
    def create(input, model, **kwargs):
        raise _ApiError("This model's maximum context length is 8191 tokens", 400)

    embedder = embedder_factory(dimensions=1024, create=create)

    with pytest.raises(_ApiError):
        embedder.encode(["a very long text"])

    assert embedder.dimensions == 1024


def test_other_api_errors_are_not_swallowed(embedder_factory):
    def create(input, model, **kwargs):
        raise _ApiError("rate limited, mentioning dimensions for no reason", 429)

    embedder = embedder_factory(dimensions=1024, create=create)

    with pytest.raises(_ApiError):
        embedder.encode(["a"])

    assert embedder.dimensions == 1024


def test_cached_vectors_of_another_size_are_not_reused(embedder_factory):
    assert embedder_factory(dimensions=1024)._cache_key == "some-model@1024"
    assert embedder_factory(dimensions=None)._cache_key == "some-model"
    assert embedder_factory(dimensions=0)._cache_key == "some-model"


def test_only_the_api_embedder_receives_the_dimensions(monkeypatch, mocker):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key")
    local = mocker.patch.object(embedding, "SentenceTransformerEmbedder")

    api = embedding_wrapper._build_embedder("OpenAIEmbedder", "m", 768)
    embedding_wrapper._build_embedder("SentenceTransformerEmbedder", "m", 768)

    assert api.dimensions == 768
    local.assert_called_once_with(model_id="m")


def test_the_default_is_1024(monkeypatch):
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)

    assert cli._resolve_embedding_dimensions(None) == 1024
    assert cli.DEFAULT_EMBEDDING_DIMENSIONS == 1024


def test_the_environment_overrides_the_default_and_the_option_overrides_the_environment(monkeypatch):
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "512")
    assert cli._resolve_embedding_dimensions(None) == 512
    assert cli._resolve_embedding_dimensions(256) == 256


def test_zero_switches_the_reduction_off(monkeypatch):
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "0")
    assert cli._resolve_embedding_dimensions(None) == 0
    assert cli._resolve_embedding_dimensions(0) == 0


def test_an_empty_variable_means_the_default(monkeypatch):
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "  ")
    assert cli._resolve_embedding_dimensions(None) == 1024


@pytest.mark.parametrize("value", ["abc", "-5", "10.5"])
def test_an_invalid_variable_is_rejected(monkeypatch, value):
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", value)
    with pytest.raises(click.BadParameter):
        cli._resolve_embedding_dimensions(None)
