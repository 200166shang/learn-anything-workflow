# ADR 0004: Direct pyVideoTrans Chinese listening capability

Status: accepted

Schema-v5 Chinese listening audio is orchestrated by the public `extract-media` Skill, which directly invokes the ordinary pyVideoTrans project CLI. pyVideoTrans is not a Skill, and neither public Skill invokes another Skill.

`video-extract plan` first detects native Chinese audio. Native Chinese remains a normal video-extract artifact. Without it, only a confirmed English source is supported: video-extract materializes `media/audio.source.m4a` and returns an `awaiting_external` boundary. Non-English or unknown sources are rejected before any cloud call.

pyVideoTrans owns `<package>/listening/zh-CN/` independently. New runs use profile `alibaba-podcast-tts-throughput`; interrupted runs use `--resume`, and optional listener decisions use `--review accepted|rejected`. Its 48 kHz mono 64 kbps MP3 is a natural-paced Chinese listening edition and may differ in duration from the source. Synchronized dubbing is outside this architecture.

Credentials are inherited through `DASHSCOPE_API_KEY` and `DASHSCOPE_WORKSPACE_ID` and are never printed or copied. Failed or uncertain paid submissions are not automatically retried. video-extract neither reads nor adopts pyVideoTrans manifests, private state, reports, or media.
