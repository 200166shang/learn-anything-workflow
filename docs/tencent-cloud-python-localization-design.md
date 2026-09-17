# 腾讯云音频本地化集成实施方案（执行版）

状态：Ready for implementation  
日期：2026-09-14  
目标执行模型：GPT-5.6 Sol，低推理模式  
腾讯实现基线：`200166shang/video-extract-skill@872b97feecffd1446303b60a8bf68224d91cecf6`

## 0. 执行规则

执行者必须按本文顺序实施，不重新选择架构、目录、命令名或完成标准。遇到本文未覆盖的问题时，只允许做不改变公开契约的最小修复；如果需要改变本文标为“固定”的决定，停止并向用户确认。

以下情况必须停止，不得猜测：

- `/Users/syz/code/audio-localization` 已存在且包含未提交改动；
- `/Users/syz/code/video-extract-core` 中待修改文件与本文描述的现状不符，且无法用局部 patch 安全保留现有内容；
- 腾讯实现基线不是提交 `872b97feecffd1446303b60a8bf68224d91cecf6`；
- 真实云测试需要密钥，但两个环境变量没有设置；
- 需要修改、删除或轮换用户的腾讯云密钥；
- 33:52 样本没有可用英文 SRT，却仍要求五分钟内完成。

普通单元测试不得调用腾讯云。只有用户明确同意消耗额度且 `TENCENT_LIVE_TEST=1` 时才能运行 live test。任何命令、日志、异常、JSON 或测试快照都不得输出密钥。

已确认 `/Users/syz/code/video-extract-core` 当前不是 Git worktree。执行者不得假设可以用 Git 回滚，不得初始化仓库，也不得整文件覆盖现有源码；修改前先读取目标文件，并使用局部 patch 保留无关内容。`/Users/syz/code/audio-localization` 则必须保持为从固定提交创建的独立 Git worktree。

## 1. 最终结果

完成后存在两个普通 Python 项目和两个顶层 Skill：

```text
extract-media Skill
  ├── video-extract CLI                 # 解析意图、获取媒体、管理 package、验证
  └── audio-localization CLI            # 执行 video-extract 返回的 argv；仅在缺少原生中文音轨时运行

video-learning Skill
  └── video-extract CLI                 # 获取视频/字幕并生成学习资料
      （不调用 extract-media Skill，也不调用 audio-localization）
```

固定定义：

- `extract-media`：面向用户的媒体提取 Skill，也是中文可听音频的工作流入口。
- `video-learning`：面向用户的学习笔记 Skill，只产出笔记和画面证据。
- `video-extract`：共享的媒体 package 项目；是 `manifest.json` 的唯一写入者和验证者。
- `audio-localization`：普通原子能力项目；读取本地化请求，调用腾讯 ASR/TMT/TTS，写回标准产物；不是 Skill。
- `localize-audio`：`video-extract` 暴露的内部流程动作，不是第三个 Skill。

禁止出现以下调用：

```text
video-learning Skill → extract-media Skill
extract-media Skill → tencent-video-localization Skill
audio-localization → 修改 manifest.json
audio-localization → 下载原视频或决定 canonical package 路径
```

## 2. 已固定的产品决定

1. 腾讯云是音频本地化唯一模型后端；不提供 MLX、Qwen、本地 Whisper、macOS `say` 或其他 TTS fallback。
2. 发现原生中文音轨时直接提取，不调用 `audio-localization`。
3. 缺少原生中文音轨时，先获取源音频；已有可靠英文 SRT 时必须复用并跳过 ASR。
4. 没有可靠字幕时才调用腾讯录音文件 ASR；此路径不承诺五分钟完成。
5. 默认 TTS 男声为 `501005`，翻译并发为 5，TTS 并发为 3，最大时间压缩为 1.35x。
6. 输出纯中文语音轨，不混入原背景声，不做声纹克隆、唇形同步或视频重新编码。
7. 不实现播放回听 ASR QA，不生成 `quality-report.json`，不以回听相似度作为完成条件。
8. 完成条件是结构与时间轴验证通过：音频可解码、时长匹配、字幕覆盖完整且有序、每段压缩不超过 1.35x、无未完成的最终文件。
9. 五分钟是“已有可靠 SRT 的 33:52 样本”的性能目标，不是外部云服务 SLA。已验证基线为首次 128.55 秒、缓存重跑 70.46 秒。
10. Schema v1–v4 package 原地保留；本次只改变 schema v5 媒体请求路径。旧 `podcast_zh` 兼容流程不在本次重写范围内。

## 3. 仓库与代码归属

### 3.1 `audio-localization` 项目

目标本地目录：

```text
/Users/syz/code/audio-localization
```

来源为现有腾讯实现仓库固定提交：

```text
https://github.com/200166shang/video-extract-skill
872b97feecffd1446303b60a8bf68224d91cecf6
```

把它从 Skill 仓库改造成普通 Python 项目：

```text
audio-localization/
├── pyproject.toml
├── README.md
├── LICENSE
├── docs/
│   └── tencent-cloud.md
├── src/
│   └── audio_localization/
│       ├── __init__.py
│       ├── cache.py
│       ├── cli.py
│       ├── domain.py
│       ├── errors.py
│       ├── media.py
│       ├── pipeline.py
│       ├── ports.py
│       ├── request.py
│       ├── srt.py
│       └── tencent.py
└── tests/
    ├── test_cache.py
    ├── test_cli.py
    ├── test_pipeline.py
    ├── test_request.py
    ├── test_srt.py
    └── test_tencent.py
```

必须执行的重命名：

- Python distribution：`video-extract-skill` → `audio-localization`；
- Python package：`video_extract_skill` → `audio_localization`；
- CLI：`video-localize` → `audio-localize`；
- README 标题：`video-extract-skill` → `audio-localization`；
- `references/tencent-cloud.md` → `docs/tencent-cloud.md`。

必须删除：

- 根目录 `SKILL.md`；
- `evals/`；
- 只为 Skill 包装存在的 `scripts/localize.py`。

保留原 MIT `LICENSE`。不要把该项目安装成 Codex Skill，也不要在任何 Skill 目录内复制它。

### 3.2 `video-extract` 项目

目标目录：

```text
/Users/syz/code/video-extract-core
```

它继续负责：

- 解析 URL、本地文件与平台身份；
- 选择原生中文或源语言音轨；
- 选择并保存可靠字幕；
- 创建 canonical package；
- 写入和更新 `manifest.json`；
- 产生本地化请求；
- 验收并登记 `audio-localization` 的产物；
- `plan`、`ensure`、`status`、`verify` 的稳定接口。

它不复制腾讯 SDK 实现，不直接调用腾讯 API。

### 3.3 文件变更矩阵

执行者按此表创建、修改和删除文件，不自行另建平行实现。

