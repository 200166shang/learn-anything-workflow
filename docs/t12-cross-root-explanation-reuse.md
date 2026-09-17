# T12 cross-root explanation reuse

Cross-root reuse is a pinned citation, not a merge. `explanation prepare
--reuse-review FILE` requires a structured check of concept meaning,
coordinate/assumption choices, applicability conditions, citation intent, and
the necessary explanation retained in the current root. It also verifies the
target explanation's source versions. `explanation commit` publishes that
local context and the citation atomically with the current root explanation.

The citation pins source module, thread, question, explanation revision,
section, object digest, and checked source versions. It does not change the
source root's prose, question identity, feedback, or position. `learning
locate` is read-only and compares the pin with the current target. A missing,
moved, or revised target is reported on that citation as `needs_review`; the
last checked pin remains authoritative until another explicit reuse review is
committed in the current root. This confines repair to the affected root.

Browsing never changes position. Cross-module continuation remains an
explicit `learning resume --thread-id TARGET --question-id TARGET_QUESTION
--from-question-id ORIGIN` operation and returns through `learning back`.

Automated fixtures use local Markdown only. Real teaching equivalence and the
continued usefulness of the retained paragraph remain user acceptance items.
