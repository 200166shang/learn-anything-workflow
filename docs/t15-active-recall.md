# T15 Active Recall and independent Review record

Issue: `#27`. Specification: `#12` (US16, US24, US25; I2/I3/I4/I7; A10/A11).

## Public contract

- `video-extract review prepare --question-id …` atomically saves a pending Review preparation pinned to the current question, explanation revision/object, source versions, and Learn generation. Its response contains the question and mechanism-first hint policy, but no answer, explanation location, or object digest.
- `video-extract review record --preparation-id … --event-id …` consumes exactly one preparation. It saves the user's actual answer, optional necessary summary, exact hints, model evaluation, and any user correction before revealing the pinned explanation location.
- `video-extract review show` reads Review history across sessions. `learning.review` exposes the same actions through the stable capability registry. The project-owned `review` Skill is the only routing guidance installed for explicit Review intent.

Review uses its own `review-snapshot-v1` authority pointer and immutable generation manifests under the results root. It does not publish a Learn generation, change current learning position or return route, update feedback, or rebuild a graph. A discovered explanation error is routed back to Learn; only an explicit request to deepen may change Learn position.

`event_id` is idempotent for an identical normalized fact and conflicts if reused for different content. A preparation cannot be scored twice. Skip and no-answer use `not_scored`. Each event retains both the model's suggestion and the user-corrected effective evaluation.

`video_extract.review.backup_entries` first deep-validates the authority pointer, manifest digest, schemas, and preparation/event reachability, then returns the complete current Review generation. Paths inside records are results-root-relative so moving a workspace does not change identity.

## Automated evidence

Fixture-only tests cover pre-reveal secrecy, durable pending preparations, independent Learn state, pinned source/explanation versions, user correction, idempotent replay and conflict protection, skipped reviews, missing explanations, CLI, capability routing, cross-session history, and backup reachability. No model, personal learning library, remote service, reminder, migration, upload, or paid action is used.

Full A10 scheduling and A11 real cross-day delivery remain outside T15 and require their owning slices plus final T27 acceptance.