| 项目 | 动作 | 文件 |
|---|---|---|
| `audio-localization` | 重命名目录并修改 | `src/audio_localization/{__init__,cache,cli,domain,errors,media,pipeline,ports,srt,tencent}.py` |
| `audio-localization` | 新增 | `src/audio_localization/request.py` |
| `audio-localization` | 修改 | `pyproject.toml`, `README.md` |
| `audio-localization` | 移动 | `references/tencent-cloud.md` → `docs/tencent-cloud.md` |
| `audio-localization` | 删除 | `SKILL.md`, `evals/`, `scripts/localize.py` |
| `audio-localization` | 修改/新增测试 | `tests/test_cache.py`, `tests/test_cli.py`, `tests/test_pipeline.py`, `tests/test_request.py`, `tests/test_srt.py`, `tests/test_tencent.py` |
| `video-extract` | 修改 | `video_extract/contracts.py`, `video_extract/planner.py`, `video_extract/media_workflow.py`, `video_extract/validate.py`, `video_extract/cli.py` |
| `video-extract` | 修改 | `schemas/manifest-v5.schema.json`, `README.md`, `CONTEXT.md`, `docs/package-contract.md` |
| `video-extract` | 检查，不预期修改 | `pyproject.toml`, `uv.lock`；保留 `faster-whisper`，确认没有 MLX/Qwen 本地化依赖 |
| `video-extract` | 新增 | `docs/adr/0004-external-audio-localization-capability.md` |
| `video-extract` | 新增测试 | `tests/test_localization_contract_v5.py`, `tests/test_media_localization_workflow_v5.py` |
| `video-extract` | 新增显式 live test | `tests/live/test_tencent_localization_live.py`（默认 skip） |
| `video-extract` | 修改测试 | `tests/test_media_request_v5.py`, `tests/test_cli_contract.py` |
| `video-extract` | 删除 | `video_extract/audio_localization.py`, `tests/test_audio_localization.py` |
| `extract-media` Skill | 修改 | `SKILL.md`, `references/chinese-audio.md`, `evals/evals.json` |
| `video-learning` Skill | 不改文件 | 检查 `/Users/syz/.agents/skills/video-learning/SKILL.md` 及其直接引用文件 |

## 4. Canonical package 契约

所有流程共享一个 package：

```text
/Users/syz/Media/video-extract/items/<platform>/<stable-id>/
├── manifest.json
├── media/
│   ├── video.mp4                       # 请求或学习流程需要时存在
│   ├── audio.source.m4a                # 源音频
│   └── audio.zh-CN.m4a                 # 最终中文音频
├── subtitles/
│   └── source.<language>.srt           # 平台字幕或腾讯 ASR 结果
├── localization/
│   ├── request.json                    # video-extract 写
│   ├── script.zh-CN.json               # audio-localization 写
│   └── report.json                     # audio-localization 写
├── evidence/                           # video-learning 使用
├── notes/                              # video-learning 使用
└── .work/
    └── audio-localization/
        ├── asr/
        ├── translation/
        ├── tts/
        └── timeline/
```

固定所有权：

| 文件 | 写入者 | 读取者 |
|---|---|---|
| `manifest.json` | `video-extract` | `video-extract`、两个 Skill |
| `localization/request.json` | `video-extract` | `audio-localization` |
| `localization/script.zh-CN.json` | `audio-localization` | `video-extract` validator |
| `localization/report.json` | `audio-localization` | `video-extract` validator、Skill |
| `media/audio.zh-CN.m4a` | `audio-localization` | `video-extract` validator、用户 |
| `subtitles/source.en.srt` | `video-extract`（已有字幕）或 `audio-localization`（本次腾讯 ASR） | 两个项目、两个 Skill |
| `.work/audio-localization/**` | `audio-localization` | `audio-localization` |

所有持久化路径都必须是 package-relative POSIX 路径。报告不得保存绝对路径。单个最终文件使用同目录隐藏 sibling 和 `os.replace` 原子替换；跨文件一致性由第 5 节的 request 指纹、第 6 节的内容 hash 和 report-last commit 规则共同保证。

## 5. 跨项目请求契约

`video-extract` 在需要腾讯本地化时写入 `localization/request.json`。固定 schema：

```json
{
  "schema": "audio-localization-request-v1",
  "request_id": "alr-0123456789abcdef01234567",
  "package_schema_version": 5,
  "provider": "tencent-cloud",
  "source": {
    "audio": "media/audio.source.m4a",
    "audio_sha256": "<64 lowercase hex characters>",
    "subtitle": "subtitles/source.en.srt",
    "subtitle_sha256": "<64 lowercase hex characters>",
    "asr_output": "subtitles/source.en.srt",
    "language": "en"
  },
  "target": {
    "language": "zh-CN",
    "audio": "media/audio.zh-CN.m4a",
    "script": "localization/script.zh-CN.json",
    "report": "localization/report.json"
  },
  "work_dir": ".work/audio-localization",
  "config_sha256": "<64 lowercase hex characters>",
  "config": {
    "region": "ap-guangzhou",
    "voice": 501005,
    "sample_rate": 24000,
    "tts_speed": 0.0,
    "translation_concurrency": 5,
    "tts_concurrency": 3,
    "max_translation_chars": 1800,
    "max_tts_chars": 70,
    "max_speed_ratio": 1.35,
    "output_bitrate": "192k"
  }
}
```

规则：

- `source.audio_sha256` 是源音频文件字节的 SHA-256。
- 有字幕时，`source.subtitle_sha256` 是字幕文件字节的 SHA-256；没有字幕时，`source.subtitle` 与 `source.subtitle_sha256` 都必须为 `null`。
- `source.asr_output` 始终为 `subtitles/source.en.srt`。没有字幕时，`audio-localization` 使用腾讯 ASR，并把 SRT 原子写到该路径。
- `config_sha256` 是 `config` object 使用 UTF-8、key 排序、无多余空格 JSON 编码后的 SHA-256。
- `request_id` 是除 `request_id` 外整个 request object 使用同样 canonical JSON 编码后的 SHA-256 前 24 个十六进制字符，并带 `alr-` 前缀。相同输入字节、路径和配置产生相同 ID；任一输入或配置变化必须产生新 ID。
- request reader 必须从磁盘重算实际源音频 hash、已有字幕 hash、config hash 和 request ID；字段彼此相等但与实际文件或 canonical JSON 不相等时仍然失败。`audio-localization` 启动时和 `video-extract` adoption 时各自执行一次，不共享 producer 的判断结果。
- `quality=high` 对应 `output_bitrate=192k`；`quality=standard|balanced` 对应 `128k`。
- 首版只接受 `source.language=en` 与 `target.language=zh-CN`。其他组合返回配置错误，不静默猜测。
- `provider` 只能是 `tencent-cloud`，不增加 `--provider` 选择器。
- `request.json` 不包含 SecretId、SecretKey、Cookie、Authorization、签名 URL或完整远程媒体 URL。
- `audio-localization` 必须使用与 `video_extract.manifest.package_path` 等价的安全解析规则，拒绝逃逸 package 的路径。

## 6. 本地化产物契约

### 6.1 `script.zh-CN.json`

不要直接沿用基线实现中“每个拆分 TTS segment 重复携带全部 `source_ids`”的格式。该实现会让同一字幕 ID 在拆分后重复出现，而现有 `_assert_source_coverage` 用集合比较，无法发现重复。

固定使用下面的两层结构：

