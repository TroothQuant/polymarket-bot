"""AI ensemble probability estimation for prediction markets.

Supported providers: anthropic, openai, openrouter, azure_openai, gemini
"""

import json
import logging
import statistics
import time
from typing import Optional

import requests

from config import BotConfig
from models import MarketInfo, Estimate

log = logging.getLogger("bot.estimator")

SYSTEM_PROMPT = """You are a calibrated probability estimator for prediction markets.
Given a market question, estimate the TRUE probability that the outcome resolves YES.

Rules:
- Output ONLY valid JSON: {"probability": 0.XX, "reasoning": "one sentence"}
- probability must be between 0.02 and 0.98
- Be well-calibrated: events you rate at 70% should happen ~70% of the time
- Use base rates, current knowledge, and logical reasoning
- The current market price reflects real-money consensus from many informed traders — treat it as a Bayesian prior. Only deviate significantly if you have strong specific reasoning.
- If deeply uncertain, stay close to the market price
- Keep reasoning under 50 words"""


def _build_user_prompt(market: MarketInfo) -> str:
    desc = market.description[:500] if market.description else "N/A"
    return (
        f"Market: {market.question}\n"
        f"Event: {market.event_title}\n"
        f"Description: {desc}\n"
        f"Category: {market.category}\n"
        f"Resolution date: {market.end_date or 'Unknown'}\n"
        f"Current market price: YES at {market.outcome_yes_price:.0%} / NO at {market.outcome_no_price:.0%}\n\n"
        f"Estimate the probability this resolves YES. Output JSON only."
    )


