# Providers

What each vendor's API looked like when this was built, where it came from, and where the build differs from the original plan. Checked on 2026-09-26. Vendors change model IDs, prices, and request formats; re-check the linked pages when you update `config/models.yaml`.

SDK versions at the time: `anthropic` 1.8.0, `openai` 3.19.2, `google-genai` 2.25.0.

## Summary

| Key | Model | API | Search tool | Input / cached / output per 1M | Per search |
|---|---|---|---|---|---|
| `claude` | `claude-opus-5-5` | Anthropic Messages | `web_search_20260318` | $4.00 / $0.20 / $20.00 | $0.010 |
| `chatgpt` | `gpt-6-sol` | OpenAI Responses | `{"type": "web_search"}` | $2.00 / $0.20 / $10.00 | $0.010 |
| `gemini` | `gemini-3.8-flash` | Google Interactions | `{"type": "google_search"}` | $0.75 / $0.075 / $3.75 | $0.014 |
| `muse` | `muse-spark-1.3` | Meta Responses (OpenAI-compatible) | `{"type": "web_search"}` | $1.25 / $0.15 / $4.25 | $0.0025 |

Every provider runs statelessly: the bot sends the full system prompt and transcript on each call and stores nothing on the vendor side (`store: false` where the API offers it).

## Claude (Anthropic)

Sources:
- Models: https://platform.claude.com/docs/en/about-claude/models/overview
- Opus 5.5: https://platform.claude.com/docs/en/models/opus-5-5/overview
- Pricing: https://platform.claude.com/docs/en/about-claude/pricing
- Web search tool: https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
- Prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Refusals and fallback: https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback

Findings:
- Model `claude-opus-5-5`. The models overview says to start with Opus 5.5 for most workloads. The current lineup also has `claude-fable-5-1` (most capable, $10 / $50), `claude-sonnet-5`, and `claude-haiku-4-5`.
- Adaptive thinking is always on for Opus 5.5 and cannot be disabled. `budget_tokens` is rejected with a 400. Depth is set with `output_config.effort` (`low` to `max`, default `medium` on this model), exposed as `effort` in the config.
- The system prompt goes in `system`. Assistant prefill is not allowed, which the bot never needs.
- Web search: `{"type": "web_search_20260318", "name": "web_search", "max_uses": 5}`. This version runs dynamic filtering (the model filters results with code before reading them). Older models need `web_search_20250305`. Search must be enabled for the organization in the Claude Console; it is on unless an admin turned it off.
- Citations arrive on `text` blocks as `web_search_result_location` objects with `url`, `title`, and `cited_text`.
- Usage fields: `input_tokens` (uncached), `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`, and `server_tool_use.web_search_requests`.
- Prices: $4 input, $20 output, $5 for 5-minute cache writes, $0.20 for cache reads, per 1M tokens. Search costs $10 per 1,000 searches plus tokens.
- Minimum cacheable prefix for Opus 5.5 is 512 tokens. Shorter prompts are not cached and no error is returned.
- A long search can end with `stop_reason: "pause_turn"`. The provider sends the paused turn back unchanged, up to 3 times.
- A declined request ends with `stop_reason: "refusal"`. The config sets `fallbacks: default`, which asks the API to rerun a declined request on a fallback model it picks (beta `server-side-fallback-2026-07-01`). Set `fallbacks: null` to turn this off.

## ChatGPT (OpenAI)

Sources:
- Models: https://developers.openai.com/api/docs/models
- Pricing: https://developers.openai.com/api/docs/pricing
- Web search: https://developers.openai.com/api/docs/guides/tools-web-search

Findings:
- Current models are `gpt-6-astra` ($10 / $50, most capable), `gpt-6-sol` ($2 / $10, balanced), and `gpt-6-luna` ($0.10 / $0.50). The config uses `gpt-6-sol`. OpenAI describes Astra as the choice for the hardest work; switch the `model` field if you want it.
- The Responses API is used because web search needs it. Instructions go in as a `developer` message.
- Web search: `{"type": "web_search"}`. Each search appears as a `web_search_call` output item. Citations are `url_citation` annotations (`url`, `title`, `start_index`, `end_index`) on `output_text` parts.
- Usage fields: `input_tokens` (includes cached), `input_tokens_details.cached_tokens`, `output_tokens` (includes reasoning).
- `max_output_tokens` includes reasoning tokens. Reasoning depth is `reasoning.effort`, exposed as `reasoning_effort` in the config and left at the model default.
- Web search costs $10 per 1,000 calls plus search content tokens at model rates.

