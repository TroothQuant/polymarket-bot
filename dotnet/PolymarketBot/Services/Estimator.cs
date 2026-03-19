using System.Text;
using System.Text.Json;
using Microsoft.Extensions.Logging;
using PolymarketBot.Models;

namespace PolymarketBot.Services;

public sealed class Estimator
{
    private const string SystemPrompt =
        "You are a calibrated probability estimator for prediction markets.\n" +
        "Given a market question, estimate the TRUE probability that the outcome resolves YES.\n" +
        "\n" +
        "Rules:\n" +
        "- Output ONLY valid JSON: {\"probability\": 0.XX, \"reasoning\": \"one sentence\"}\n" +
        "- probability must be between 0.02 and 0.98\n" +
        "- Be well-calibrated: events you rate at 70% should happen ~70% of the time\n" +
        "- Use base rates, current knowledge, and logical reasoning\n" +
        "- The current market price reflects real-money consensus from many informed traders — treat it as a Bayesian prior. Only deviate significantly if you have strong specific reasoning.\n" +
        "- If deeply uncertain, stay close to the market price\n" +
        "- Keep reasoning under 50 words";

    private readonly BotConfig _config;
    private readonly HttpClient _http;
    private readonly ILogger<Estimator> _log;

    public Estimator(BotConfig config, HttpClient http, ILogger<Estimator> log)
    {
        _config = config;
        _http = http;
        _log = log;
    }

    public async Task<Estimate?> EstimateAsync(MarketInfo market, CancellationToken ct = default)
    {
        var rawEstimates = new List<double>();
        var totalInput = 0;
        var totalOutput = 0;
        var firstReasoning = "";

        for (var i = 0; i < _config.EnsembleSize; i++)
        {
            var result = await SingleCallAsync(market, ct);
            if (result is null) continue;

            rawEstimates.Add(result.Value.Probability);
            if (string.IsNullOrEmpty(firstReasoning))
                firstReasoning = result.Value.Reasoning;
            totalInput += result.Value.InputTokens;
            totalOutput += result.Value.OutputTokens;
        }

        if (rawEstimates.Count < 2)
        {
            _log.LogWarning("Only {Count} valid estimates for: {Question}",
                rawEstimates.Count, Truncate(market.Question, 60));
            if (rawEstimates.Count == 0) return null;
        }

        // Trimmed mean: drop highest and lowest if enough samples
        List<double> trimmed;
        if (rawEstimates.Count >= 4)
        {
            var sorted = rawEstimates.OrderBy(x => x).ToList();
            trimmed = sorted.Skip(1).Take(sorted.Count - 2).ToList();
        }
        else
        {
            trimmed = rawEstimates;
        }

        var fairProb = trimmed.Average();
        var confidence = rawEstimates.Count > 1 ? StdDev(rawEstimates) : 1.0;

        // Confidence filter: skip if ensemble disagreement is too high
        if (rawEstimates.Count >= 2 && confidence > _config.MaxEstimateStd)
        {
            _log.LogInformation("SKIP (low confidence): {Question} std={Std:F3} > max={Max:F3}",
                Truncate(market.Question, 50), confidence, _config.MaxEstimateStd);
            return null;
        }

        _log.LogInformation("Estimate: {Question} -> {Prob:P2} (n={Count}, std={Std:F3})",
            Truncate(market.Question, 50), fairProb, rawEstimates.Count, confidence);

        return new Estimate
        {
            MarketConditionId = market.ConditionId,
            Question = market.Question,
            FairProbability = fairProb,
            RawEstimates = rawEstimates,
            Confidence = confidence,
            ReasoningSummary = firstReasoning,
            InputTokensUsed = totalInput,
            OutputTokensUsed = totalOutput,
        };
    }

    private async Task<CallResult?> SingleCallAsync(MarketInfo market, CancellationToken ct)
    {
        // Retry delays for transient overload (429 / 529): 10s, 20s, 40s
        int[] backoffMs = { 10_000, 20_000, 40_000 };

        for (var attempt = 0; attempt <= backoffMs.Length; attempt++)
        {
            try
            {
                var (status, body) = await MakeProviderRequestAsync(market, ct);

                if (status == 429 || status == 529)
                {
                    if (attempt < backoffMs.Length)
                    {
                        var delay = backoffMs[attempt];
                        _log.LogWarning("{Provider} {Status} (attempt {A}/{Max}) — retrying in {Sec}s",
                            _config.AiProvider, status, attempt + 1, backoffMs.Length, delay / 1000);
                        await Task.Delay(delay, ct);
                        continue;
                    }
                    _log.LogError("{Provider} {Status}: giving up after {Max} retries for {Question}",
                        _config.AiProvider, status, backoffMs.Length, Truncate(market.Question, 40));
                    return null;
                }

                if (status < 200 || status >= 300)
                {
                    _log.LogError("{Provider} HTTP {Status}: {Body}",
                        _config.AiProvider, status, body[..Math.Min(body.Length, 200)]);
                    return null;
                }

                return ParseProviderResponse(body);
            }
            catch (JsonException ex)
            {
                _log.LogDebug("Failed to parse estimate response: {Error}", ex.Message);
                return null;
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                _log.LogDebug("{Provider} call failed: {Error}", _config.AiProvider, ex.Message);
                return null;
            }
        }

        return null;
    }