```json
{
  "schema": "localized-script-v3",
  "request_id": "alr-0123456789abcdef01234567",
  "source_audio_sha256": "<same as request>",
  "source_subtitle_sha256": "<actual SRT sha256>",
  "config_sha256": "<same as request>",
  "source_cue_ids": ["1", "2", "3"],
  "translation_units": [
    {
      "id": "u-1",
      "start": 0.0,
      "end": 4.2,
      "source_ids": ["1", "2"],
      "source_text": "Hello. Again.",
      "translated_text": "你好。再说一次。",
      "speaker": "unknown"
    }
  ],
  "segments": [
    {
      "id": "u-1-0",
      "translation_unit_id": "u-1",
      "part_index": 0,
      "part_count": 1,
      "start": 0.0,
      "end": 4.2,
      "text": "你好。再说一次。",
      "speaker": "unknown",
      "voice": 501005,
      "synthesized_duration_seconds": 3.7,
      "slot_duration_seconds": 4.2,
      "speed_ratio": 1.0
    }
  ]
}
```

验证算法固定为：

1. 解析源 SRT 得到 `expected_ids`，保持原顺序，不转成集合。
2. 要求每个源 cue ID 非空且在源 SRT 中唯一；重复 ID 直接失败。
3. 按 `translation_units` 顺序展开全部 `source_ids` 得到 `actual_ids`。
4. 要求 `actual_ids == expected_ids`；这同时验证完整、顺序、无重复、无遗漏。
5. 每个 segment 的 `translation_unit_id` 必须存在。
6. 同一 unit 的 segment 必须连续，`part_index` 从 0 到 `part_count - 1` 恰好出现一次。
7. segment 文本非空，时间为非负递增区间，voice 是正整数。
8. `translation_units` 与 segments 的总体顺序必须与时间轴顺序一致。
9. 每个 unit 的 `source_ids` 必须对应源 SRT 中一段连续 cue；unit 的 start/end 必须分别等于首/尾 cue 边界，容差 1 毫秒。
10. 同一 unit 内 segments 必须完整、连续、互不重叠地覆盖 unit slot，边界容差 1 毫秒；全局 unit 按时间递增且不重叠。遇到重叠源 SRT 直接返回输入错误，不自动裁切或混音。
11. validator 用 `synthesized_duration_seconds / slot_duration_seconds` 重新计算 `max(1.0, ratio)`，要求与 `speed_ratio` 差不超过 0.001，并要求不超过 1.35。

### 6.2 `report.json`

固定 schema：

```json
{
  "schema": "tencent-localization-report-v2",
  "request_id": "alr-0123456789abcdef01234567",
  "source_audio_sha256": "<same as request>",
  "source_subtitle_sha256": "<same as script>",
  "config_sha256": "<same as request>",
  "status": "complete",
  "provider": "tencent-cloud",
  "source": {
    "audio": "media/audio.source.m4a",
    "subtitle": "subtitles/source.en.srt",
    "subtitle_origin": "existing",
    "duration_seconds": 2032.0,
    "cue_count": 177
  },
  "output": {
    "audio": "media/audio.zh-CN.m4a",
    "audio_sha256": "<64 lowercase hex characters>",
    "script_sha256": "<64 lowercase hex characters>",
    "duration_seconds": 2032.0,
    "bitrate": "192k"
  },
  "coverage": {
    "ordered_exactly_once": true,
    "source_cue_count": 177,
    "translation_unit_count": 120,
    "segment_count": 177
  },
  "timeline": {
    "duration_delta_seconds": 0.0,
    "max_speed_ratio_allowed": 1.35,
    "max_speed_ratio_observed": 1.21
  },
  "usage": {
    "translated_characters": 10000,
    "tts_characters": 6000,
    "asr_requests": 0,
    "translation_requests": 12,
    "tts_requests": 177,
    "translation_cache_hits": 0,
    "tts_cache_hits": 0,
    "asr_cache_hits": 0
  },
  "timings": {
    "asr_seconds": 0.0,
    "translation_seconds": 20.0,
    "tts_seconds": 90.0,
    "timeline_seconds": 18.0,
    "total_seconds": 128.0
  },
  "config": {
    "region": "ap-guangzhou",
    "voice": 501005,
    "translation_concurrency": 5,
    "tts_concurrency": 3
  }
}
```

`subtitle_origin` 只能是 `existing` 或 `tencent_asr`。report 必须在 script 与最终音频完成后最后写入，它是本次输出集合的 commit marker。`output.audio_sha256` 和 `output.script_sha256` 必须匹配实际文件。本报告是执行与结构校验报告，不是听感质量报告；不得出现 `playback_qa`、相似度、回听转写或“人工已试听”字段。

### 6.3 `manifest.json` 登记

`audio-localization` 成功退出后，`video-extract` 重新读取并独立验证三个产物。`subtitle_origin` 只由 request 决定：request 的 `source.subtitle` 非 null 为 `existing`，为 null 则为 `tencent_asr`。只有验证全部通过时才登记：

```json
{
  "artifacts": {
    "source_audio": "media/audio.source.m4a",
    "source_subtitle": "subtitles/source.en.srt",
    "localization_request": "localization/request.json",
    "localized_script": "localization/script.zh-CN.json",
    "localization_report": "localization/report.json",
    "localized_audio": "media/audio.zh-CN.m4a"
  },
  "provenance": {
    "localized_audio": {
      "kind": "synthesized",
      "language": "zh-CN",
      "provider": "tencent-cloud",
      "request_id": "alr-0123456789abcdef01234567",
      "source_audio_sha256": "<same as request>",
      "source_subtitle_sha256": "<same as script>",
      "config_sha256": "<same as request>",
      "voice": 501005,
      "script_schema": "localized-script-v3",
      "report_schema": "tencent-localization-report-v2"
    }
  }
}
```

如果腾讯 ASR 生成字幕，还要登记：

```json
{
  "provenance": {
    "source_subtitle": {
      "kind": "asr",
      "provider": "tencent-cloud",
      "language": "en"
    }
  }
}
```

失败时保留 `.work/audio-localization/`，但不得在 manifest 中登记新的 `localized_audio`、`localized_script` 或 `localization_report`。

创建新 request 时，`video-extract` 先原子替换 `localization/request.json`，再在一次独立的 manifest 原子写中移除旧的 `localized_script`、`localization_report`、`localized_audio` 登记及对应 synthesized provenance，同时登记新 `localization_request` 和 pause。两次写入之间的崩溃按 B3 的 reconcile 规则恢复。旧输出文件可以暂时留在固定路径，但在新 request 完成 adoption 前不再是 manifest 事实源。

`audio-localization` 必须先在 `.work/audio-localization/runs/<request_id>/staging/` 生成并自检所有文件，再按以下顺序提交固定路径：

1. ASR 产生字幕时提交 `subtitles/source.en.srt`；
2. 提交 `localization/script.zh-CN.json`；
3. 提交 `media/audio.zh-CN.m4a`；
4. 最后提交 `localization/report.json`。

每个临时文件名固定为 `.<final-name>.partial-<request_id>`。进程在 report 提交前中断时，第二次 ensure 不得 adoption；重跑相同 request 时由 cache 修复并重新提交完整集合。