## Gemini (Google)

Sources:
- Models: https://ai.google.dev/gemini-api/docs/models
- Pricing: https://ai.google.dev/gemini-api/docs/pricing
- Interactions API: https://ai.google.dev/gemini-api/docs/interactions
- Thinking: https://ai.google.dev/gemini-api/docs/thinking
- Grounding with Google Search: https://ai.google.dev/gemini-api/docs/google-search

Findings:
- `gemini-3.8-flash` is the newest stable model. The only current Pro model is `gemini-3.1-pro-preview`, which is preview-only and older, so the config uses Flash.
- Google made the Interactions API generally available in June 2026, recommends it for new projects, and calls `generateContent` legacy but still supported. The bot uses Interactions (`client.aio.interactions.create`) in stateless mode (`store=False`), with history sent as `user_input` and `model_output` steps.
- System prompt: `system_instruction`. Output limit and thinking: `generation_config.max_output_tokens` and `generation_config.thinking_level` (`minimal`, `low`, `medium`, `high`; default `medium`). Thinking cannot be turned off, and thinking tokens count against `max_output_tokens` and are billed as output. The llm-discussion fix (`thinking_budget=0`) does not apply to Gemini 3; the bot sets a large `max_output_tokens` instead.
- Web search: `{"type": "google_search"}`. Citations are `url_citation` annotations on text content. Usage reports searches in `grounding_tool_count`.
- Usage fields: `total_input_tokens`, `total_cached_tokens`, `total_output_tokens`, `total_thought_tokens`.
- Prices are promotional through 2026-12-31 and double on 2027-01-01 (input $1.50, output $7.50, cached $0.15).
- Search: 5,000 free queries per month shared across Gemini 3.x models, then $14 per 1,000. The cost estimate counts every search as paid.

## Muse Spark (Meta)

Sources:
- Models: https://dev.meta.ai/docs/models
- Pricing and rate limits: https://dev.meta.ai/docs/pricing-rate-limits
- Search grounding: https://dev.meta.ai/docs/search-grounding
- Reasoning: https://dev.meta.ai/docs/reasoning

Findings:
- `muse-spark-1.3` is the recommended model. Base URL `https://api.meta.ai/v1`, used with the `openai` SDK.
- Meta serves Muse Spark on three protocols: Responses, Chat Completions, and an Anthropic-style Messages API.
- Search grounding is available only on the Responses API. The docs state it is not available through Chat Completions. So Muse Spark uses the same Responses code as ChatGPT (`providers/openai.py`), with a different `base_url` and key.
- Web search: `{"type": "web_search"}`. Results come back as `web_search_call` items and `url_citation` annotations, the same shapes as OpenAI.
- Muse Spark always reasons. `reasoning.effort` accepts `minimal` through `max`; `none` returns a 400. Reasoning tokens count against `max_output_tokens`.
- Prices (Standard tier): $1.25 input, $0.15 cached, $4.25 output per 1M tokens. Search costs $2.50 per 1,000 queries. A cheaper `muse-spark-1.3-contributor` tier lets Meta train on your prompts; the config does not use it.
- Meta adds its own steering prompt to every request. Those tokens are not billed or reported.

## Differences from the build plan

- **Gemini API.** The plan assumed `generate_content`. Google's docs now recommend the Interactions API for new work, so the bot uses it.
- **Meta endpoint.** The plan left open whether Meta's endpoint supports Responses or Chat Completions, and asked the smoke test to try both. Meta's docs answer it: both exist, but search works only on Responses. The smoke test checks Responses only, and there is no separate `providers/meta.py`; Meta is an `openai_responses` provider in `models.yaml`.
- **Anthropic web search version.** `web_search_20260318` is the newest version in the docs, with dynamic filtering.
- **Reply fields.** `Reply` has an extra `cache_write_tokens` field so Anthropic cache writes (billed above the input rate) are costed correctly.
- **Cache breakpoints.** `generate()` takes `cache_breakpoints: list[int]` instead of `cache_after_index`, so Anthropic can mark both the submission and the end of the transcript.
