# VideoLearning 工作区重构执行计划

> 状态：DRAFT — 等待用户 Review。当前文件只定义未来执行方案；在用户明确批准并要求执行前，不得移动目录、重写配置、初始化 Git 或修改 Skill。

## 1. 执行目标

把当前分散的代码、Media 数据和 Obsidian Vault 收拢到一个共同父目录，同时保持三者边界独立：

```text
/Users/syz/VideoLearning/
├── workspace.toml
├── video-extract-core/          # 独立代码项目；本地 Git 仓库
├── media/                       # 权威媒体数据；不进入 Git
└── 学习系统/                    # 独立 Obsidian Vault
    ├── .obsidian/
    ├── .claudian/
    ├── 收件箱/
    ├── 资料库/
    │   └── 网站视频转录/        # video-extract 完全管理的派生阅读层
    ├── threads/                 # learning-learn 的 Living Learning Documents
    ├── concepts/                # learning-learn 的独立可复用 Concepts
    └── REVIEW.md                # 仅在首次接受 Memory Target 时由 learning-learn 创建
```

重构后满足：

1. 整个工作区可以整体搬迁，相对路径保持稳定。
2. 代码只管理程序、格式、Schema 和生成规则。
3. `media/` 是视频、音频、字幕、源笔记、证据和 manifest 的权威来源。
4. `学习系统/资料库/网站视频转录` 是可重建的 Obsidian 阅读副本。
5. `threads/`、`concepts/` 和 `REVIEW.md` 是不可覆盖的长期学习成果。
6. 新电脑只需复制 Media、同步或复制 Vault、拉取代码、填写一个本机定位器并执行重建命令。

## 2. 明确不做的事情

- 不把 `media/` 放入代码 Git 仓库。
- 不把大视频、WAV、缓存或全部候选截图复制到 Obsidian。
- 不把 `threads/`、`concepts/` 或 `REVIEW.md` 当成可重建输出。
- 不修改 `learning-learn` 的 Learning Thread、Concept 或 Review 语义。
- 不创建空的 `REVIEW.md`；必须遵守 `learning-learn` 的首次 Memory Target 接受规则。
- 不引入 RAG、向量数据库、云数据库或第二套索引。
- 不删除现有 migration quarantine。
- 不配置 Git remote，不 push，不猜测 Git 用户身份。
- 不在本计划 Review 阶段执行任何文件移动。

## 3. 当前基线

执行者必须重新测量，不得只相信下列快照：

| 项目 | 当前状态 |
|---|---|
| 代码 | `/Users/syz/code/video-extract-core`，约 366 MB，当前不是 Git 仓库 |
| Media | `/Users/syz/Media/video-extract`，约 16 GB |
| 当前 Obsidian Vault | `/Users/syz/code/obsidian_本地知识库/编程开发`，约 77 MB |
| 当前自动输出 | Vault 内的 `网站视频转录/` |
| 当前个人区 | `收件箱/` |
| Learning artifacts | 当前未发现 `threads/`、`concepts/`、`REVIEW.md` |
| 外层遗留 | `/Users/syz/code/obsidian_本地知识库/source` 当前为空 |
| 测试基线 | 79 passed、1 skipped、11 subtests passed |

代码中的已知路径耦合至少包括：

- `video_extract/package_paths.py`
- `video_extract/cli.py`
- `output_paths.py`
- `video_extract/obsidian_export.py` 生成的 Properties 和 `file://` 链接
- `README.md` 和部分设计文档
- `/Users/syz/.agents/skills/video-learning/SKILL.md`
- `/Users/syz/.agents/skills/video-learning/references/evidence-notes.md`

## 4. 数据所有权

| 区域 | 权威性 | 是否可重建 | 写入者 |
|---|---|---:|---|
| `video-extract-core/` | 程序事实源 | 可从 Git 恢复 | 开发者、Codex |
| `media/items/` | 媒体与源笔记事实源 | 否 | video-extract pipeline |
| `media/collections/` | 集合目录与顺序事实源 | 部分 | video-extract pipeline |
| `media/catalog/library.sqlite` | 搜索派生物 | 是 | `library rebuild` |
| `media/playback/` | 播放派生物 | 是 | `playlist` |
| `学习系统/资料库/网站视频转录/` | 阅读派生物 | 是 | `export-obsidian` |
| `学习系统/threads/` | 学习事实源 | 否 | 用户、`learning-learn` |
| `学习系统/concepts/` | 概念事实源 | 否 | 用户、`learning-learn` |
| `学习系统/REVIEW.md` | 复习队列事实源 | 否 | 用户、`learning-learn` |

