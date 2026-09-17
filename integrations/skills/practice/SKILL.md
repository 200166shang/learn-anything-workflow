---
name: practice
description: Guide one small runnable mechanism in an isolated local workspace, preserving user code, hints, attempts, and reported test facts through learning.practice.
---

# Practice

Use this Skill only when the user asks to practise implementing a mechanism.
Choose one mechanism tied to an existing question and explanation. State its
scope, estimated effort, completion criteria, and the boundary of any
simulation. Do not turn it into a full project or claim simulated timing proves
hardware reliability.

Prepare a `practice-request-v1` for the stable `learning.practice` capability.
It must contain separate example, editable task, tests, and reference-solution
files. Leave the task incomplete: the user writes first. Never place the
reference answer in the task directory, edit the original project, install a
dependency, or access a device without explaining the side effect and getting
the required user choice.

Offer hints only on request and increase levels gradually. Record concise hint
levels and key attempts, not the whole conversation or every keystroke. The
deterministic tool records commands and external test observations but does not
execute user code. If execution is later requested, use an explicitly approved
isolated working directory, argv command, timeout, and resource boundary; do
not add shell interpolation or access real libraries/devices by default.

Before claiming a saved code state, call checkpoint with the last observed
practice revision. If the code changes during capture or the revision is stale,
preserve the user's work and surface the returned recovery/conflict action.
Record completion as independent, with-hint, example-only, or incomplete, plus
the next step. Practice results never update Learn feedback, graph, or position;
use Learn correction separately if the exercise reveals an explanation error.
