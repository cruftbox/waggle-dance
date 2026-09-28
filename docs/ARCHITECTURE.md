# Architecture

How a conversation runs inside the bot, for someone changing the code.

## Modules

| Module | Role |
|---|---|
| `main.py` | Startup: loads settings, config, instructions, providers, and the database, then runs the bot. Defines `/reload`'s config reload |
| `bot.py` | The Discord client: channel messages, background runs, the follow-up queue, closing, and auto-close |
| `commands.py` | Slash commands and the `/help` text |
| `orchestrator.py` | The conversation logic: opening, follow-ups, disagree, vote, consensus, close, exports, and cost reports. It never talks to Discord directly |
| `discord_io.py` | Posting through the webhook as each model, splitting long replies, and the sources line |
| `transcript.py` | Transcript entries and how each model's view of them is built |
| `prompts.py` | Every prompt the models see |
| `providers/` | One class per API: `anthropic.py`, `openai.py` (OpenAI and Meta), `gemini.py`, and `mock.py` |
| `store.py` | SQLite storage in `data/waggle.db` |
| `ingest.py` | Reading attachments (`.txt`, `.md`, `.pdf`) and web pages for `/review` |
| `voting.py` | Parsing ballots and the Borda count |
| `costs.py` | Cost estimates from token counts and configured prices |
| `pdf.py` | Laying out the Markdown transcript as a PDF |
| `config.py` | Loading and validating `.env`, `models.yaml`, and instruction files |

The orchestrator posts through an `Output` interface. The bot implements it with the Discord webhook, and the tests implement it with lists, so the conversation logic is tested without Discord.

## A conversation

1. **Start.** With no conversation open, a plain message from an allowed user in the channel starts one. The message text is the topic, and any attached files become the submission. `/new topic` and `/review url` start one the same way. Starting a conversation closes any open one first.
2. **Opening round.** Every enabled model answers the submission at the same time. Each model's view is built before any call starts, so none sees another's answer. This keeps the first takes independent: a model that sees an answer first tends to agree with it or argue against it, instead of forming its own view.
3. **Follow-ups.** Each later plain message from the owner is added to the transcript, and the models reply one after another. Each model sees every reply posted before its own, so later models respond to earlier ones. The first speaker rotates with each follow-up so no model always goes first or last.
4. **Commands.** `/disagree`, `/vote`, and `/consensus` add their output to the same transcript, so the models can refer to it later.
5. **Close.** `/close`, `/summarize`, `/new`, or 24 hours without activity mark the conversation closed, write the Markdown and PDF exports to `data/exports/`, and drop it from memory. `/summarize` first runs a consensus and then posts the closing note and the files.

A cheap model names each conversation in the background when it starts. The name is used for export file names.

## One command at a time

Each conversation has a lock, and only one command or round runs at a time. The bot counts a conversation as busy from the moment it starts a command, before the command takes the lock, so a message or slash command arriving in between cannot slip ahead of it.

A plain message that arrives while a round or command is running goes into a queue (👀). When the running work finishes, the bot adds every queued message to the transcript and runs one follow-up round for all of them, still holding the lock. Slash commands that arrive while the conversation is busy are refused with a private "Busy" reply, except `/cost` and `/export`, which only read. A message that arrives while the conversation is closing is not recorded (⏳).

The queue is kept in memory, so a restart loses queued messages.

## What each model sees

The transcript is one list of entries: the owner's messages, each model's replies, and moderator notes. For each call, `transcript.build_view` turns it into that model's conversation:

- The first user message is the submission: the topic, or the post and linked pages for a review.
- The model's own earlier replies are assistant messages.
- Everything else is a user message labeled with the speaker, like `[Gemini]: ...` or `[Michael]: ...`. Consecutive user messages are merged by the providers.
- The last user message is the instruction for this call, for example "reply to the latest message" or "list only the disagreements".

The system prompt is built fresh for each call from the instruction files and the discussion rules (see [CONFIG.md](CONFIG.md#instruction-files)).

Every call is stateless: the full system prompt and transcript go to the vendor each time, and nothing is stored on the vendor's side. The submission and the end of the transcript are marked for Anthropic's prompt cache. OpenAI, Meta, and Gemini cache repeated prefixes on their own. Anthropic's cache lasts 5 minutes, so a follow-up after a longer pause pays to write the cache again.

## Summaries and votes

`/disagree`, `/consensus`, `/summarize`, and the candidate list for `/vote` each go to one model, chosen in rotation. The rotation counters are stored in the database, so they carry across conversations and restarts.

For `/vote`, the chosen model lists the candidate positions (or proposed edits, in a review) as JSON. Every model then ranks them as JSON, and the bot tallies a Borda count in code. A reply that is not valid JSON is retried once with the parse error. A model whose ballot still fails is left out of the tally and listed as dropped.

## Storage

`data/waggle.db` has four tables:

- `sessions`: one row per conversation, with its topic, submission, settings, status, and timestamps.
- `entries`: every transcript entry, with citations and the Discord message IDs it was posted as.
- `usage`: one row per model call, with token counts, search count, and estimated cost.
- `meta`: counters, including the summarizer rotation.

At startup, the open conversation is loaded back from the database and continues where it left off.

## Costs

Each provider reports token usage, and `costs.reply_cost` prices it with the `prices` in `models.yaml`. Anthropic reports cache writes separately from input, and they are priced at `cache_write`. Search calls are counted from each API's usage fields and priced at `per_search`.

Before each round or command, the orchestrator estimates its cost from the conversation's average cost per call so far (or a default guess for the first call). If the total would pass `session_cost_warning`, it posts a warning and proceeds.