    private string ActiveModel =>
        !string.IsNullOrEmpty(_config.AiModel) ? _config.AiModel : _config.ClaudeModel;

    private async Task<(int status, string body)> MakeProviderRequestAsync(MarketInfo market, CancellationToken ct)
    {
        var userPrompt = BuildUserPrompt(market);
        return _config.AiProvider.ToLowerInvariant() switch
        {
            "gemini"       => await MakeGeminiRequestAsync(userPrompt, ct),
            "openai"       => await MakeOpenAiCompatRequestAsync("openai", userPrompt, ct),
            "openrouter"   => await MakeOpenAiCompatRequestAsync("openrouter", userPrompt, ct),
            "azure_openai" => await MakeOpenAiCompatRequestAsync("azure_openai", userPrompt, ct),
            _              => await MakeAnthropicRequestAsync(userPrompt, ct),  // default: anthropic
        };
    }

    // ── Anthropic ──────────────────────────────────────────────────────────

    private async Task<(int, string)> MakeAnthropicRequestAsync(string userPrompt, CancellationToken ct)
    {
        var body = JsonSerializer.Serialize(new
        {
            model = ActiveModel,
            max_tokens = _config.MaxEstimateTokens,
            temperature = _config.EnsembleTemperature,
            system = SystemPrompt,
            messages = new[] { new { role = "user", content = userPrompt } }
        });
        var host = string.IsNullOrEmpty(_config.AnthropicApiHost) ? "https://api.anthropic.com" : _config.AnthropicApiHost;
        var req = new HttpRequestMessage(HttpMethod.Post, $"{host}/v1/messages")
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json")
        };
        req.Headers.Add("x-api-key", _config.AnthropicApiKey);
        req.Headers.Add("anthropic-version", "2023-06-01");
        var resp = await _http.SendAsync(req, ct);
        return ((int)resp.StatusCode, await resp.Content.ReadAsStringAsync(ct));
    }

    private static CallResult? ParseAnthropicResponse(string body)
    {
        var doc = JsonDocument.Parse(body);
        var text = doc.RootElement.GetProperty("content")[0].GetProperty("text").GetString()?.Trim() ?? "";
        var usage = doc.RootElement.GetProperty("usage");
        var inputTokens = usage.GetProperty("input_tokens").GetInt32();
        var outputTokens = usage.GetProperty("output_tokens").GetInt32();
        var (prob, reasoning) = ParseProbabilityJson(text);
        return prob < 0 ? null : new CallResult(prob, reasoning, inputTokens, outputTokens);
    }

    // ── OpenAI-compatible (OpenAI, OpenRouter, Azure OpenAI) ──────────────

    private async Task<(int, string)> MakeOpenAiCompatRequestAsync(string provider, string userPrompt, CancellationToken ct)
    {
        string url;
        var model = ActiveModel;
        var req = new HttpRequestMessage(HttpMethod.Post, "");

        if (provider == "openai")
        {
            var host = string.IsNullOrEmpty(_config.OpenAiApiHost) ? "https://api.openai.com" : _config.OpenAiApiHost.TrimEnd('/');
            url = $"{host}/v1/chat/completions";
            req.Headers.Add("Authorization", $"Bearer {_config.OpenAiApiKey}");
        }
        else if (provider == "openrouter")
        {
            url = "https://openrouter.ai/api/v1/chat/completions";
            req.Headers.Add("Authorization", $"Bearer {_config.OpenRouterApiKey}");
        }
        else  // azure_openai
        {
            var endpoint = _config.AzureOpenAiEndpoint.TrimEnd('/');
            var deployment = _config.AzureOpenAiDeployment;
            var version = string.IsNullOrEmpty(_config.AzureOpenAiApiVersion) ? "2024-02-01" : _config.AzureOpenAiApiVersion;
            url = $"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={version}";
            req.Headers.Add("api-key", _config.AzureOpenAiApiKey);
            model = deployment;  // Azure uses deployment name
        }

        var body = JsonSerializer.Serialize(new
        {
            model,
            messages = new[]
            {
                new { role = "system", content = SystemPrompt },
                new { role = "user",   content = userPrompt }
            },
            temperature = _config.EnsembleTemperature,
            max_tokens = _config.MaxEstimateTokens,
        });
        req.RequestUri = new Uri(url);
        req.Content = new StringContent(body, Encoding.UTF8, "application/json");
        var resp = await _http.SendAsync(req, ct);
        return ((int)resp.StatusCode, await resp.Content.ReadAsStringAsync(ct));
    }

    private static CallResult? ParseOpenAiCompatResponse(string body)
    {
        var doc = JsonDocument.Parse(body);
        var text = doc.RootElement
            .GetProperty("choices")[0]
            .GetProperty("message")
            .GetProperty("content")
            .GetString()?.Trim() ?? "";
        var usage = doc.RootElement.TryGetProperty("usage", out var u) ? u : (JsonElement?)null;
        var inputTokens  = usage?.TryGetProperty("prompt_tokens",     out var pt) == true ? pt.GetInt32() : 0;
        var outputTokens = usage?.TryGetProperty("completion_tokens", out var ct2) == true ? ct2.GetInt32() : 0;
        var (prob, reasoning) = ParseProbabilityJson(text);
        return prob < 0 ? null : new CallResult(prob, reasoning, inputTokens, outputTokens);
    }

    // ── Google Gemini ──────────────────────────────────────────────────────

    private async Task<(int, string)> MakeGeminiRequestAsync(string userPrompt, CancellationToken ct)
    {
        var model = ActiveModel;
        var url = $"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={_config.GeminiApiKey}";
        var body = JsonSerializer.Serialize(new
        {
            systemInstruction = new { parts = new[] { new { text = SystemPrompt } } },
            contents = new[] { new { role = "user", parts = new[] { new { text = userPrompt } } } },
            generationConfig = new
            {
                temperature = _config.EnsembleTemperature,
                maxOutputTokens = _config.MaxEstimateTokens,
            }
        });
        var req = new HttpRequestMessage(HttpMethod.Post, url)
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json")
        };
        var resp = await _http.SendAsync(req, ct);
        return ((int)resp.StatusCode, await resp.Content.ReadAsStringAsync(ct));
    }

    private static CallResult? ParseGeminiResponse(string body)
    {
        var doc = JsonDocument.Parse(body);
        var text = doc.RootElement
            .GetProperty("candidates")[0]
            .GetProperty("content")
            .GetProperty("parts")[0]
            .GetProperty("text")
            .GetString()?.Trim() ?? "";
        var meta = doc.RootElement.TryGetProperty("usageMetadata", out var m) ? m : (JsonElement?)null;
        var inputTokens  = meta?.TryGetProperty("promptTokenCount",     out var pt) == true ? pt.GetInt32() : 0;
        var outputTokens = meta?.TryGetProperty("candidatesTokenCount", out var ct2) == true ? ct2.GetInt32() : 0;
        var (prob, reasoning) = ParseProbabilityJson(text);
        return prob < 0 ? null : new CallResult(prob, reasoning, inputTokens, outputTokens);
    }

    // ── Response parsing (shared) ──────────────────────────────────────────

    private CallResult? ParseProviderResponse(string body)
    {
        return _config.AiProvider.ToLowerInvariant() switch
        {
            "gemini"       => ParseGeminiResponse(body),
            "openai"       => ParseOpenAiCompatResponse(body),
            "openrouter"   => ParseOpenAiCompatResponse(body),
            "azure_openai" => ParseOpenAiCompatResponse(body),
            _              => ParseAnthropicResponse(body),
        };
    }

    /// <summary>
    /// Parse {"probability": 0.XX, "reasoning": "..."} from model response text.
    /// Returns (prob, reasoning) where prob = -1 signals parse failure.
    /// </summary>
    private static (double prob, string reasoning) ParseProbabilityJson(string text)
    {
        try
        {
            if (text.StartsWith("```"))
            {
                var lines = text.Split('\n').Where(l => !l.TrimStart().StartsWith("```")).ToArray();
                text = string.Join('\n', lines);
            }
            var doc = JsonDocument.Parse(text);
            var prob = doc.RootElement.GetProperty("probability").GetDouble();
            var reasoning = doc.RootElement.TryGetProperty("reasoning", out var r) ? r.GetString() ?? "" : "";
            return (Math.Clamp(prob, 0.02, 0.98), reasoning);
        }
        catch
        {
            return (-1, "");
        }
    }

    private static string BuildUserPrompt(MarketInfo market)
    {
        var desc = market.Description.Length > 500
            ? market.Description[..500]
            : market.Description;
        if (string.IsNullOrEmpty(desc)) desc = "N/A";

        return $"Market: {market.Question}\n" +
            $"Event: {market.EventTitle}\n" +
            $"Description: {desc}\n" +
            $"Category: {market.Category}\n" +
            $"Resolution date: {(string.IsNullOrEmpty(market.EndDate) ? "Unknown" : market.EndDate)}\n" +
            $"Current market price: YES at {market.OutcomeYesPrice:P0} / NO at {market.OutcomeNoPrice:P0}\n" +
            "\n" +
            "Estimate the probability this resolves YES. Output JSON only.";
    }

    private static double StdDev(List<double> values)
    {
        var mean = values.Average();
        var sumSq = values.Sum(v => (v - mean) * (v - mean));
        return Math.Sqrt(sumSq / (values.Count - 1));
    }

    /// <summary>
    /// Makes a minimal test call to validate the configured provider's API key.
    /// Returns false on auth failure (HTTP 401/403). Network/rate-limit errors return true (don't block startup).
    /// </summary>
    public async Task<bool> ValidateApiKeyAsync(CancellationToken ct = default)
    {
        try
        {
            var provider = _config.AiProvider.ToLowerInvariant();
            HttpRequestMessage req;

            if (provider == "gemini")
            {
                var model = ActiveModel;
                var url = $"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={_config.GeminiApiKey}";
                var body = JsonSerializer.Serialize(new
                {
                    contents = new[] { new { parts = new[] { new { text = "hi" } } } },
                    generationConfig = new { maxOutputTokens = 1 }
                });
                req = new HttpRequestMessage(HttpMethod.Post, url)
                {
                    Content = new StringContent(body, Encoding.UTF8, "application/json")
                };
            }
            else if (provider is "openai" or "openrouter" or "azure_openai")
            {
                var (url, authHeader, authValue) = provider switch
                {
                    "openai" => ($"{(_config.OpenAiApiHost.TrimEnd('/'))}/v1/chat/completions", "Authorization", $"Bearer {_config.OpenAiApiKey}"),
                    "openrouter" => ("https://openrouter.ai/api/v1/chat/completions", "Authorization", $"Bearer {_config.OpenRouterApiKey}"),
                    _ => ($"{_config.AzureOpenAiEndpoint.TrimEnd('/')}/openai/deployments/{_config.AzureOpenAiDeployment}/chat/completions?api-version={_config.AzureOpenAiApiVersion}", "api-key", _config.AzureOpenAiApiKey),
                };
                var body = JsonSerializer.Serialize(new
                {
                    model = provider == "azure_openai" ? _config.AzureOpenAiDeployment : ActiveModel,
                    messages = new[] { new { role = "user", content = "hi" } },
                    max_tokens = 1
                });
                req = new HttpRequestMessage(HttpMethod.Post, url)
                {
                    Content = new StringContent(body, Encoding.UTF8, "application/json")
                };
                req.Headers.Add(authHeader, authValue);
            }
            else  // anthropic
            {
                var host = string.IsNullOrEmpty(_config.AnthropicApiHost) ? "https://api.anthropic.com" : _config.AnthropicApiHost;
                var body = JsonSerializer.Serialize(new
                {
                    model = ActiveModel,
                    max_tokens = 1,
                    messages = new[] { new { role = "user", content = "hi" } }
                });
                req = new HttpRequestMessage(HttpMethod.Post, $"{host}/v1/messages")
                {
                    Content = new StringContent(body, Encoding.UTF8, "application/json")
                };
                req.Headers.Add("x-api-key", _config.AnthropicApiKey);
                req.Headers.Add("anthropic-version", "2023-06-01");
            }

            var resp = await _http.SendAsync(req, ct);
            var status = (int)resp.StatusCode;

            if (status == 401 || status == 403)
            {
                var respBody = await resp.Content.ReadAsStringAsync(ct);
                _log.LogError("{Provider} API key invalid (HTTP {Status}): {Body}",
                    _config.AiProvider, status, respBody[..Math.Min(respBody.Length, 200)]);
                return false;
            }
            // Gemini returns 400 for invalid keys — check response body
            if (provider == "gemini" && status == 400)
            {
                var respBody = await resp.Content.ReadAsStringAsync(ct);
                if (respBody.Contains("API key", StringComparison.OrdinalIgnoreCase))
                {
                    _log.LogError("Gemini API key invalid: {Body}", respBody[..Math.Min(respBody.Length, 200)]);
                    return false;
                }
            }
            return true;
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            _log.LogWarning("API key validation network error: {Error} — continuing", ex.Message);
            return true;
        }
    }

    private static string Truncate(string s, int maxLen)
        => s.Length <= maxLen ? s : s[..maxLen] + "...";

    private readonly record struct CallResult(double Probability, string Reasoning, int InputTokens, int OutputTokens);
}
