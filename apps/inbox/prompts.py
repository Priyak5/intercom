"""AI summarisation prompts + transcript builder.

The system prompt hard-locks the JSON output schema. The user turn packs the
transcript, and `build_transcript` trims older messages until an estimated
token count fits under `AI_MAX_INPUT_TOKENS` — always keeping the first
message (which usually states the customer's core ask) and the tail of the
conversation (which carries the most recent state).

We use a rough `chars // 4` heuristic for token estimation. The Anthropic SDK
still enforces the real limit at call time — this budget is just to avoid
sending obviously-too-large payloads.
"""

from __future__ import annotations

SUMMARIZE_SYSTEM = """You are a support triage assistant. Read the conversation
transcript and return STRICT JSON with these keys — no prose, no code fences:

{
  "what_they_want": "1-2 sentence description of the customer's core request",
  "whats_been_tried": "what the agent(s) already suggested or attempted",
  "current_status": "where the conversation stands right now",
  "key_details": ["concrete fact 1", "concrete fact 2"]
}

Rules:
- Return the JSON object only. No surrounding text, no ```json fences.
- key_details holds up to 5 concrete facts (order id, error message, version,
  environment, timestamp). Empty list if none.
- If any string field is unknown, use "" (empty string).
- Never invent facts not present in the transcript."""


SUMMARIZE_USER_TEMPLATE = """Conversation between customer and support agent(s).
Messages are chronological; sender_type indicates who wrote each.

{transcript}

Return the JSON now."""


# Rough tokens-per-char ratio for pre-flight budget checks. Real tokenisation
# depends on the model's BPE; this is deliberately conservative (Claude's BPE
# averages ~3.5-4 chars/token for English).
_CHARS_PER_TOKEN = 4

# Cap how many messages we ever ship, regardless of tokens. Prevents pathological
# 10000-message conversations from ballooning the request even if each message
# is very short.
MAX_TAIL_MESSAGES = 40


def _fmt(msg) -> str:
    """One message → a single transcript line like `[3 agent] Hello there`."""
    sender = getattr(msg, "sender_type", "?")
    body = (getattr(msg, "body_text", "") or "").strip()
    return f"[{msg.seq} {sender}] {body}"


def build_transcript(messages, max_input_tokens: int) -> str:
    """Turn an ordered iterable of Message rows into a prompt-ready transcript.

    Keeps the first message (usually the customer's opening ask) always; keeps
    up to `MAX_TAIL_MESSAGES` most-recent messages; drops middle messages first
    until the total estimated token count fits under the budget.
    """
    msgs = list(messages)
    if not msgs:
        return ""

    first = msgs[0]
    tail = msgs[1:][-MAX_TAIL_MESSAGES:]
    kept = [first] + tail if tail else [first]

    # If we're already over-budget with just the first + tail, drop from the
    # OLDEST tail message first (keep the most recent context).
    char_budget = max_input_tokens * _CHARS_PER_TOKEN
    while len(kept) > 1 and sum(len(_fmt(m)) + 1 for m in kept) > char_budget:
        # kept = [first, tail[0], tail[1], ..., tail[-1]] — drop tail[0].
        kept = [kept[0]] + kept[2:]

    # As a last resort (first message alone is enormous), truncate the first.
    lines = [_fmt(m) for m in kept]
    joined = "\n".join(lines)
    if len(joined) > char_budget:
        joined = joined[:char_budget]
    return joined


def user_prompt(messages, max_input_tokens: int) -> str:
    return SUMMARIZE_USER_TEMPLATE.format(
        transcript=build_transcript(messages, max_input_tokens)
    )
