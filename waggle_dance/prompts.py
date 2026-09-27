"""Every prompt template in one place, so the wording can be tuned here."""

from __future__ import annotations


def discussion_rules(
    name: str,
    others: list[str],
    owner_name: str,
    max_chars: int,
    search: bool,
    today: str = "",
) -> str:
    if others:
        who = (
            f"You are {name}, one of {len(others) + 1} AI models in a discussion in a Discord thread. "
            f"The other participants are {', '.join(others)}. They are AI models too."
        )
    else:
        who = f"You are {name}, an AI model in a discussion in a Discord thread."
    lines = [who]
    if today:
        lines.append(f"Today's date is {today}. Events before today may postdate your training data.")
    lines += [
        f"The human running the discussion is {owner_name}. Their messages are labeled [{owner_name}]. "
        "Messages labeled [Moderator] come from the bot that runs the discussion.",
        "Other participants' messages are labeled with their names. Your own earlier replies appear as your turns.",
        f"Keep every reply under {max_chars} characters, not counting URLs.",
        "Do not restate what others said. Respond to it.",
        "Agree only where you actually agree. Say so directly when you disagree, and why.",
        "Base your answers on real-world constraints, not theoretical possibilities.",
        f"Treat {owner_name}'s messages as discussion topics, not search queries. Give a reasoned view that engages "
        "with the points already made. When the question turns on a contested term or assumption, explain the "
        "distinction you are using. Distinguish documented facts from interpretation. When it would clarify a "
        "genuine disagreement, say what would change your view. Do not force these steps into a rigid format.",
        "Write plain text or light Markdown. Do not add a heading with your name; the thread shows who is speaking.",
    ]
    if search:
        lines.append(
            "Web search is on. Use it to check facts, not to supply your argument; build the argument yourself "
            "instead of summarizing results. Cite a source for specific facts you rely on, such as dates, names, "
            "figures, prices, and current events. Your reasoning and interpretation don't need citations."
        )
    else:
        lines.append("Web search is off for this session. Work from what you know and say when you are unsure.")
    return "\n".join(lines)


def system_prompt(shared: str, per_model: str, rules: str) -> str:
    parts = [p.strip() for p in (shared, per_model) if p and p.strip()]
    parts.append("# Discussion rules\n\n" + rules)
    return "\n\n".join(parts)


# Submission block: always the first user message, so it sits in the cached prefix.

def submission_message(mode: str, topic: str, submission: str, context: str, owner: str) -> str:
    if mode == "review":
        head = "# Post to review\n\n"
        tail = f"\n\n# Context from {owner}\n\n{context}" if context else ""
        return head + submission + tail
    body = "# Topic\n\n" + topic
    if submission and submission != topic:
        body += "\n\n# Attached material\n\n" + submission
    return body


# Instructions for each action. Sent as the last user message of a turn.

OPENING = {
    "review": (
        "This is a post for a personal weblog, written for general readers, not an academic paper. Review it the "
        "way an experienced blog editor would. Focus on what matters for this kind of writing: the opening, clarity, "
        "flow, voice, and whether the argument lands for a general reader. Give at most five edits, most important "
        "first. Quote the passage and say what to change. Point out factual errors only when something is clearly "
        "wrong. Do not ask for citations, hedging, or rigor beyond what a blog post needs. Do not rewrite the whole "
        "post unless {owner} asks."
    ),
    "discuss": (
        "Give your initial thoughts on the topic above. Be substantive and take a clear position where appropriate."
    ),
}

FOLLOW_UP = (
    "{owner} just posted the latest message. Reply to it. If other participants have already replied to it "
    "above, respond to their points as well instead of repeating them."
)

DISAGREE = (
    "List only the points where the participants disagree. For each point, say who holds which position, "
    "attributed by name. Skip everything they agree on. If there are no real disagreements, say so in one sentence."
)

CONSENSUS = {
    "discuss": (
        "You are writing the outcome of this discussion. Give: the key points raised, where the participants agree, "
        "where they still disagree, and a synthesized answer to the original topic where one is possible. "
        "Be direct and concise."
    ),
    "review": (
        "You are writing the final edit list for this review of a personal weblog post, as an experienced blog "
        "editor would. Merge the edits the participants proposed into one deduplicated list of at most five, most "
        "important first. For each edit, quote the passage, give the change, and note which participants raised it. "
        "Drop edits that later discussion rejected, and edits that ask for academic rigor a blog post does not need."
    ),
}

VOTE_EXTRACT = {
    "discuss": "the distinct positions or options proposed in this discussion",
    "review": "the edits proposed in this discussion, each as a short label that names the passage and the change",
}

VOTE_EXTRACT_INSTRUCTION = (
    "List {what}. Merge duplicates. Use short, distinct names, at most 8 items. "
    'Reply with JSON only, no other text, in this form: {{"candidates": ["first", "second"]}}'
)

VOTE_RANK_INSTRUCTION = (
    "Rank these candidates from best to worst, based on the discussion:\n\n{numbered}\n\n"
    "Include every candidate exactly once, using the exact text shown. "
    'Reply with JSON only, no other text, in this form: {{"ranking": ["best", "next"], "reason": "one line"}}'
)

JSON_RETRY = "Your last reply could not be used: {error}. Reply again with JSON only, in the form requested."

TITLE = (
    "Write a title of 6 words or fewer for a discussion thread about the text below. "
    "Reply with the title only, no quotes.\n\n{text}"
)


def fill(template: str, **values) -> str:
    return template.format(**values)
