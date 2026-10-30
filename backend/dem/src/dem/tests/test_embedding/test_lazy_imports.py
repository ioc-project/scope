#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""The embedders import their client library when they are built, so an API-only install can use dem.embedding."""

import subprocess
import sys
import textwrap


def _run(code: str) -> str:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_importing_the_package_loads_no_model_library():
    loaded = _run(
        """
        import sys
        import dem.embedding
        heavy = ("torch", "transformers", "sentence_transformers", "gensim", "openai", "google.genai")
        print(",".join(name for name in heavy if name in sys.modules))
        """
    )
    assert loaded == ""


def test_api_embedder_works_without_the_local_model_libraries():
    out = _run(
        """
        import sys
        for name in ("torch", "transformers", "sentence_transformers", "gensim"):
            sys.modules[name] = None  # importing them raises ImportError, as when they are not installed
        from dem.embedding import OpenAIEmbedder
        OpenAIEmbedder("text-embedding-3-small", api_key="key", base_url="http://localhost")
        print("torch" in sys.modules and sys.modules["torch"] is not None)
        """
    )
    assert out == "False"


def test_local_embedder_without_its_libraries_says_which_extra_to_install():
    out = _run(
        """
        import sys
        sys.modules["torch"] = None
        from dem.embedding import SentenceTransformerEmbedder
        try:
            SentenceTransformerEmbedder("all-MiniLM-L6-v2")
        except ImportError as exc:
            print(exc)
        """
    )
    assert "dem[embedding-local]" in out
