# T08: recursive questions, feedback, and resumable navigation

Issue: `learn-anything-workflow#20`.

The public seams are `learning pursue/feedback/resume/back/show/locate` plus conflict `replay`. Actual questions are
persisted before an explanation. A caller explicitly re-enters an existing opaque question identity;
matching wording is only an ambiguity signal and never an automatic merge. Each entry records the
user's actual wording and actual source question separately from the historical relationship, without
overwriting the question's first wording.

Explicit understanding feedback has three states: `understood`, `confused`, and `parked`. The latest
value is a projection over an immutable feedback history. Confusions remain question facts. Silence,
answer correctness, browsing, Review, and Practice do not create feedback.

Each thread owns a return-route stack whose frames contain module, thread, and question identities.
It is not reconstructed from question ancestry. Read-only show, locate, and resume preview do not
change positions. Explicit resume atomically changes the target position and pushes the actual origin;
back pops that frame, including across modules. A parked resume preview returns user choices instead
of selecting a branch.

T08 extends `learning-record-v2` only with optional thread navigation fields, so T07 snapshots remain
valid and readers project missing fields as empty lists. Publishing still uses immutable objects and
manifests plus one atomic current pointer. A stale same-question feedback or position mutation is
preserved as a conflict candidate. Feedback to a different, unchanged question may be revalidated
against the expected snapshot and merged without overwriting shared state. Pursue candidates contain
the normalized full intent and declared position effect, so a user-selected candidate can be replayed
without reconstructing missing identity or wording from chat history.

Automated fixtures simulate a controlled session restart and F-navigation behavior. They do not claim
the real next-day A07 evidence reserved for T26.