## 7. CLI 契约

### 7.1 `audio-localization`

唯一生产命令：

```bash
audio-localize --package /absolute/path/to/package --json
```

它只读取 `<package>/localization/request.json`，不接受单独的 `--audio`、`--output` 或 `--provider`。这样调用者不能绕开 canonical package。

重跑语义固定为：如果 request_id、三个 input/config hash、三个最终 output hash 及结构校验全部匹配，直接返回 `complete`，`artifact_reuse=true`，不调用云端也不重新封装。此时 CLI 的 `cloud_requests_this_run` 三项必须全为 0；report 保留最初生成该产物时的 usage，不被重跑覆盖。只要任一项不匹配，就按相同 request 从 cache 恢复并重新提交完整输出集合；不得把旧 request 的最终文件当作 cache hit。

本地开发安装固定使用：

```bash
uv tool install --editable '/Users/syz/code/audio-localization[tencent]'
audio-localize --help
```

不要把 `/Users/syz/code/audio-localization` 写成 `video-extract` 的绝对路径依赖。远端 GitHub 仓库改名属于外部仓库设置，不在本实施任务中自动执行；本地目录、distribution、Python package 和 CLI 改名即可。

成功输出：

```json
{
  "status": "complete",
  "package": "/absolute/path/to/package",
  "audio": "media/audio.zh-CN.m4a",
  "script": "localization/script.zh-CN.json",
  "report": "localization/report.json",
  "elapsed_seconds": 128.55,
  "cache_hits": 0,
  "artifact_reuse": false,
  "cloud_requests_this_run": {
    "asr": 0,
    "translation": 12,
    "tts": 177
  }
}
```

失败输出：

```json
{
  "status": "failed",
  "code": "configuration_error | permanent_cloud_error | transient_cloud_error | timeline_error | invalid_request",
  "message": "不包含凭据的可操作错误"
}
```

退出码固定：成功 `0`，请求或配置错误 `2`，云端/时间轴/媒体执行失败 `1`，用户中断 `130`。

凭据只从以下环境变量读取：

```text
TENCENTCLOUD_SECRET_ID
TENCENTCLOUD_SECRET_KEY
```

Region 由 `request.json` 提供，默认 `ap-guangzhou`。不得支持命令行密钥参数。

### 7.2 `video-extract`

用户请求中文音频时，入口保持：

```bash
video-extract ensure SOURCE \
  --media audio \
  --language zh-CN \
  --quality high \
  [--output PACKAGE] \
  --json
```

如果存在原生中文音轨，命令直接完成。如果需要合成，第一次返回：

```json
{
  "status": "awaiting_localization",
  "package": "/absolute/path/to/package",
  "action": "localize-audio",
  "request": "localization/request.json",
  "command": [
    "/Users/syz/.local/bin/audio-localize",
    "--package",
    "/absolute/path/to/package",
    "--json"
  ]
}
```

`video-extract` 使用 `shutil.which("audio-localize")` 解析 executable，并在 command 数组中返回解析后的绝对路径；找不到时返回明确 blocker。调用者执行返回的 argv 数组，不用 shell 拼接。`audio-localize` 成功后，调用者原样重跑 `video-extract ensure ...`；第二次 ensure 负责验证、登记并返回 `complete`。

保留 `video-extract localize-audio` 作为兼容包装器，但改为接收 package：

```bash
video-extract localize-audio PACKAGE --json
```

包装器只做三件事：读取 `request.json`、以 argv 调用 `audio-localize --package PACKAGE --json`、成功后调用同一个 adoption validator。不要在包装器中复制腾讯实现。

### 7.3 状态迁移

以下表格是唯一状态解释。`video-extract ensure` 只有在当前 request 确实要求合成中文音频时才检查本地化产物；其他媒体请求和 `notes_zh` 不触发 adoption。

| 进入时状态 | 处理 | manifest 变化 | ensure 结果 | 退出码 |
|---|---|---|---|---:|
| 需要合成，无 request | 生成 request，撤销旧本地化登记 | 写 request artifact 与 pause | `awaiting_localization` + command | 0 |
| awaiting，request 未变，无/缺少输出 | 不重写 request | 保持 pause | `awaiting_localization` + 同一 command | 0 |
| awaiting，存在 partial 或 report 不存在 | 不 adoption | 保持 pause | `awaiting_localization` + 同一 command | 0 |
| awaiting，完整输出且 request/hash/结构有效 | adoption | 登记产物与 provenance，清 pause | `complete` | 0 |
| awaiting，report 存在但完整输出无效 | 不 adoption | 保持 pause，并记录 sanitized error | `failed` + validation errors + command | 1 |
| complete，相同 request 且仍有效 | 直接复用 | 不改变 | `complete`，cache hit | 0 |
| complete，输入或配置产生新 request | 撤销旧登记并写新 request | 写新 pause | `awaiting_localization` + 新 command | 0 |
| `audio-localize` 配置/请求错误 | 不碰 manifest | pause 保持 | localizer `failed` | 2 |
| `audio-localize` 云端/时间轴/媒体失败 | 保留 cache，不碰 manifest | pause 保持 | localizer `failed` | 1 |
| 用户中断 `audio-localize` | 保留 cache 和 partial，不碰 manifest | pause 保持 | 中断 | 130 |

如果 `audio-localize` 返回失败，Skill 本轮停止并报告，不自动反复重试。用户或后续任务重跑同一 command 时继续利用 cache。

## 8. `audio-localization` 项目实施步骤

### A1. 固定基线并改名

1. 克隆或更新源仓库到 `/Users/syz/code/audio-localization`。
2. 验证 `git rev-parse HEAD` 等于固定提交。
3. 如果目录已有未提交改动，停止。
4. 移动 `src/video_extract_skill` 到 `src/audio_localization` 并更新所有 import。
5. 按第 3.1 节修改 `pyproject.toml`、README 和 CLI 名称。
6. 删除 Skill 专属文件，移动腾讯说明文档。
7. 运行原有 8 个测试，全部恢复为通过状态后再继续。
8. 运行 `uv tool install --editable '/Users/syz/code/audio-localization[tencent]'`，再用 `command -v audio-localize` 和 `audio-localize --help` 验证调用入口。

依赖保持为：

```toml
[project.optional-dependencies]
tencent = [
  "tencentcloud-sdk-python-common>=3.0.1482,<4",
  "tencentcloud-sdk-python-asr>=3.0.1482,<4",
  "tencentcloud-sdk-python-tts>=3.0.1482,<4",
]
test = ["pytest>=8,<9"]
```

不要添加 `tencentcloud-sdk-python-tmt`；已验证实现使用官方 `CommonClient` 调用 TMT，因为当前生成 SDK 缺少 `TextTranslateRequest`。

### A2. 改成 package 请求接口

新增 `request.py`：

- 定义 request dataclass；
- 读取并严格验证 `audio-localization-request-v1`；
- 将所有相对路径安全解析到 package 内；
- 验证源音频存在且 `ffprobe` 时长大于 0；
- 有字幕时验证 SRT 至少有一个非空 timed cue；
- 无字幕时允许腾讯 ASR；
- 可以忽略不影响行为的额外字段，但必须拒绝未知 schema、provider、语言和越界路径。

