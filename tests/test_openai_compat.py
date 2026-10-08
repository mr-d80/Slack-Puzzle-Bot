from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import ai_smoketest
import insights
import nl_query
from openai_compat import (
    chat_completion_token_limit_param,
    default_reasoning_effort,
    model_uses_reasoning,
    supports_temperature,
)


class GPT6RequestCompatibilityTests(unittest.TestCase):
    def test_auto_routes_gpt6_to_responses_for_bot_and_smoketest(self) -> None:
        expected = ("responses", "/v1/responses")
        self.assertEqual(insights._resolve_api_mode("gpt-6-luna", "auto"), expected)
        self.assertEqual(ai_smoketest._resolve_api_mode("gpt-6-luna", "auto"), expected)

    def test_gpt6_defaults_to_low_reasoning_and_enables_it_for_queries(self) -> None:
        self.assertEqual(default_reasoning_effort("gpt-6-luna"), "low")
        self.assertTrue(model_uses_reasoning("gpt-6-luna"))
        self.assertTrue(nl_query._model_supports_reasoning("gpt-6-luna"))
        self.assertEqual(nl_query._env_reasoning_effort("gpt-6-luna"), "low")

    def test_gpt6_temperature_requires_explicit_none_effort(self) -> None:
        self.assertFalse(supports_temperature("gpt-6-luna", "low"))
        self.assertFalse(supports_temperature("gpt-6-luna", ""))
        self.assertTrue(supports_temperature("gpt-6-luna", "none"))

    def test_chat_completions_uses_current_token_limit_field(self) -> None:
        self.assertEqual(chat_completion_token_limit_param("gpt-6-luna"), "max_completion_tokens")
        self.assertEqual(chat_completion_token_limit_param("gpt-5-mini"), "max_completion_tokens")
        self.assertEqual(chat_completion_token_limit_param("gpt-4.1-mini"), "max_tokens")

    def test_smoketest_chat_payload_omits_temperature_with_reasoning(self) -> None:
        body = ai_smoketest._build_chat_body(
            model="gpt-6-luna",
            sys_prompt="system",
            user_prompt="user",
            temperature=0.7,
            max_tokens=500,
            reasoning_effort="low",
            include_temperature=supports_temperature("gpt-6-luna", "low"),
        )
        self.assertEqual(body["max_completion_tokens"], 500)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)
        self.assertEqual(body["reasoning_effort"], "low")

    def test_smoketest_responses_payload_uses_responses_token_field(self) -> None:
        body = ai_smoketest._build_responses_body(
            model="gpt-6-luna",
            sys_prompt="system",
            user_prompt="user",
            extra_fields={"text": {"verbosity": "medium"}},
            temperature=0.7,
            max_tokens=500,
            reasoning_effort="low",
            include_temperature=supports_temperature("gpt-6-luna", "low"),
        )
        self.assertEqual(body["max_output_tokens"], 500)
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)
        self.assertEqual(body["text"], {"verbosity": "medium"})

    def test_natural_language_translation_sends_gpt6_reasoning_to_responses(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_MODEL": "gpt-6-luna",
                "OPENAI_API_MODE": "auto",
                "OPENAI_REASONING_EFFORT": "low",
            },
        ), patch.object(nl_query, "_call_openai_translate", return_value={"measure": "wins"}) as call:
            result = nl_query._openai_translate("who wins Zip?", ["Zip"], "2026-09-23")

        self.assertEqual(result, {"measure": "wins"})
        mode, payload = call.call_args.args[:2]
        self.assertEqual(mode, "responses")
        self.assertEqual(payload["max_output_tokens"], nl_query.DEFAULT_MAX_OUT)
        self.assertEqual(payload["reasoning"], {"effort": "low"})

    def test_explicit_gpt6_chat_fallback_uses_supported_fields(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_MODEL": "gpt-6-luna",
                "OPENAI_API_MODE": "chat",
                "OPENAI_REASONING_EFFORT": "low",
            },
        ), patch.object(nl_query, "_call_openai_translate", return_value={"measure": "wins"}) as call:
            result = nl_query._openai_translate("who wins Zip?", ["Zip"], "2026-09-23")

        self.assertEqual(result, {"measure": "wins"})
        mode, payload = call.call_args.args[:2]
        self.assertEqual(mode, "chat")
        self.assertEqual(payload["max_completion_tokens"], nl_query.DEFAULT_MAX_OUT)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["reasoning_effort"], "low")


if __name__ == "__main__":
    unittest.main()
