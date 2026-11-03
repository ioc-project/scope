#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""OpenAI-compatible judge client, in place of the LiteLLM one of ``metrics-computation-engine``.

``metrics_computation_engine.llm_judge.llm.LLMClient`` is a thin wrapper around ``litellm.completion`` and importing it
loads LiteLLM, about 300 MB of memory per process. The judge only talks to one OpenAI-compatible gateway, so this client
calls the ``openai`` SDK directly, with the same constructor and ``query`` signature.

``install()`` registers it as ``metrics_computation_engine.llm_judge.llm``, so the ``Jury`` and ``BaseMetric`` classes
of that package (which import ``LLMClient`` when they are imported) use it too.
"""

import sys
import threading
import types
from typing import Any, Dict, List, Optional

_LLM_MODULE = "metrics_computation_engine.llm_judge.llm"

# Parameters the judge adds when it can, and that some models refuse. LiteLLM's ``drop_params`` removed them silently.
_OPTIONAL_PARAMS = ("reasoning_effort", "temperature", "response_format")


class LLMClient:
    """One model behind an OpenAI-compatible endpoint (Azure OpenAI when ``api_version`` is set)."""

    def __init__(self, model_config: Dict[str, Any]) -> None:
        self.model = model_config.get("LLM_MODEL_NAME", "")
        self.api_base = model_config["LLM_BASE_MODEL_URL"]
        self.api_key = model_config["LLM_API_KEY"]
        self.api_version = model_config.get("api_version", "")
        self.num_retries = int(model_config.get("NUM_LLM_RETRIES", 3))
        self._client: Optional[Any] = None
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        # Built on the first query: the key may be empty when the client is only constructed (tests, no gateway).
        with self._lock:
            if self._client is None:
                import openai

                if self.api_version:
                    self._client = openai.AzureOpenAI(
                        azure_endpoint=self.api_base,
                        api_key=self.api_key,
                        api_version=self.api_version,
                        max_retries=self.num_retries,
                    )
                else:
                    self._client = openai.OpenAI(
                        base_url=self.api_base or None,
                        api_key=self.api_key,
                        max_retries=self.num_retries,
                    )
            return self._client

    def query(self, messages: List[Dict[str, str]], **kwargs: Any) -> Any:
        """Run one chat completion and return the SDK response (``choices``, ``usage``)."""
        import openai

        client = self._get_client()
        try:
            return client.chat.completions.create(
                model=self.model, messages=messages, **kwargs
            )
        except openai.BadRequestError as exc:
            # The endpoint rejected an optional parameter: retry without it, as LiteLLM's drop_params did.
            refused = [
                name for name in _OPTIONAL_PARAMS if name in kwargs and name in str(exc)
            ]
            if not refused:
                raise
            kept = {k: v for k, v in kwargs.items() if k not in refused}
            return client.chat.completions.create(
                model=self.model, messages=messages, **kept
            )


def install() -> None:
    """Make ``metrics_computation_engine.llm_judge.llm`` resolve to this client, unless LiteLLM's is already loaded."""
    if _LLM_MODULE in sys.modules:
        return
    module = types.ModuleType(_LLM_MODULE)
    module.__doc__ = __doc__
    module.LLMClient = LLMClient  # type: ignore[attr-defined]
    sys.modules[_LLM_MODULE] = module
