#!/usr/bin/env python3
"""
scripts/smoke_test_gemini.py

Run this FIRST, before trying any real question through the API. It
exercises exactly one thing: does llm_client.call_llm_json() successfully
reach Gemini and get back parseable JSON. Everything else in the pipeline
(routing, query construction, validation) depends on this one call shape
working -- if it doesn't, every single question will fail the same way, so
it's much faster to find that out here than by debugging a full pipeline
run.

Usage:
    GEMINI_API_KEY=<your key> python3 scripts/smoke_test_gemini.py
    # or, if you have a .env file:
    python3 scripts/smoke_test_gemini.py

Exit code 0 = the call worked and returned valid JSON. Non-zero = read the
printed error; it'll be one of:
  - ImportError: google-genai isn't installed (`pip install google-genai`)
  - An SDK-level exception: the installed google-genai version's
    generate_content()/GenerateContentConfig() signature differs from what
    app/llm_client.py assumes. Check the installed version's docs and
    adjust `_call_gemini_once` in that file -- it's the only place that
    needs to change.
  - A JSON parse failure: the model responded but not with valid JSON --
    less likely with response_mime_type="application/json" set, but print
    the raw text to see what came back.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app import llm_client  # noqa: E402


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY")
    # Must match config.py's Settings.gemini_model default. This was
    # previously "gemini-2.0-flash-lite", which Google shut down on
    # June 1, 2026 -- this script would 404 immediately if GEMINI_MODEL
    # wasn't set in the environment, with no clue that the *default*
    # itself was the problem. Keep these two defaults in sync.
    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
    if not api_key or api_key.startswith("replace-with"):
        print("GEMINI_API_KEY is not set (or still the .env.example placeholder). "
              "Set it in your environment or .env file.")
        return 1

    print(f"Calling Gemini (model={model!r}) with a trivial prompt...")
    try:
        response = llm_client.call_llm_json(
            prompt="Reply with the JSON object {\"ok\": true, \"model_said\": \"<put a short greeting here>\"}",
            system_instruction="Respond with ONLY a JSON object, no other text, no markdown fences.",
            api_key=api_key,
            model=model,
            max_retries=1,  # fail fast for a smoke test; the real pipeline retries 3x
        )
    except llm_client.LLMCallError as e:
        print(f"\nFAILED: {e}")
        print("\nThis means the SDK call itself is broken -- check that google-genai is "
              "installed and up to date, and that the call shape in "
              "app/llm_client.py::_call_gemini_once matches the installed version's API.")
        return 1

    print(f"\nSUCCESS. Raw text: {response.text!r}")
    print(f"Parsed: {response.parsed}")
    if not isinstance(response.parsed, dict) or "ok" not in response.parsed:
        print("\nWARNING: got valid JSON back, but not the shape asked for. The SDK "
              "call itself works; something about how it's following instructions "
              "may need attention, but that's a prompt-quality question, not a "
              "wiring problem.")
        return 0

    print("\nThe Gemini integration is working end-to-end. Safe to move on to "
          "scripts/smoke_test_prometheus.py, then real questions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