把 `pipeline.localize_audio(audio, output, ...)` 改为：

```python
def localize_package(
    package: Path,
    *,
    provider: CloudProvider | None = None,
    media: MediaRunner | None = None,
) -> LocalizationResult:
    ...
```

生产调用只传 package。测试仍通过注入 fake provider/media 隔离网络和 FFmpeg。

### A3. 修复字幕覆盖模型

修改 `domain.py`：

- `TranslationUnit` 保留 `source_ids`、`source_text`、`translated_text`；
- `LocalizedSegment` 删除 `source_ids`，新增 `translation_unit_id`、`part_index`、`part_count`；
- 生成第 6.1 节的 `localized-script-v3`；
- 将 coverage 校验从集合比较改为有序列表相等；
- 增加回归测试：一个 source cue 翻译后拆为两个 TTS segment 时，source ID 只在 translation unit 中出现一次，验证仍通过；刻意重复或调换 source ID 时必须失败。

### A4. 补齐缓存恢复

保留 content-addressed translation 与 TTS cache，并新增 ASR cache：

- ASR key：音频 chunk SHA-256、offset、engine、diarize、provider schema；
- translation key：provider、source/target language、source text、provider schema；
- TTS key：provider、文本、voice、sample rate、speed、codec、provider schema；
- timeline key：TTS 音频 hash、start/end、speed ratio、output sample rate。

缓存文件只有在 JSON schema 可读或媒体可被 `ffprobe` 解码时才命中。损坏缓存只删除对应 cache entry，不删除整个 `.work`。最终封装失败不得重新调用翻译或 TTS。

pipeline 为本次进程维护 `asr_requests`、`translation_requests`、`tts_requests` 三个计数器：只有实际进入 provider 网络方法时加一，cache hit 和 artifact reuse 不加。首次完成时写入 report usage，每次 CLI 调用都在 stdout JSON 返回 `cloud_requests_this_run`；这样测试无需读取腾讯账单即可验证是否发生云调用。

### A5. 腾讯 Provider

从基线原样保留以下已验证行为：

- `Credentials` 的两个字段 `repr=False`；
- TMT 使用 `CommonClient("tmt", "2018-03-21", ...)`；
- TTS 使用 `TextToVoice`，MP3、24kHz、默认 voice `501005`；
- ASR 使用 `CreateRecTask` + `DescribeTaskStatus`；
- ASR chunk 小于等于 5 MiB；
- 永久错误不重试，临时错误最多 3 次；
- TMT dispatch 至少间隔 0.3 秒；
- SDK 细节不得进入 pipeline/domain。

补充要求：

- 所有 FFmpeg/FFprobe 子进程设置有限超时并捕获末尾 500 字符错误；
- 错误消息只包含服务、错误码和可操作建议；
- `ConfigurationError` 的安装提示改为 `pip install 'audio-localization[tencent]'`；
- ASR 成功后原子写入标准 SRT 和 cache；
- provider 不写 manifest。

无字幕 ASR 拼接算法固定为：

1. FFmpeg 按时间连续、无重叠切成最长 600 秒的 16kHz 单声道 MP3；若任一 chunk 超过 5 MiB，则逐步缩短该 chunk，直到满足限制。
2. 记录每个 chunk 在源音频中的计划 `start_offset`，不得用前面识别结果的末尾时间推算。
3. 按 chunk 顺序调用或命中 ASR cache，把返回句子的局部时间加上该 chunk 的 `start_offset`。
4. 丢弃空文本或 `end <= start` 的句子；其余句子按 `(start, end, chunk_index, sentence_index)` 排序。
5. 相邻句子重叠不超过 50 毫秒时，在内存中把前一句 `end` 设为后一句 `start`；如果前一句因此变成空区间则失败。超过 50 毫秒的真实重叠直接失败，不自动合并或混音。既有 SRT 与 ASR SRT 的所有 parser/validator 都使用同一个归一化函数。
6. 忽略腾讯返回的外部句子 ID，按最终顺序生成唯一内部 ID `asr-000001`、`asr-000002`……。
7. 生成的最后 cue 不得超过源音频时长 0.1 秒；写入 SRT 后重新解析并通过相同 cue 校验。
8. request 始终保持 `source.subtitle=null`；report 始终记录 `subtitle_origin=tencent_asr`。同一 request 重跑即使 SRT 已存在，也不得改记为 `existing`。

### A6. 时间轴和报告

保留基线的停顿、`atempo`、补静音和最终 loudness normalize 行为。固定规则：

- TTS 时长不超过 slot：原速并补静音；
- TTS 时长超过 slot：使用所需比例的 `atempo`；
- 所需比例大于 1.35：抛 `TimelineError`，不强行压缩；
- 最终 AAC 时长与源音频差不超过 0.1 秒；
- 最终文件先写隐藏 partial，验证后原子替换；
- 报告记录实际观察到的最大 speed ratio；
- 不运行或模拟播放回听 QA。

## 9. `video-extract` 项目实施步骤

### B1. 更新 v5 artifact 名称

修改 `video_extract/contracts.py` 中 `V5_ARTIFACTS`：

```python
V5_ARTIFACTS = {
    "source_video": "media/video.mp4",
    "source_audio": "media/audio.source.m4a",
    "source_subtitle": "subtitles/source.srt",
    "localization_request": "localization/request.json",
    "localized_script": "localization/script.zh-CN.json",
    "localization_report": "localization/report.json",
    "localized_audio": "media/audio.zh-CN.m4a",
    "candidate_json": "evidence/candidates.json",
    "approved_json": "evidence/approved.json",
    "contact_sheet": "evidence/contact-sheet.jpg",
    "notes": "notes/notes.zh-CN.md",
}
```

删除 v5 的 `voice_reference` 和 `audio_qa_report`。不要修改 schema v1–v4 的 `DEFAULT_ARTIFACTS`。

实际字幕仍允许 `subtitles/source.<language>.srt`；`V5_ARTIFACTS["source_subtitle"]` 只是无语言信息时的 fallback，manifest 必须登记真实文件名。

### B2. 修改 planner

修改 `build_media_plan`：

- 中文音频且有原生中文：仍只规划 `normalize_native_chinese_audio`；
- 中文音频且无原生中文：规划 `materialize_source_audio`；
- 有可靠英文字幕：再规划 `materialize_formal_subtitle`；只接受语言标签 `en` 或 `en-*`；
- 无可靠字幕：不要单独规划 `transcribe_source_audio`，ASR 是 `audio-localization` 内部步骤；
- 最后规划 `localize_audio`，reason 改为 `generate Mandarin audio with Tencent Cloud`；
- 不再出现 `generate and verify Mandarin audio locally`。

字幕单独请求的行为保持不变：平台无字幕时明确返回 unavailable，不为了字幕提取请求启动 ASR。

