---
name: learning
description: Learn a first root question from registered source material, preserving the chosen question before explanation and committing source-grounded teaching revisions through video-extract.
---

# Learning

Confirm the user's learning goal, material scope, and registered source version. Create a module only after that confirmation. A new conversation or newly discovered material is not permission to create another module.

Use `video-extract learning recommend --module-id MODULE --workspace CONFIG --json` to prepare root-question recommendations. Explain how a small set of candidates connects the confirmed scope, then wait for the user to choose. Recommendations are temporary and must not be entered as history.

Save the chosen root immediately with `learning thread create`. Save every actual follow-up immediately with `learning pursue`, even before an answer exists. Do not merge questions by title. Use the relationship that reflects the actual entry (`deepens`, `applies`, or `related`).

Run `explanation prepare` for the selected question and follow its model action. Preserve the returned stable section marker. Distinguish course facts, current code, supplemental sources, and inference in the evidence JSON. Commit only via `explanation commit`; never overwrite result objects or learning snapshot files directly.

Register public supplemental material with complete provenance and applicability, then add it with role `supplemental_source`. Treat `manual_review_required` as an external-fact review boundary. Before reuse, honor `learning module show` and `learning locate` source checks: keep historical explanation locations visible, revise only `needs_review` claims, and do not silently move a pinned reference to a new source version. Current-code locators include file plus symbol/line and the file content digest; an inference never becomes a course or code fact.

Choose the teaching profile that matches the material: `linear_transform`, `recognition_to_action`, or `frame_pipeline`. The final explanation must support the profile's causal and boundary checks, not merely contain a fixed summary template. If a required source version is unavailable, pause that question and leave unrelated questions usable.

After a complete explanation, offer the user the understood/confused/parked choice conversationally. Feedback persistence and cross-root navigation are delivered by later learning slices; do not invent those records here.
