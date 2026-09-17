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