“可靠英文字幕”的唯一 predicate：文件在 package 内、SRT 可解析、至少一个非空 cue、cue ID 唯一、时间非负、按第 A5 节的 50 毫秒规则归一化后单调不重叠、末尾不超过源音频时长 0.1 秒，并且 manifest provenance 或文件名表明语言是 `en`/`en-*`。新平台获取时只选择 `automatic=false` 的英文轨；已有 package 中通过上述结构校验的英文 SRT 可以复用，包括以前由其他 ASR 生成但已经落盘的字幕。自动平台字幕不得作为新本地化请求的可靠字幕。

### B3. 生成请求并暂停

修改 `video_extract/media_workflow.py`：

1. 在进入 `localize_audio` stage 时，确保源音频已登记。
2. 如果 manifest 已登记可靠 source subtitle，则写入其路径；否则写 `null`。
3. 从选中音轨或 inventory 的语言元数据确认源语言；`en`、`en-US`、`en-GB` 等统一写为 `en`。非英语或未知语言返回明确 blocker，不猜测。
4. 计算源文件 hash、config hash 和 deterministic request ID。
5. 先按第 5 节原子替换 `localization/request.json`。
6. 再从 manifest 移除旧本地化 artifacts/provenance，登记新 `localization_request`，写入 `pause.status=awaiting_localization`，并原子替换 manifest。跨文件不能声称单事务原子性；若在两次替换之间崩溃，下次 ensure 按当前输入重算 request ID 并完成相同两步。
7. 返回第 7.2 节的结构化结果与 argv command。
8. 不在 `ensure` 内直接 import 腾讯 SDK。

每次 ensure 的判断顺序固定如下，禁止“先 adoption 再计算本次 request”：

1. 从本次 CLI 参数得到目标语言、quality 和 config，并重新计算实际源音频 hash。
2. 读取 manifest 当前登记的 `localization_request`；从实际文件重算并验证它的 hash/ID。
3. 若旧 request 的源音频 hash、目标语言和 config 与本次调用一致，则把旧 request 作为 `expected_request`。即使它的腾讯 ASR 字幕已经登记进 manifest，也保留原 `source.subtitle=null` 和原 request ID，用于恢复及第三次 ensure 的 artifact reuse。
4. 若上述核心输入任一变化，则这是新请求。此时才根据当前可靠英文字幕生成新 request；以前的腾讯 ASR 字幕现在可以作为新请求的 existing subtitle，避免重复 ASR。
5. 只有 report 的 request ID 等于 `expected_request.request_id` 时才进入完整验证。其他 request ID 的旧 report 视为“本次输出尚未生成”，不是当前请求的 invalid output。
6. 没有当前 request 的完整输出：按 B3 的固定提交顺序写/修复 request 和 manifest pause，返回 `awaiting_localization`。
7. 当前 request 的完整输出存在：使用 `validate_localization_outputs` 独立读取源 SRT、script、report 和音频。
8. 全部通过时登记 artifacts、provenance 和 `localized_audio_ready` stage，清除 pause；stage fingerprint 等于 request ID，并记录 report 总耗时与 cache/artifact reuse 计数。当前 request 的 report 已存在但验证失败时保留 pause，返回 `failed` 与具体结构错误；不得只按文件存在判定完成。

崩溃恢复固定为：request 先提交、manifest 后提交。若只提交了 request，下次 ensure 重算 expected request；匹配则补写 manifest pause，不匹配则覆盖为新 request。若 manifest 已写 pause 而输出不完整，则返回同一 command。旧 report 只有 request ID 匹配当前 expected request 时才可能成为 commit marker。

### B4. 修改 validator

在 `video_extract/validate.py` 增加纯函数：

```python
def validate_localization_outputs(package: Path) -> list[str]:
    ...
```

检查项固定为：

- request、script、report 都是非空 JSON object 且 schema 正确；
- 所有路径安全且与固定 package 路径一致；
- 从实际文件重算源音频/字幕 SHA-256，从 canonical config JSON 重算 config hash，并从 request 重算 request ID；不得只比较 JSON 中自报字段；
- 源音频和中文音频可被 `ffprobe` 解码并有正时长；
- 有 source subtitle，且 timed cues 可解析；
- script 通过第 6.1 节的 ordered exactly-once 算法；
- report `status=complete`、`provider=tencent-cloud`；
- request、script、report 的 `request_id`、源音频 hash 和 config hash 完全一致；已有字幕路径还要求三者的字幕 hash 一致；ASR 路径要求 request 字幕 hash 为 null，script/report 字幕 hash 相等且匹配新 SRT；
- report 的 voice、并发、bitrate 和 speed cap 与 request config 一致；
- report 的 audio/script hash 与实际文件字节一致；
- report `coverage.ordered_exactly_once=true`；
- report 与实际 cue/unit/segment 数量相等；
- report 与 `ffprobe` 的源/输出时长误差各不超过 0.1 秒；
- 输出与源时长差不超过 0.1 秒；
- `max_speed_ratio_observed <= max_speed_ratio_allowed <= 1.35`；
- validator 根据 script 的逐段 duration 字段重新计算 speed ratio 和最大值，不只信任 report；
- 输出 codec 是 AAC、sample rate 是 24000；high 的 ffprobe bitrate 在 192k 的 ±25% 内，standard/balanced 在 128k 的 ±25% 内；
- `media/`、`localization/`、`subtitles/` 中不存在当前 request ID 的最终产物 partial 文件。

修改 `validate_media_request`：合成中文音频调用该函数；删除 `audio_qa_report` 和 `report.passed` 检查。原生中文音轨仍只验证媒体与 provenance。

### B5. 修改 CLI

修改 `video_extract/cli.py`：

- `cmd_localize_audio` 不再 import `run_mlx_localization`；
- 参数改为位置参数 `PACKAGE` 和 `--json`；
- 先用 `shutil.which("audio-localize")` 取得绝对 executable，再使用 `subprocess.run([executable, "--package", str(package), "--json"], ...)`，不得使用 `shell=True`；
- 子命令成功后调用 adoption validator 并返回验证结果；
- 找不到 executable 时给出 `install audio-localization[tencent]` 的明确错误；
- help 文案改为 `run the package-scoped Tencent localization capability`；
- `doctor` 增加非强制检查 `audio-localize`；只有用户实际请求合成中文音频时缺失才阻塞。

### B6. 删除旧 MLX 执行代码

在新的 fake-provider 单元测试和 package 集成测试通过后：

- 删除 `video_extract/audio_localization.py`；
- 删除 `tests/test_audio_localization.py`；
- 确认仓库代码中没有 `run_mlx_localization`、`mlx-community/Qwen3`、`quality-report.json`、`playback_qa` 引用；
- 不删除 `faster-whisper`，因为 `video-learning` 无正式字幕时仍可能用它生成学习转写；
- 不删除 schema v1–v4 兼容用的 `podcast.py`、`tts.py`、`orchestrator.py`。

### B7. Schema 和文档

修改：

- `schemas/manifest-v5.schema.json`：允许第 6.3 节的 artifacts/provenance；
- `docs/package-contract.md`：补充 schema v5 中文音频完成规则；
- `README.md`：公开工作流改以 `--media audio --language zh-CN` 为主，`podcast_zh` 标为旧别名；
- `CONTEXT.md`：把“经过回听验证”改为“经过结构与时间轴验证”；
- 新增 ADR `docs/adr/0004-external-audio-localization-capability.md`，说明它是普通原子项目，不是 Skill，且 manifest 单写者是 `video-extract`；
- 在 ADR 中记录 `video-learning` 不调用该能力。