`video-extract` 的任何命令都只能在 `资料库/网站视频转录` 这个受管区域内创建、更新或清理文件。它必须拒绝写入 `threads`、`concepts`、`REVIEW.md`、`收件箱` 和其他 Vault 路径。

## 5. 配置契约

### 5.1 根配置

在 `/Users/syz/VideoLearning/workspace.toml` 建立唯一工作区配置：

```toml
schema_version = 1

[paths]
project = "video-extract-core"
media = "media"
obsidian = "学习系统"

[obsidian]
generated = "资料库/网站视频转录"
threads = "threads"
concepts = "concepts"
review = "REVIEW.md"
```

所有相对路径均相对于 `workspace.toml` 所在目录解析。解析后必须使用 `Path.resolve()`，并验证受管路径没有逃出对应根目录。

### 5.2 配置发现优先级

CLI 按以下顺序寻找配置：

1. 显式 `--workspace /path/to/workspace.toml`。
2. 环境变量 `VIDEO_EXTRACT_WORKSPACE`。
3. 从当前目录向父目录查找 `workspace.toml`。
4. 用户定位器 `~/.config/video-extract/config.toml` 中的 `workspace = "/absolute/path/to/workspace.toml"`。
5. 找不到时返回明确错误和初始化提示；新工作流不得静默退回 `/Users/syz/...` 硬编码路径。

用户定位器是唯一允许的机器相关绝对路径。跨电脑时只需更新这个文件一次。

### 5.3 兼容性

- 现有命令显式传入 `--library-root`、`--root`、`--output-root` 时继续可用。
- 未显式传路径时改从 WorkspaceConfig 读取。
- manifest 内部 artifact 路径继续保持包内相对路径。
- Obsidian 中的 `file://` 源视频链接属于派生内容，迁移后通过重新导出更新。
- Properties 新增或保留可移植字段：`item_id`、`platform`、`package_relpath`、`source_video_relpath`。
- 可保留派生的绝对 `package_path`、`source_video` 以支持本机点击，但不得将其作为身份或索引主键。

## 6. 新 CLI 契约

新增 `video-extract workspace` 命令组：

```text
video-extract workspace show [--workspace PATH] --json
video-extract workspace doctor [--workspace PATH] --json
video-extract workspace rebuild [--workspace PATH] --dry-run --json
video-extract workspace rebuild [--workspace PATH] --apply --json
video-extract workspace prepare-migration [--workspace PATH] [--full-hash] --json
```

### `workspace show`

只读输出配置来源、工作区根目录、解析后的项目/Media/Vault/生成区路径。不得创建文件。

### `workspace doctor`

只读检查：

- 配置 Schema 和路径 containment。
- 工具依赖。
- Media package 数量、状态分层和缺失文件。
- SQLite stale/missing path。
- Obsidian 受管区断图、孤儿笔记、大媒体和越界输出。
- Bases YAML 基本结构。
- playback 断软链接、非 MP4 条目和真实文件副本。
- `threads`、`concepts`、`REVIEW.md` 的边界。
- live code、Skill 和当前生成文件中的旧绝对路径。

历史 migration reports 可以记录旧路径，但必须在报告中单独分类为 historical reference。

### `workspace rebuild`

固定顺序：

1. WorkspaceConfig 与依赖预检。
2. Media 全库结构审计；不修补历史内容事实。
3. 临时构建 SQLite 后原子替换。
4. 临时构建 Obsidian 受管区，验证后用同设备 rename 切换。
5. 重建 playback 软链接和 M3U8。
6. 运行最终 doctor。

`--dry-run` 不得写 journal、SQLite、Obsidian 或 playback。`--apply` 才允许写入。

### `workspace prepare-migration`

只生成迁出清单，不复制文件：

