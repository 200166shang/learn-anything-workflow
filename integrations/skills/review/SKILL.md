---
name: review
description: Run active recall against an existing persisted explanation, reveal only after the user's attempt, and save Review facts without changing Learn state.
---

# Review

Resolve an explicit review intent to an existing `question_id`. Run `video-extract review prepare --question-id QUESTION --preparation-id review-preparation-STABLE_RETRY_ID --workspace CONFIG --json`, retaining that retry identity until the preparation is observed. If the question or persisted explanation is missing, explain that no review history was created; never invent a thread, question, or past learning.

When proposing flash cards, use `video-extract card propose` with one memory target, its applicability conditions, prompt, answer, reason, and the related question. A proposal is deliberately not stored. Show only a few candidates and call `card select --candidate ...` only for the user's explicit choices. A `reuse` proposal still requires selection before adding the new question association; do not merge cards whose applicability conditions differ.

For a selected card, prepare with `review prepare --card-version-id ...`. If the tool reports a stale bound explanation, stop scoring and ask the user to confirm a wording-only or material `card revise`; do not continue using the old answer. Wording revisions preserve the active version's schedule. Answer or condition changes use a material revision and start a new version on the following Asia/Shanghai date.

Ask the returned mechanism prompt before showing the explanation. Do not inspect or disclose the explanation object or location while the preparation is pending. If the user needs help, give at most one minimal hint at a time and retain the exact hints used. Judge equivalent causal reasoning, not matching words.

After an answer, skip, or explicit non-answer, call `review record` exactly once with a stable event ID and the actual Asia/Shanghai review date for a card. Preserve the user's actual answer, a necessary short summary, every hint, and the model's suggested evaluation. Use `not_scored` for skip or no answer; neither is a failure. When the user corrects the evaluation, pass both the correction in their words and the corrected evaluation. The tool then reveals the pinned explanation version, preserves both judgments, and derives the next card schedule from the immutable facts.

Review never changes the current Learn question, return route, graph, unresolved confusions, or understanding feedback. If recall exposes a factual error in the explanation, report it as a Learn correction candidate. Change learning position only after the user explicitly asks to deepen the topic.

Use `review show --question-id QUESTION` in a new session to retrieve actual Review history. Historical events remain pinned to their original question, explanation revision, object digest, source versions, schema version, and engine version.
