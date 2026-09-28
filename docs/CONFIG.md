# Configuration

waggle-dance reads three kinds of settings:

- `.env`: Discord IDs, API keys, and container settings. Read at startup.
- `config/models.yaml`: the models, their prices, and discussion limits. Reread by `/reload`.
- `instructions/`: text added to every model's system prompt. Reread by `/reload`.

`config/models.yaml` and `instructions/shared.md` are created from the `.example` files on first start and are not tracked by git, so your edits survive `git pull`.

---

## `.env`

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | | The bot token from the Developer Portal |
| `DISCORD_GUILD_ID` | yes | | Your server ID. Slash commands are registered in this server only |
| `DISCORD_CHANNEL_ID` | yes | | The one text channel the bot works in |
| `ALLOWED_USER_IDS` | yes | | Comma-separated Discord user IDs that can use the bot. Everyone else is ignored |
| `OWNER_NAME` | no | `Owner` | The name the models use for you. Messages from every allowed user are labeled with this name |
| `ANTHROPIC_API_KEY` | if Claude is enabled | | Anthropic API key |
| `OPENAI_API_KEY` | if ChatGPT is enabled | | OpenAI API key |
| `GEMINI_API_KEY` | if Gemini is enabled | | Google Gemini API key |
| `META_API_KEY` | if Muse Spark is enabled | | Meta API key |
| `AUTO_CLOSE_HOURS` | no | `24` | Close the open conversation after this many hours without activity |
| `MOCK_MODELS` | no | `0` | `1` replaces every model with canned replies, for testing without API costs |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `APP_UID`, `APP_GID` | no | `1000` | User and group the container runs as. Match the owner of the project directory on the host. Changing these needs `docker compose up -d --build` |
| `TZ` | no | `UTC` | Time zone for the date the models are told, for example `America/Los_Angeles` |

The key variable names are not fixed: each model's `api_key_env` in `models.yaml` names the variable it reads.

After editing `.env`, run `docker compose up -d`. A plain `docker compose restart` does not reread it.

---

## `config/models.yaml`

Run `/reload` in Discord after editing. If the file has an error, `/reload` reports it and keeps the old configuration. At startup, an error stops the bot and is printed in the log.

### Top-level settings

| Field | Default | Meaning |
|---|---|---|
| `max_reply_chars` | `2000` | Reply length limit, stated to the models in their instructions. URLs do not count. Models mostly respect it; the bot does not cut replies off |
| `session_cost_warning` | `5.00` | Post a spend warning before a round that would take the conversation's estimated cost past this many dollars |
| `timeout_seconds` | `120` | How long to wait for one model call. Searches are slow. A model that times out posts an error, and the round continues without it |
| `style_filters` | `[]` | Regex replacements applied to model replies before posting. See below |

### Per-model settings

Each entry under `models:` has a key (`claude`, `chatgpt`, `gemini`, `muse`). The key names the model in `/instructions` and the per-model instruction file.

| Field | Providers | Meaning |
|---|---|---|
| `enabled` | all | `false` leaves the model out of new conversations. Its API key is then not needed |
| `display_name` | all | The name shown in Discord and used in the discussion |
| `provider` | all | `anthropic`, `openai_responses`, or `gemini`. See [Adding a provider](#adding-an-openai-compatible-provider) |
| `model` | all | The vendor's model ID, exactly as their API expects it |
| `api_key_env` | all | The `.env` variable that holds this model's key |
| `base_url` | `openai_responses` | API address. Empty means OpenAI. Muse Spark uses `https://api.meta.ai/v1` |
| `avatar_url` | all | Image URL for the model's Discord avatar. Empty uses the webhook's default avatar |
| `color` | all | Required, in `#RRGGBB` form. Not currently shown in Discord |
| `max_tokens` | all | Output limit per call, including thinking or reasoning tokens. Keep it large (16000) or thinking can use it all and leave an empty reply |
| `timeout_seconds` | all | Overrides the top-level timeout for this model |
| `effort` | `anthropic` | `low`, `medium`, `high`, `xhigh`, or `max`. `null` uses the model default |
| `fallbacks` | `anthropic` | `default` lets Anthropic rerun a declined request on a fallback model. `null` turns this off |
| `search.tool` | `anthropic` | Required. The web search tool version, for example `web_search_20250305` |
| `search.max_uses` | `anthropic` | Most searches per call |
| `reasoning_effort` | `openai_responses` | Passed to the API as `reasoning.effort`. `null` uses the model default |
| `thinking_level` | `gemini` | `minimal`, `low`, `medium`, or `high`. `null` uses the model default |
| `prices.input` | all | Dollars per million uncached input tokens |
| `prices.cached_input` | all | Dollars per million cached input tokens |
| `prices.cache_write` | `anthropic` | Dollars per million tokens written to Anthropic's prompt cache. Defaults to `prices.input` |
| `prices.output` | all | Dollars per million output tokens, including reasoning |
| `prices.per_search` | all | Dollars per web search, for example `0.01` for $10 per 1,000 |

Prices only feed `/cost` and the spend warning. The vendors bill from their own price lists, so update these when prices change.

### Style filters

Each filter is a regex `pattern` and a `replace` string, applied in order to every model reply before it is posted and saved. For example, to replace em dashes with a comma:

```yaml
style_filters:
  - pattern: "\\s*\u2014\\s*"
    replace: ", "
```

---

## Switching a model

1. Find the exact model ID in the vendor's documentation, or list the IDs your key can use. For OpenAI:

   ```bash
   docker compose exec waggle-dance python -c "import os, openai; print('\n'.join(sorted(m.id for m in openai.OpenAI(api_key=os.environ['OPENAI_API_KEY']).models.list())))"
   ```

2. Change `model:` and all of the `prices` fields to match the new model.
3. Run `/reload`. It applies from the next model call, including in the open conversation.
4. Run `/models` to confirm the new ID, and optionally `docker compose exec waggle-dance python scripts/smoke_models.py --only chatgpt` to make one real call.

A wrong model ID is not caught by `/reload`. It shows up as an error in the channel the first time the model is called. Older models may also lack web search or a reasoning setting, which fails the same way.

---

## Instruction files

Each model's system prompt is built from three layers, in this order:

1. `instructions/shared.md`: your text for every model, for example who you are and what you want from the discussion.
2. `instructions/models/<key>.md`: your text for one model only, for example `instructions/models/gemini.md`. Optional.
3. The discussion rules, written by the bot: the model's name and the other participants, today's date, the reply length limit, how to treat your messages, and when to cite sources. These live in `waggle_dance/prompts.py`.

Run `/reload` after editing, then `/instructions` (or `/instructions gemini`) to see the full prompt a model receives.

---

## Adding an OpenAI-compatible provider

A vendor that offers an OpenAI-style Responses API, with the `web_search` tool, can be added without code. Muse Spark works this way.

1. Add an entry under `models:` with `provider: openai_responses`, the vendor's `base_url`, a new `api_key_env`, its `model`, and its `prices`.
2. Add the key to `.env` under that variable name and run `docker compose up -d`.
3. Run `scripts/smoke_models.py --only <key>` to check it.

A vendor with only a Chat Completions API does not work as-is: the bot's search and citation handling expects the Responses API. See [PROVIDERS.md](PROVIDERS.md) for how each current vendor differs.