- 必须迁移：`media/items`、`media/collections`、整个 Obsidian Vault 中的非派生学习成果、workspace 配置。
- 可重建：SQLite、playback、`资料库/网站视频转录`。
- 可选 `--full-hash` 对不可重建文件生成 SHA-256；默认只记录路径、size、mtime 和关键 Markdown/manifest hash。
- 输出新电脑恢复步骤和旧电脑安全删除前的验收条件。

## 7. Obsidian 与 learning-learn 契约

Vault 根目录是 `/Users/syz/VideoLearning/学习系统`，不是 `学习系统/学习系统`。

在 Vault 根目录添加 `AGENTS.md`，内容只表达以下约束：

- Living Learning Documents 存放在 `threads/`。
- 独立 Concepts 存放在根级 `concepts/`。
- 唯一 Review Queue 是根级 `REVIEW.md`，仅按 `learning-learn` 规则懒创建。
- `资料库/网站视频转录` 是只读 Source Boundary，可被学习文档引用，不由 `learning-learn` 修改。
- Learning Thread 使用相对 Markdown 链接引用视频阅读笔记。

将 Vault 根目录保存为一个独立 Codex 项目。调用 `learning-learn` 时，应在该项目运行，或显式提供 Living Learning Document 路径。不得把学习文档写到代码仓库。

## 8. 实施阶段

### 阶段 0：冻结与基线

1. 确认无下载、转写、导出、索引或播放重建进程。
2. 提醒用户关闭 Obsidian；若 Obsidian 仍持有当前 Vault，真实 cutover 前停止。
3. 确认以下路径位于同一文件系统设备：旧代码、旧 Media、旧 Vault、目标父目录。
4. 运行完整测试、现有 doctor、Media validation、Obsidian 断链检查和 playback 检查。
5. 记录文件数量、目录大小、关键 inode、package 数量、导出笔记数、图片数和软链接数。
6. 写入只读基线报告；此阶段移动数必须为 0。

完成标准：基线可重复，所有已知历史 invalid 被分类，`migration_regression=0`。

### 阶段 1：建立 Git 回滚基线、回归保护和 WorkspaceConfig

在修改源码前扩充 `.gitignore` 并扫描大文件、疑似凭证、浏览器会话和第三方临时 checkout。确认 Git 候选清单不包含 Media、Vault、Cookie、`.lark-vc-auth.png` 或其他私密文件后执行 `git init`。若系统已有 Git identity，创建本地 baseline commit；若没有，保留初始化状态并报告，不猜身份、不 push。Git 初始化失败不授权跳过文件安全审计。

新增：

- `video_extract/workspace.py`
- `tests/test_workspace.py`
- `workspace.example.toml`

测试先行覆盖：

- 相对路径随工作区整体移动而重新解析。
- discovery precedence。
- path containment 和 symlink escape。
- 缺配置时明确失败。
- 显式旧 CLI 路径参数仍可用。
- 配置读取不产生文件写入。

完成标准：新测试先红后绿，原完整测试仍通过。

### 阶段 2：消除 live 硬编码路径

依次改造：

- `video_extract/package_paths.py`
- `video_extract/cli.py`
- `video_extract/obsidian_export.py`
- `video_extract/library.py`
- `video_extract/playlists.py`
- `output_paths.py`
- 仍在正式入口使用的顶层脚本

更新 Obsidian Properties 为相对身份字段；绝对字段只能是重建产生的本机便利字段。

完成标准：临时复制一份 fixture workspace 到不同父路径，无代码修改即可通过 show、doctor、export、index 和 playlist 测试。

### 阶段 3：实现 Workspace 命令

在 CLI 中实现第 6 节五个命令。重建必须调用现有模块，不复制第二套导出、索引或 playlist 逻辑。

Obsidian 受管区需要 marker，例如：

```text
资料库/网站视频转录/.video-extract-managed.json
```

marker 记录 workspace schema、生成器版本、上次 rebuild 时间、条目数和文件 manifest。只有 marker 与配置共同确认的目录才能被原子替换。

完成标准：连续执行两次 rebuild，第二次结果幂等；在 `threads` 写 fixture 后 rebuild，fixture hash 完全不变。

### 阶段 4：Git 边界复核和文档准备

