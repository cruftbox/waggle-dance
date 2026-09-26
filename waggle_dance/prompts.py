"""Every prompt template in one place, so the wording can be tuned here."""

from __future__ import annotations

ROLE_PRESETS = {
    "skeptic": "the skeptic. Look for weak claims, missing evidence, and risks the others are glossing over. "
    "Say plainly when something does not hold up.",
    "advocate": "the advocate. Make the strongest honest case for the ideas on the table, "
    "and say what would have to be true for them to work.",
    "editor": "the editor. Focus on clarity, structure, word choice, and cutting what is not needed.",
    "target reader": "the target reader. React as the intended audience would: what you understand, "
    "what loses you, and what you would do next.",
}


def role_text(role: str | None) -> str:
    if not role:
        return ""
    preset = ROLE_PRESETS.get(role.strip().lower())
    return f"Your role for the rest of this session: you are {preset or role.strip() + '.'}"


def discussion_rules(
    name: str,
    others: list[str],
    owner_name: str,
    max_chars: int,
    search: bool,
    role: str | None,
) -> str:
    if others:
        who = (
            f"You are {name}, one of {len(others) + 1} AI models in a discussion in a Discord thread. "
            f"The other participants are {', '.join(others)}. They are AI models too."
        )
    else:
        who = f"You are {name}, an AI model in a discussion in a Discord thread."
    lines = [
        who,
        f"The human running the discussion is {owner_name}. Their messages are labeled [{owner_name}]. "
        "Messages labeled [Moderator] come from the bot that runs the discussion.",
        "Other participants' messages are labeled with their names. Your own earlier replies appear as your turns.",
        f"Keep every reply under {max_chars} characters, not counting URLs.",
        "Do not restate what others said. Respond to it.",
        "Agree only where you actually agree. Say so directly when you disagree, and why.",
        "Write plain text or light Markdown. Do not add a heading with your name; the thread shows who is speaking.",
    ]
    if search:
        lines.append(
            "Web search is on. Cite a source URL for every factual claim about a product, price, or current event."
        )
    else:
        lines.append("Web search is off for this session. Work from what you know and say when you are unsure.")
    if role:
        lines.append(role_text(role))
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
    if mode == "recommend":
        return "# What is needed\n\n" + submission + ("\n\n# Constraints\n\n" + context if context else "")
    body = "# Topic\n\n" + topic
    if submission and submission != topic:
        body += "\n\n# Attached material\n\n" + submission
    return body


# Instructions for each action. Sent as the last user message of a turn.

OPENING = {
    "review": (
        "Review the post above. Give specific edits, ranked from most to least important. For each edit, "
        "quote the passage you would change, say what to change it to or why, and mark its severity as "
        "must fix, should fix, or optional. Do not rewrite the whole post unless {owner} asks."
    ),
    "recommend": (
        "Use web search to propose a shortlist of up to 3 products that meet the need and every constraint above. "
        "For each product give the name, the current price, a one-line reason, and a source URL for the price or "
        "product page. If you cannot find a source for a product, write NO SOURCE in place of the URL."
    ),
    "discuss": (
        "Give your initial thoughts on the topic above. Be substantive and take a clear position where appropriate."
    ),
}

DEBATE = (
    "It is your turn in the debate (round {round}). Respond to the points made so far, especially the most recent "
    "ones. Push back where you disagree, concede where you were wrong, and add anything important that is missing."
)

ASK = "{owner} asked you a question in the latest message. Answer it directly."

ASK_WITH_QUESTION = "{owner} asks you: {question}\n\nAnswer it directly."

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
        "You are writing the final edit list for this review. Merge every edit the participants proposed into one "
        "deduplicated list, ordered by severity (must fix, should fix, optional). For each edit, quote the passage, "
        "give the change, and note which participants raised it. Drop edits that later discussion rejected, and say "
        "which ones you dropped and why."
    ),
    "recommend": (
        "You are writing the final recommendation. Use the vote tally from the Moderator. Name the top pick and the "
        "runners-up, each with price and source URL, and note where the participants disagreed. If a product has no "
        "source, say so."
    ),
}

VOTE_EXTRACT = {
    "recommend": "the products proposed in this discussion",
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
