"""LLM client for the review arms, with a mock backend for offline validation.

INTEGRITY RULE -- read before using the mock
--------------------------------------------
The mock backend exists ONLY to validate that the pipeline wiring works without
network access or an API key. It does NOT read the dataset, does NOT know the
ground truth, and is NOT a model.

Mock output is therefore MEANINGLESS as an experimental result. To make it
impossible to report by accident, every mock response sets `model="mock"` and
every results file written from a run containing mock responses is stamped
`valid_for_reporting: false`. `experiments/analyze.py` refuses to emit a results
table from such a run.

A mock that returned the seeded defects would amount to fabricating experimental
results. It deliberately does not.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

# The project proposal named Llama 3.3 via Groq. As of 2026-10-04 that model is
# no longer served by Groq (404 model_not_found); the available 131k-context
# code-capable models are openai/gpt-oss-{120b,20b} and qwen/qwen3.8-27b.
# Recorded as a deviation in docs/RESEARCH_DESIGN.md §8.
DEFAULT_MODEL = "openai/gpt-oss-120b"
MOCK_MODEL_NAME = "mock"


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    error: Optional[str] = None

    @property
    def is_mock(self) -> bool:
        return self.model == MOCK_MODEL_NAME


# --------------------------------------------------------------------------- #
# Robust JSON extraction
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> tuple[dict[str, Any], Optional[str]]:
    """Parse a JSON object out of an LLM response.

    Returns (payload, error). On failure returns ({}, reason) rather than
    raising: a malformed response for one case must not abort a whole run, and
    the failure is recorded in ReviewResult.errors so it appears in the results
    rather than vanishing. Silently swallowing these would understate the arm's
    error rate.
    """
    if not text or not text.strip():
        return {}, "empty response"

    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)]
    candidates.append(text)
    # Last resort: the outermost {...} span.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        try:
            payload = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload, None
        if isinstance(payload, list):
            return {"issues": payload}, None

    return {}, f"could not parse JSON from response ({len(text)} chars)"


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

class MockLLM:
    """Deterministic offline stand-in. See the INTEGRITY RULE above."""

    model = MOCK_MODEL_NAME

    def complete(self, system: str, user: str, temperature: float = 0.0,
                 max_tokens: int = 2048, json_mode: bool = True) -> LLMResponse:
        # A fixed, generic response. Intentionally does NOT inspect `user` for
        # the seeded defect -- it must not be able to appear correct.
        payload = {
            "issues": [{
                "file": "MOCK",
                "line": 0,
                "severity": "low",
                "category": "logic",
                "message": ("MOCK RESPONSE -- no model was called. This output is "
                            "for pipeline validation only and is not a result."),
                "suggestion": "Set REVIEWMIND_LLM_MODE=live with a GROQ_API_KEY.",
            }],
            "test_suggestions": [],
        }
        return LLMResponse(
            text=json.dumps(payload),
            model=MOCK_MODEL_NAME,
            prompt_tokens=len(system.split()) + len(user.split()),
            completion_tokens=0,
            latency_s=0.0,
        )


class GroqLLM:
    """Groq chat-completions backend, with retry on transient failures.

    WHY RETRY IS NOT OPTIONAL HERE
    ------------------------------
    The first full three-arm run lost two Arm C data points to HTTP 429
    (rate limit). Because a failed call yields zero issues, the scorer counted
    them as missed detections -- making Arm C appear WORSE than Arm B (80% vs
    100% recall). That difference was caused entirely by API throttling, not by
    retrieval.

    An infrastructure failure that silently becomes a "result" is a measurement
    validity bug, so transient errors are retried with exponential backoff.
    Errors that survive all attempts are still recorded in ReviewResult.errors
    and still counted as misses -- we do not quietly drop inconvenient cases.
    Arm C is the heaviest arm (largest prompts), so throttling hits it first and
    hardest; without retry the bias is systematically against our own method.
    """

    MAX_ATTEMPTS = 5
    BASE_DELAY_S = 4.0

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL,
                 tokens_per_minute: int = 8000) -> None:
        from groq import Groq  # imported lazily so mock mode needs no SDK

        from reviewmind.review.throttle import TokenBucket

        self._client = Groq(api_key=api_key)
        self.model = model
        # Pace calls to stay inside the measured TPM budget. See throttle.py.
        self._bucket = TokenBucket(tokens_per_minute=tokens_per_minute)

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        name = type(exc).__name__
        if name in {"RateLimitError", "APIConnectionError", "APITimeoutError",
                    "InternalServerError", "APIStatusError"}:
            return True
        status = getattr(exc, "status_code", None)
        return status in {408, 409, 429, 500, 502, 503, 504}

    @staticmethod
    def _retry_after(exc: Exception, attempt: int, base: float) -> float:
        """Honour a server-provided Retry-After when present, else back off."""
        resp = getattr(exc, "response", None)
        headers = getattr(resp, "headers", None) or {}
        for key in ("retry-after", "Retry-After"):
            raw = headers.get(key) if hasattr(headers, "get") else None
            if raw:
                try:
                    return min(60.0, float(raw) + 0.5)
                except (TypeError, ValueError):
                    pass
        return min(60.0, base * (2 ** (attempt - 1)))

    def complete(self, system: str, user: str, temperature: float = 0.0,
                 max_tokens: int = 2048, json_mode: bool = True) -> LLMResponse:
        import sys as _sys

        from reviewmind.review.throttle import estimate_tokens

        est = estimate_tokens(system, user, max_completion=min(max_tokens, 800))
        self._bucket.acquire(est)

        started = time.perf_counter()
        last_error: Optional[str] = None

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                resp = self._call(system, user, temperature, max_tokens, started, json_mode)
                self._bucket.record(est, resp.prompt_tokens + resp.completion_tokens)
                return resp
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                if not self._is_transient(exc) or attempt == self.MAX_ATTEMPTS:
                    break
                delay = self._retry_after(exc, attempt, self.BASE_DELAY_S)
                print(f"      transient {type(exc).__name__}, retry "
                      f"{attempt}/{self.MAX_ATTEMPTS - 1} in {delay:.0f}s",
                      file=_sys.stderr)
                time.sleep(delay)

        return LLMResponse(
            text="", model=self.model,
            latency_s=time.perf_counter() - started,
            error=last_error,
        )

    def _call(self, system: str, user: str, temperature: float,
              max_tokens: int, started: float,
              json_mode: bool = True) -> LLMResponse:
        if True:
            extra = ({"response_format": {"type": "json_object"}} if json_mode else {})
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                # Ask for JSON at the API level as well as in the prompt; belt
                # and braces, since a parse failure costs us a data point.
                **extra,
            )

        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text=(resp.choices[0].message.content or ""),
            model=self.model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_s=time.perf_counter() - started,
        )


def build_llm(mode: Optional[str] = None, model: Optional[str] = None):
    """Construct a backend from the environment.

    Falls back to the mock ONLY when no key is present, and says so loudly on
    stderr -- a silent fallback could let a mock run be mistaken for a real one.
    """
    import sys

    mode = (mode or os.getenv("REVIEWMIND_LLM_MODE", "mock")).strip().lower()
    model = model or os.getenv("GROQ_MODEL", DEFAULT_MODEL)
    key = os.getenv("GROQ_API_KEY", "").strip()
    tpm = int(os.getenv("GROQ_TOKENS_PER_MINUTE", "8000"))

    if mode == "live":
        if not key:
            print("WARNING: REVIEWMIND_LLM_MODE=live but GROQ_API_KEY is empty. "
                  "Falling back to MOCK. Results will be marked "
                  "valid_for_reporting=false.", file=sys.stderr)
            return MockLLM()
        return GroqLLM(api_key=key, model=model, tokens_per_minute=tpm)

    return MockLLM()