扩充代码项目 `.gitignore`，至少排除：

- `.venv/`、cache、`__pycache__/`
- `.lark-vc-auth.png` 和其他凭证/会话文件
- `work/` 中的浏览器状态和第三方临时 checkout
- 本机 `workspace.toml`
- 所有 Media、Vault 或迁移报告路径

重新扫描大文件和疑似凭证，确认 Git 工作树只包含代码与文档。若阶段 1 已创建 baseline commit，则将重构源码作为独立本地 commit；若 Git identity 仍缺失，只保留清晰 diff 并报告，不猜身份、不 push。

更新：

- `README.md`
- `CONTEXT.md` 中必要的架构说明
- `docs/package-contract.md` 的路径约定
- 新增 `docs/workspace-migration.md`

历史设计文档可以保留旧路径，但必须标记为 historical，不能作为当前操作指南。

### 阶段 5：Cutover dry-run

生成唯一移动计划：

```text
/Users/syz/code/video-extract-core
  -> /Users/syz/VideoLearning/video-extract-core

/Users/syz/Media/video-extract
  -> /Users/syz/VideoLearning/media

/Users/syz/code/obsidian_本地知识库/编程开发
  -> /Users/syz/VideoLearning/学习系统

学习系统/网站视频转录
  -> 学习系统/资料库/网站视频转录
```

dry-run 必须确认：

- 每个来源存在且目标不存在。
- 每个 move 都是同设备 rename。
- Obsidian generated 区无未登记人工修改；若存在则停止并列出 diff。
- `收件箱`、`.obsidian`、`.claudian` 全部被纳入 Vault 目录 rename。
- 外层空目录 `/Users/syz/code/obsidian_本地知识库/source` 不混入 Vault。
- 没有 active writers。

完成标准：所有来源 exactly once accounted，冲突、缺失、EXDEV、越界均为 0；真实移动仍为 0。

### 阶段 6：真实同设备 rename

1. 创建 `/Users/syz/VideoLearning`。
2. 写 append-only journal，记录每个目录 move 的 planned/completed 事件、dev、inode、mtime。
3. 使用目录级 rename 完成三个根目录移动；不得复制 16 GB Media。
4. 在新 Vault 中创建 `资料库/`，把现有 `网站视频转录` rename 到其下。
5. 创建 `threads/` 和 `concepts/`；不创建 `REVIEW.md`。
6. 写根级 `workspace.toml` 和 Vault `AGENTS.md`。
7. 所有后续命令使用新绝对工作目录；不得依赖已消失的旧 cwd。

只有在完整验证后，才可对已经确认为空的旧外层目录使用精确 `rmdir`。不得递归删除旧父目录。

完成标准：planned/completed journal 数一致，来源残留 0，目标缺失 0，目录 inode 与 rename 预期一致。

### 阶段 7：重建派生层

1. 配置用户定位器 `~/.config/video-extract/config.toml`。
2. 从新代码路径运行 `uv sync`。
3. 执行 `uv tool install --force --editable /Users/syz/VideoLearning/video-extract-core`，使 Skill 可以从任意工作目录调用同一 CLI；若 `uv tool` 不可用则停止并报告，不创建自定义 shell 别名绕过。
4. 运行 `workspace rebuild --dry-run`。
5. dry-run 全绿后运行 `workspace rebuild --apply`。
6. 重建 SQLite、Obsidian generated 区、Properties、Bases、playback 和 M3U8。
7. 确认 0 个断图、0 个 broken symlink、0 个 stale SQLite path。
8. 用 Obsidian 1.13.7 实际打开表格和卡片视图，二者必须显示非零且一致的结果数。
9. 用 IINA 打开第十六章播放目录，保持 13/13。

完成标准：最终 `workspace doctor` 返回结构通过；历史内容 invalid 继续如实分层，但 `migration_regression=0`。

### 阶段 8：Skill 切换

更新 `video-learning` Skill：

- 删除 `/Users/syz/...` 的代码、Media 和 Obsidian 硬编码。
- 调用已安装或当前环境可解析的 `video-extract` CLI。
- 使用 WorkspaceConfig 获取 package、export、library 和 playlist 路径。
- 保持固定顺序：Media package → verify → Obsidian export → library update → collection playlist。