## 10. Skill 修改

### C1. `extract-media`

修改：

```text
/Users/syz/.agents/skills/extract-media/SKILL.md
/Users/syz/.agents/skills/extract-media/references/chinese-audio.md
/Users/syz/.agents/skills/extract-media/evals/evals.json
```

Skill 流程固定为：

1. 运行 `video-extract plan`；
2. 运行 `video-extract ensure`；
3. 如果返回 `awaiting_localization`，按返回的 argv command 运行 `audio-localize`；
4. 原样重跑 `video-extract ensure`；
5. 运行 `video-extract verify PACKAGE --json`；
6. 报告中文音轨来自原生音轨还是 Tencent synthesized，并报告耗时与 cache hits。

删除 Skill 中以下内容：

- MLX Whisper、Qwen TTS、source voice reference；
- 本地 0.6B/1.7B 模型；
- 分轮加载/卸载模型；
- playback QA、相似度、`quality-report.json`；
- “调用腾讯本地化 Skill”的任何措辞。

新增明确说明：`audio-localize` 是普通 CLI，不是 Skill；本流程没有 Skill-to-Skill 调用。

### C2. `video-learning`

不修改执行流程，也不改 Skill 文件。检查 `/Users/syz/.agents/skills/video-learning/SKILL.md` 及该文件直接引用的 references，回归确认：

- 仍只调用 `video-extract`；
- 不产生中文配音；
- 不调用 `extract-media`；
- 不调用 `audio-localize`；
- 继续复用同一 canonical package 的媒体、字幕和 manifest。

固定静态回归命令：

```bash
if rg -n 'extract-media|audio-localize|tencent-video-localization' \
  /Users/syz/.agents/skills/video-learning/SKILL.md \
  /Users/syz/.agents/skills/video-learning/references; then exit 1; fi
```

## 11. 测试清单

### 11.1 `audio-localization` 默认测试（无网络）

必须覆盖：

- request schema、语言、provider、路径逃逸与缺失输入；
- 相同输入/config 产生相同 request ID，任一输入字节或 config 变化会改变 ID；
- BOM、多行 SRT、逗号/点毫秒格式；
- 有字幕时 fake ASR 调用次数为 0；
- 无字幕时 fake ASR 被调用并生成 SRT；
- 源 SRT cue ID 重复时立即失败；
- source cue 有序恰好覆盖一次；
- 一个 unit 拆多个 TTS segment 不重复 source ID；
- source ID 重复、遗漏、额外、乱序全部失败；
- translation/TTS/ASR cache 二次运行零云调用；
- 单个损坏 cache entry 只重做该 entry；
- translation 并发结果仍保持输入顺序；
- TTS 并发结果仍保持时间轴顺序；
- 1.35x 以内通过，超过 1.35x 失败；
- 最终时长差大于 0.1 秒失败；
- report 缺失或 output/script hash 不匹配时不得视为完整提交；
- report 只包含 package-relative 路径；
- Credentials repr、异常和 JSON 不泄露密钥；
- CLI 退出码和 JSON code 符合第 7.1 节。

### 11.2 `video-extract` 默认测试（无网络）

必须覆盖：

- 原生中文音轨不生成 request、不要求凭据；
- 无中文音轨且有 SRT：生成 request，source subtitle 非 null；
- 无中文音轨且无 SRT：生成 request，source subtitle 为 null；
- 第一次 ensure 返回 `awaiting_localization` 与 argv 数组；
- 状态迁移表的每一行至少有一个参数化测试；
- high request 完成后以 standard 调用时不得先 adoption high 产物；源音频替换为同长度不同内容时同样不得 adoption；
- 无字幕 request 经腾讯 ASR adoption 后，第三次相同 ensure 必须沿用原 request ID、保持 `source.subtitle=null` 并直接 complete；
- 只提交新 request、尚未提交 manifest pause 的模拟崩溃可在下一次 ensure 自动 reconcile；
- fake `audio-localize` 写入有效产物后，第二次 ensure adoption 并 complete；
- 缺文件、坏 schema、绝对路径、越界路径、重复 source ID、时长错误均不得 adoption；
- 旧 request 的合法产物不得被新 request adoption；
- 合成音频不再要求 playback QA；
- `video-extract verify` 对有效 Tencent 产物通过；
- 原有 schema v1–v4 测试全部不回归；
- `notes_zh` 和 v5 `video/subtitles/original audio` 规划不新增 localization stage；
- `video-learning` 的测试路径不会执行 `audio-localize`。

### 11.3 静态检查命令

在 `audio-localization`：

```bash
cd /Users/syz/code/audio-localization
uv sync --extra test --extra tencent
uv run pytest -q
uvx --from 'ruff==0.16.7' ruff check src tests
uvx --from 'ruff==0.16.7' ruff format --check src tests
python3 -m compileall -q src
command -v audio-localize
audio-localize --help
```

在 `video-extract`：

```bash
cd /Users/syz/code/video-extract-core
uv run --with pytest pytest -q
uvx --from 'ruff==0.16.7' ruff check video_extract tests
python3 -m json.tool schemas/manifest-v5.schema.json >/dev/null
python3 -m compileall -q video_extract
if rg -n 'run_mlx_localization|mlx-community/Qwen3|quality-report.json|playback_qa' \
  video_extract tests pyproject.toml uv.lock; then exit 1; fi
```

最后一个检查必须无输出；文档历史记录可保留 MLX 字样，但生产代码和测试不可保留。另运行：

```bash
if rg -n 'MLX|Qwen|playback QA|quality-report.json|tencent-video-localization Skill' \
  /Users/syz/.agents/skills/extract-media; then exit 1; fi
```

## 12. Live 验收

### 12.1 前置检查

仅检查是否存在，不输出值：

```bash
test "$TENCENT_LIVE_TEST" = "1"
test -n "$TENCENTCLOUD_SECRET_ID"
test -n "$TENCENTCLOUD_SECRET_KEY"
command -v audio-localize
ffmpeg -version
ffprobe -version
```

如果密钥缺失，停止并报告“缺少环境变量”；不得读取 CSV 或其他凭据文件。

### 12.2 短样本

`tests/live/test_tencent_localization_live.py` 必须从 `TENCENT_LIVE_SOURCE_PACKAGE` 指向的 package 读取源音频和英文 SRT，并在 pytest 临时目录运行两个 case：

1. **with-SRT**：FFmpeg 截取前 52.76 秒，筛选并截断对应 cue，生成带 subtitle 的隔离 request；
2. **without-SRT**：FFmpeg 截取前 10 秒，生成 `subtitle=null` 的隔离 request，真实验证腾讯 ASR→TMT→TTS。

两个 case 都运行真实 `audio-localize` 和 `validate_localization_outputs`。测试结束由 pytest 清理隔离 package，不修改 canonical package。

固定命令：

