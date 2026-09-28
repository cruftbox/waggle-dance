# Changelog

## 0.1.0 (2026-09-28)

First release.

- Claude, ChatGPT, Gemini, and Muse Spark discuss a topic in one Discord channel, each posting under its own name through a webhook.
- A plain message starts a conversation, with optional `.txt`, `.md`, or `.pdf` attachments. Later messages are follow-ups that every model answers in turn.
- The opening round is blind; follow-ups are sequential, with a rotating first speaker.
- Messages sent while the models are replying are queued and answered together when they finish.
- Web search is on for every model, with cited sources.
- Commands: `/new`, `/review`, `/disagree`, `/vote`, `/consensus`, `/cost`, `/export`, `/close`, `/summarize`, `/models`, `/instructions`, `/reload`, `/help`.
- `/review` reads a blog or social media post and up to 5 pages it links to, and the models critique it as blog editors.
- `/vote` tallies ranked ballots with a Borda count in code.
- Transcripts are saved as Markdown and PDF when a conversation closes, and attached by `/export` and `/summarize`.
- Cost estimates per conversation, with a configurable spend warning.
- Conversations auto-close after 24 hours without activity, and the open conversation survives restarts.
- Models, prices, reply length, and instructions are set in `config/models.yaml` and `instructions/`, and reloaded with `/reload`.
- Runs in Docker, with a mock mode for testing without API costs.
