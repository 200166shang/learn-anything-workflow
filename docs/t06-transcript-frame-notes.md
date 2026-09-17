# T06 media transcript and real-frame source notes

The public seam is `notes prepare/finalize` or `capability run source.notes`.
A prepare request may point at one item in a completed scoped media operation.
The operation's content digest and size facts are rechecked before use.

Preparation reuses formal subtitles. If none exist, it invokes the configured
transcript adapter once and caches a validated timed SRT under the workspace's
machine-local operation data. That transcript is registered in the source
snapshot without rewriting the acquired source. Audio-only notes have no image
candidates. Video notes decode candidates from the verified video at real SRT
cue timestamps and record both input-video and output-image digests.

Candidates remain machine-local until selected. A model decision is always
recorded as `model_only`; only the explicit user-reviewed CLI/request flag can
record `human`. The generated finalize request includes exactly the selected
frames as adopted attachments. Finalize rejects an adopted attachment omitted
from the Markdown, then publishes body, citations, corrections, association,
attachments and history through the existing authoritative notes commit.
Stored note and frame objects remain readable if the original media later goes
missing. Re-running preparation reuses valid transcripts and frames; deleting
one frame regenerates only that frame.

Visual preparation records are keyed by the registered source identity/version.
Finalization must match the recorded approval, including review mode, candidate
identities, image digests and the complete adopted set; removing both a Markdown
image and its request attachment cannot bypass selection. To change that set,
run preparation with an explicit new selection (an empty set requires a reason).
Decisions remain recorded, and the final approval is stored in the note revision
history. This adds optional `visual_review` to notes-v1 requests, notes and history;
existing non-media note requests remain compatible.

SRT locators normalize comma/dot milliseconds and horizontal spacing while
preserving the original transcript bytes. ASR and frame cache receipts include
the registered, versioned adapter ID; adapter authors must change that ID when
changing implementation/version. Each successful frame is checkpointed before
the next extraction. Untrusted adapter return details and exception strings are
not published or persisted; failures report a fixed stage-specific retry message.