```bash
export TENCENT_LIVE_TEST=1
export TENCENT_LIVE_SOURCE_PACKAGE=/Users/syz/Media/video-extract/items/youtube/jwChiek_aRY
uv run --project /Users/syz/code/video-extract-core --with pytest \
  pytest -q /Users/syz/code/video-extract-core/tests/live/test_tencent_localization_live.py -m live -s
```

测试内部必须从 CLI JSON 的 `cloud_requests_this_run` 断言：with-SRT 首次 `asr=0`；without-SRT 首次 `asr>=1`；两个 case 首次 translation/tts 都大于 0；结构验证都通过；第二次运行均为 `artifact_reuse=true` 且 asr/translation/tts 全为 0。不做人工试听或 playback ASR QA。缺少 flag、密钥、源文件或 executable 时 skip 并明确原因，不得把它记为代码测试失败。

### 12.3 33:52 性能样本

使用 canonical package：

```text
/Users/syz/Media/video-extract/items/youtube/jwChiek_aRY/
```

必须复用：

```text
media/audio.source.m4a
subtitles/source.en.srt
manifest.json
```

命令：

```bash
test "$TENCENT_LIVE_TEST" = "1"
audio_localize_executable=$(command -v audio-localize)
uv run --project /Users/syz/code/video-extract-core video-extract ensure \
  'https://www.youtube.com/watch?v=jwChiek_aRY' \
  --media audio --language zh-CN --quality high \
  --output /Users/syz/Media/video-extract/items/youtube/jwChiek_aRY --json \
  | tee /tmp/video-extract-jwChiek_aRY-first-ensure.json
jq -e '.status == "awaiting_localization" or .status == "complete"' \
  /tmp/video-extract-jwChiek_aRY-first-ensure.json
/usr/bin/time -p "$audio_localize_executable" \
  --package /Users/syz/Media/video-extract/items/youtube/jwChiek_aRY --json \
  | tee /tmp/video-extract-jwChiek_aRY-localize.json
uv run --project /Users/syz/code/video-extract-core video-extract ensure \
  'https://www.youtube.com/watch?v=jwChiek_aRY' \
  --media audio --language zh-CN --quality high \
  --output /Users/syz/Media/video-extract/items/youtube/jwChiek_aRY --json \
  | tee /tmp/video-extract-jwChiek_aRY-final-ensure.json
jq -e '.status == "complete"' /tmp/video-extract-jwChiek_aRY-final-ensure.json
uv run --project /Users/syz/code/video-extract-core video-extract verify \
  /Users/syz/Media/video-extract/items/youtube/jwChiek_aRY --json
```

如果第一次 ensure 已为 `complete`，说明相同 request 的有效产物已存在：只验证 artifact reuse，不再宣称测得“首次运行”性能。若必须重测首次性能，复制源音频和字幕到隔离测试 package；不得删除 canonical package 的已验证产物。

云端功能验收门槛：

- ASR 被跳过；
- 结构校验全部通过；
- 源 cue 有序且恰好覆盖一次；
- 输出和源时长差不超过 0.1 秒；
- 最大实际压缩不超过 1.35x；
- 最终音频为 `media/audio.zh-CN.m4a`；
- report 为 `localization/report.json`；
- manifest 不包含密钥、绝对缓存路径或签名 URL；
- CPU/GPU 内存中没有 MLX、Qwen 或本地 Whisper 本地化模型。

性能目标单独判定：`audio-localize` 首次运行总墙钟时间不超过 300 秒则为“性能目标通过”；超过 300 秒则为“云端功能通过、性能目标未通过”，并附阶段瓶颈报告。性能诊断只看报告中的四段耗时，依次检查 TTS 限流/重试、翻译请求粒度、重复 FFmpeg 编码、cache miss；不得先盲目提高并发。

## 13. 实施顺序与检查点

严格按以下顺序：

1. **A1 改名**：原 8 个测试通过。
2. **A2–A3 契约与覆盖修复**：新 request/script 测试通过。
3. **A4–A6 缓存、Provider、时间轴**：`audio-localization` 全部无网络测试通过。
4. **B1–B4 package 集成**：fake 子进程端到端测试通过。
5. **B5 CLI**：包装器与 executable discovery 测试通过。
6. **B6 删除旧 MLX 代码**：完整 `video-extract` 测试通过。
7. **B7 schema、文档**：静态检查通过。
8. **C1 Skill 更新**：eval 描述不再出现 MLX 或 playback QA。
9. **C2 video-learning 回归**：确认无 Skill-to-Skill 和无配音调用。
10. **短样本 live test**：用户允许且密钥已设置时执行。
11. **33:52 live test**：短样本通过后只执行一次首次运行，再验证一次 artifact reuse。

每个检查点失败时先修复当前阶段，不继续堆叠后续改动。

## 14. 回滚与数据安全

- 不移动或批量迁移 schema v1–v4 package。
- 不删除 canonical package 中的源音频、源字幕、源视频或 manifest。
- 创建新 request 时先撤销旧本地化登记；固定路径中的旧文件可暂留，但不再是事实源，并可能被新 request 的 report-last 提交流程替换。
- `.work/audio-localization` 是可恢复 cache，只有用户明确要求时才删除。
- `video-extract` 与 `audio-localization` 的代码回滚不删除 package 数据。
- 若外部 `audio-localization` 不可用，`video-extract ensure` 保持 `awaiting_localization` 并返回可执行恢复命令，不回退到本地模型。

## 15. 完成定义

### 15.1 实现完成

以下条件全部满足即可标记“实现完成”，不以密钥或 live test 授权为前提：

- 腾讯实现已成为普通 `audio-localization` 项目，仓库内没有 Skill entry/evals；
- `extract-media` 和 `video-learning` 仍是两个互不调用的顶层 Skill；
- `video-extract` 是 manifest 唯一写入者；
- `audio-localization` 只通过 `request.json` 读写同一个 canonical package；
- 原生中文音轨完全绕过腾讯云；
- 有英文 SRT 的合成路径完全绕过 ASR；
- 无字幕路径使用腾讯 ASR，不加载本地本地化模型；
- 旧 MLX 本地化生产入口已删除；
- playback QA 及其完成条件已删除；
- ordered exactly-once coverage 缺陷已修复并有回归测试；
- 两个项目的默认测试和静态检查通过；
- 所有日志、错误和产物均不包含腾讯密钥。

### 15.2 云端功能验收完成

用户明确授权、设置 `TENCENT_LIVE_TEST=1` 和两个腾讯密钥环境变量后，再要求：

- 短样本 live 验收通过；
- 33:52 样本结构验证通过；
- `video-extract verify PACKAGE --json` 对最终 package 返回成功；
- 相同 request 重跑 `artifact_reuse=true` 且不产生新云调用；
- 最终交付路径、耗时、cache hits、字幕来源和 Tencent provenance 可从 manifest/report 读取。

### 15.3 性能目标

- 33:52 样本首次运行不超过 300 秒：`性能目标通过`；
- 超过 300 秒：`性能目标未通过`，附 translation/TTS/timeline/total 指标和明确瓶颈；
- 性能目标未通过不应误报为云端功能失败，也不能误报为五分钟目标达成。

没有密钥或用户未授权 live test 时，最终状态应报告为“实现完成，云端验收待授权”，不得报告为代码失败。