`learning-learn` 主 Skill 不硬编码本机路径。通过 Vault 根级 `AGENTS.md` 和 Codex 项目根目录让它自然解析 `threads`、`concepts`、`REVIEW.md`。

完成标准：

- `video-learning` 用一个小型本地 fixture 完整跑通。
- `learning-learn` 的路径解析测试在临时 Vault 中把 Living Document 放进 `threads/`，Concept 放进 `concepts/`；未接受 Memory Target 时不创建 `REVIEW.md`。
- 所有 smoke fixture 在验收后进入明确的测试临时目录，不污染真实学习成果。

### 阶段 9：最终退役报告

报告至少包含：

- 旧路径存在性和新路径存在性。
- journal 计划/完成数。
- package、笔记、图片、SQLite 条目、播放链接前后数量。
- tests、doctor、Obsidian UI、IINA、Skill smoke 结果。
- live old-path references。
- historical references。
- quarantine 内容和大小；不删除。
- Git 初始化/commit 状态；不 push。
- 新电脑恢复命令。

只有以下条件全部满足，才可标记 `operational_cutover_status=complete`：

- `migration_regression=0`
- `missing_targets=0`
- `conflicts=0`
- `broken_obsidian_images=0`
- `broken_playback_links=0`
- `sqlite_stale_paths=0`
- 完整测试通过
- 新项目和 Vault 可正常打开
- `threads`、`concepts`、`REVIEW.md` 边界未被侵犯

## 9. 全局停止条件

出现任一情况立即停止，不扩大处理范围：

- 来源与目标不在同一文件系统，rename 返回或预示 `EXDEV`。
- 目标目录已存在且非空。
- Obsidian 或 pipeline 仍在写相关目录。
- generated 区发现 export-manifest 未登记的人工修改。
- 同一来源被计划两次或同一目标接收不同内容。
- package identity 不明确。
- journal 无法追加或 planned/completed 不一致。
- 真实移动后出现缺失目标、inode 异常或源残留。
- rebuild 试图写入 `threads`、`concepts`、`REVIEW.md` 或 `收件箱`。
- 测试、doctor、Obsidian UI 或 playback 验收出现迁移回归。
- Git 暂存区包含 Media、Vault、凭证或浏览器会话。

停止时保留现场和 journal，只读生成报告；不得用复制、覆盖或递归删除绕过问题。

## 10. 交给执行 Agent 的指令

将本文件原样提供给 `gpt-5.6-sol`、reasoning effort `low` 的 worker，并附加：

```text
你负责完整实施 docs/workspace-refactor-execution-plan.md。

你不是单独在文件系统中工作；保留用户和其他 Agent 的改动，不回退无关内容。先完整读取计划、CONTEXT.md、相关 Skill 和当前代码。严格按阶段执行并在每一阶段向父 Agent 汇报：状态、计划移动数、实际移动数、冲突、缺失、报告路径、下一阶段。

阶段 0–5 未全部通过前，不得真实移动目录。大资源迁移只允许同设备 rename，不允许先复制一份，不允许 cp/rsync/shutil.copytree。Obsidian 小型派生层只能由受管重建流程生成。禁止覆盖人工学习成果，禁止删除 quarantine，禁止 push。

遇到全局停止条件立即停止并报告；不要自行放宽验收。只有所有 operational cutover 条件通过后才报告完成。
```

## 11. Review 时需要确认的决策

用户批准本计划即代表接受以下具体选择：

1. 共同父目录固定为 `/Users/syz/VideoLearning`。
2. Obsidian Vault 根目录固定命名为 `学习系统`。
3. 自动视频阅读区固定为 `资料库/网站视频转录`。
4. `learning-learn` 使用根级 `threads/`、`concepts/` 和懒创建的 `REVIEW.md`。
5. 代码项目初始化本地 Git，但不配置 remote、不 push、不猜 Git identity。
6. 当前 16 GB Media 使用同设备 rename，不生成安全副本。
7. 当前 Vault 整体移动；`.obsidian`、`.claudian`、`收件箱` 一并保留。
8. migration quarantine 保留，不在本次重构中清理。