class Estimator:
    def __init__(self, config: BotConfig):
        self.config = config
        self._provider = config.ai_provider.lower()
        self._model = config.ai_model or config.claude_model  # backward compat

        if self._provider == "anthropic":
            import anthropic as _anthropic_sdk
            self._anthropic_sdk = _anthropic_sdk
            kwargs: dict = {"api_key": config.anthropic_api_key}
            if config.anthropic_api_host:
                kwargs["base_url"] = config.anthropic_api_host
            self._anthropic_client = _anthropic_sdk.Anthropic(**kwargs)
        else:
            self._anthropic_sdk = None
            self._anthropic_client = None

    def estimate(self, market: MarketInfo) -> Optional[Estimate]:
        """Run ensemble estimation: N independent AI calls, trimmed mean."""
        raw_estimates: list[float] = []
        total_input = 0
        total_output = 0
        first_reasoning = ""

        for _ in range(self.config.ensemble_size):
            result = self._single_call(market)
            if result is None:
                continue
            prob, reasoning, in_tok, out_tok = result
            raw_estimates.append(prob)
            if not first_reasoning:
                first_reasoning = reasoning
            total_input += in_tok
            total_output += out_tok

        if len(raw_estimates) < 2:
            log.warning(f"Only {len(raw_estimates)} valid estimates for: {market.question[:60]}")
            if not raw_estimates:
                return None

        if len(raw_estimates) >= 4:
            sorted_est = sorted(raw_estimates)
            trimmed = sorted_est[1:-1]
        else:
            trimmed = raw_estimates

        fair_prob = statistics.mean(trimmed)
        confidence = statistics.stdev(raw_estimates) if len(raw_estimates) > 1 else 1.0

        if len(raw_estimates) >= 2 and confidence > self.config.max_estimate_std:
            log.info(
                f"SKIP (low confidence): {market.question[:50]}... "
                f"std={confidence:.3f} > max={self.config.max_estimate_std:.3f}"
            )
            return None

        log.info(
            f"Estimate: {market.question[:50]}... -> {fair_prob:.2%} "
            f"(n={len(raw_estimates)}, std={confidence:.3f})"
        )

        return Estimate(
            market_condition_id=market.condition_id,
            question=market.question,
            fair_probability=fair_prob,
            raw_estimates=raw_estimates,
            confidence=confidence,
            reasoning_summary=first_reasoning,
            input_tokens_used=total_input,
            output_tokens_used=total_output,
        )

    # ── Provider dispatch ──────────────────────────────────────────────────

    def _single_call(self, market: MarketInfo):
        """Single call to the configured AI provider. Returns (prob, reasoning, in_tok, out_tok) or None."""
        if self._provider == "anthropic":
            return self._call_anthropic(market)
        elif self._provider == "gemini":
            return self._call_gemini(market)
        elif self._provider in ("openai", "openrouter", "azure_openai"):
            return self._call_openai_compat(market)
        else:
            log.error(f"Unknown AI provider: {self._provider}")
            return None

    def _parse_json_response(self, text: str):
        """Parse probability JSON from model response. Returns (prob, reasoning) or None."""
        try:
            if text.startswith("```"):
                lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
                text = "\n".join(lines)
            data = json.loads(text)
            prob = float(data["probability"])
            reasoning = data.get("reasoning", "")
            prob = max(0.02, min(0.98, prob))
            return prob, reasoning
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            log.debug(f"Failed to parse estimate response: {e}")
            return None

    # ── Anthropic ─────────────────────────────────────────────────────────

    def _call_anthropic(self, market: MarketInfo):
        try:
            response = self._anthropic_client.messages.create(
                model=self._model,
                max_tokens=self.config.max_estimate_tokens,
                temperature=self.config.ensemble_temperature,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _build_user_prompt(market)}],
            )
            text = response.content[0].text.strip()
            in_tok = response.usage.input_tokens
            out_tok = response.usage.output_tokens
            result = self._parse_json_response(text)
            if result is None:
                return None
            prob, reasoning = result
            return prob, reasoning, in_tok, out_tok
        except self._anthropic_sdk.RateLimitError:
            log.warning("Anthropic rate limit — waiting 5s")
            time.sleep(5)
            return None
        except self._anthropic_sdk.APIError as e:
            log.error(f"Anthropic API error: {e}")
            return None

    # ── OpenAI-compatible (OpenAI, OpenRouter, Azure OpenAI) ──────────────

    def _call_openai_compat(self, market: MarketInfo):
        provider = self._provider
        model = self._model

        if provider == "openai":
            host = (self.config.openai_api_host or "https://api.openai.com").rstrip("/")
            url = f"{host}/v1/chat/completions"
            headers = {"Authorization": f"Bearer {self.config.openai_api_key}"}
        elif provider == "openrouter":
            host = (self.config.openrouter_api_host or "https://openrouter.ai").rstrip("/")
            url = f"{host}/api/v1/chat/completions"
            headers = {"Authorization": f"Bearer {self.config.openrouter_api_key}"}
        else:  # azure_openai
            endpoint = self.config.azure_openai_endpoint.rstrip("/")
            deployment = self.config.azure_openai_deployment
            version = self.config.azure_openai_api_version or "2024-02-01"
            url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={version}"
            headers = {"api-key": self.config.azure_openai_api_key}
            model = deployment  # Azure uses deployment name

        headers["Content-Type"] = "application/json"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(market)},
            ],
            "temperature": self.config.ensemble_temperature,
            "max_tokens": self.config.max_estimate_tokens,
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code == 429:
                log.warning(f"{provider} rate limit — waiting 5s")
                time.sleep(5)
                return None
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            usage = data.get("usage", {})
            in_tok = usage.get("prompt_tokens", 0)
            out_tok = usage.get("completion_tokens", 0)
            result = self._parse_json_response(text)
            if result is None:
                return None
            prob, reasoning = result
            return prob, reasoning, in_tok, out_tok
        except requests.exceptions.HTTPError as e:
            log.error(f"{provider} API error: {e}")
            return None
        except Exception as e:
            log.debug(f"{provider} call failed: {e}")
            return None

    # ── Google Gemini ─────────────────────────────────────────────────────

    def _call_gemini(self, market: MarketInfo):
        model = self._model
        host = (self.config.gemini_api_host or "https://generativelanguage.googleapis.com").rstrip("/")
        url = f"{host}/v1beta/models/{model}:generateContent?key={self.config.gemini_api_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": _build_user_prompt(market)}]}],
            "generationConfig": {
                "temperature": self.config.ensemble_temperature,
                "maxOutputTokens": self.config.max_estimate_tokens,
            },
        }
        try:
            resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
            if resp.status_code == 429:
                log.warning("Gemini rate limit — waiting 5s")
                time.sleep(5)
                return None
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            usage = data.get("usageMetadata", {})
            in_tok = usage.get("promptTokenCount", 0)
            out_tok = usage.get("candidatesTokenCount", 0)
            result = self._parse_json_response(text)
            if result is None:
                return None
            prob, reasoning = result
            return prob, reasoning, in_tok, out_tok
        except requests.exceptions.HTTPError as e:
            log.error(f"Gemini API error: {e}")
            return None
        except Exception as e:
            log.debug(f"Gemini call failed: {e}")
            return None

    # ── API key validation ────────────────────────────────────────────────

    def validate_api_key(self) -> bool:
        """Make a minimal test call to validate the configured provider's API key.

        Returns False on auth failure (HTTP 401/403, AuthenticationError).
        Other errors (network, rate-limit) return True so transient failures don't block startup.
        """
        provider = self._provider
        try:
            if provider == "anthropic":
                self._anthropic_client.messages.create(
                    model=self._model,
                    max_tokens=1,
                    messages=[{"role": "user", "content": "hi"}],
                )
                return True

            elif provider in ("openai", "openrouter", "azure_openai"):
                if provider == "openai":
                    host = (self.config.openai_api_host or "https://api.openai.com").rstrip("/")
                    url = f"{host}/v1/chat/completions"
                    auth_headers = {"Authorization": f"Bearer {self.config.openai_api_key}"}
                elif provider == "openrouter":
                    host = (self.config.openrouter_api_host or "https://openrouter.ai").rstrip("/")
                    url = f"{host}/api/v1/chat/completions"
                    auth_headers = {"Authorization": f"Bearer {self.config.openrouter_api_key}"}
                else:
                    endpoint = self.config.azure_openai_endpoint.rstrip("/")
                    deployment = self.config.azure_openai_deployment
                    version = self.config.azure_openai_api_version or "2024-02-01"
                    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={version}"
                    auth_headers = {"api-key": self.config.azure_openai_api_key}
                resp = requests.post(
                    url,
                    json={"model": self._model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
                    headers={**auth_headers, "Content-Type": "application/json"},
                    timeout=10,
                )
                if resp.status_code in (401, 403):
                    return False
                return True

            elif provider == "gemini":
                host = (self.config.gemini_api_host or "https://generativelanguage.googleapis.com").rstrip("/")
                resp = requests.post(
                    f"{host}/v1beta/models/{self._model}:generateContent"
                    f"?key={self.config.gemini_api_key}",
                    json={"contents": [{"parts": [{"text": "hi"}]}], "generationConfig": {"maxOutputTokens": 1}},
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
                # Gemini returns 400 with "API key" in body for invalid keys (not 401)
                if resp.status_code == 403:
                    return False
                if resp.status_code == 400 and "API key" in resp.text:
                    return False
                return True

        except Exception as e:
            if self._anthropic_sdk and isinstance(e, self._anthropic_sdk.AuthenticationError):
                return False
            # Network errors, rate limits, etc. — don't block startup
            return True

        return True
