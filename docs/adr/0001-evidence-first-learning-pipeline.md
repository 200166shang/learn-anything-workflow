# Adopt an evidence-first staged learning pipeline

**Status: accepted**

学习资料采用“单视频笔记 → 显式请求的集合汇总”的两层结构，并把媒体、转写、候选画面、证据选择和笔记拆成可重跑阶段。候选与证据选择是两个状态；默认模型审阅显式记录为 `model_only`，人工审阅才记录为 `human`。这个方案保留 Markdown 作为长期格式，同时用 schema v3 JSON 保存机器中间结果和审阅语义。

稳定 `video-extract` CLI 与权威 validator 构成项目 interface。Collection 的目录/汇总状态与 item 状态分开，schema 1/2 只做非破坏兼容。单一 `video-learning` skill 在此 seam 上编排；acquisition 是按需 reference，不是独立 operational skill。各阶段采用独立并行策略，validated fingerprint 支持恢复与缓存复用。

## Considered options

- 一次性把整段转写交给 AI：实现简单，但证据绑定、失败重跑和截图确认都不可控。
- 不记录审阅语义地自动选图：吞吐量高，但无法区分模型选择和人工批准。
- 只生成课程级总结：阅读短，但会丢失视频级时间戳和局部上下文。

## Consequences

- 需要维护中间 JSON、截图候选和确认结果等额外文件。
- 默认可由模型选择截图；用户明确要求时保留人工审阅停点。
- 后续可以独立替换 OCR、截图算法或 AI 模型，而不必重新下载和转写视频。
