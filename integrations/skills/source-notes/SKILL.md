---
name: source-notes
description: Write Chinese source-grounded notes from an already prepared VideoLearning package containing a document, transcript, audio, or video; do not download sources or maintain learning question graphs.
---

For workspace-v2 registered text or transcripts, call `video-extract notes
prepare SOURCE_ID --workspace WORKSPACE --json`. Continue the returned
`awaiting_model` action by writing a `source-note-finalize-request-v1` JSON file,
then call `video-extract notes finalize SOURCE_ID --request REQUEST --workspace
WORKSPACE --json`. Preserve the exact `source_id` and `source_version`, use only
the locator policy in the prepared input, and never invent timestamps or claim
that an SRT is matched to video without a `source associate` record backed by
structured, verifiable matching facts. Free-form claims remain unverified.

# Source notes

Work only from the managed package returned by `video-extract source import`, `video-extract ensure`, or an existing package. Use `video-extract notes prepare PACKAGE --workspace CONFIG --json` to discover the next action.

When the action is `evidence_select`, read [evidence and writing](references/evidence-notes.md), inspect the candidates, and write the requested approved evidence JSON. Record `model_only` unless the user actually reviews it. When the action is `notes_write`, read the generated notes input and write the requested Chinese Markdown note. Rerun prepare until it returns `ready`, then run `video-extract notes finalize`.

Reconstruct the source's main line and important reasoning instead of paraphrasing transcript chunks. Keep source-specific conclusions beside real timestamps, headings, or paragraph locators. Adopt every approved image and exclude material explicitly when coverage would otherwise be ambiguous. Audio and transcript sources require timestamps; documents use headings or paragraph locators. Never fabricate screenshots for sources without video.

The package chooses storage. Do not write loose notes beside an input file or choose another Vault directory. This skill does not acquire media, choose an ASR provider, create Mandarin audio, update learning `thread.yaml`, or perform a learning dialogue.
