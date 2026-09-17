# GitHub 小鹅通下载工具调研

调研日期：2026-09-16

## 结论

GitHub 上确实有小鹅通下载工具，但没有一个方案能仅凭课程首页 URL，稳定适配所有店铺模板并完全无感下载。实现较成熟的项目采用的共同链路是：

1. 在真实浏览器环境中登录并保留持久会话。
2. 从页面 DOM、Vue 状态或小鹅通私有接口取得课程目录、`resource_id`、`userId` 和 `play_sign`。
3. 逐课打开播放页，从播放接口、Vue 状态或浏览器资源记录中捕获带签名的 m3u8。
4. 下载 m3u8 与分片，保留签名查询参数；如存在加密则获取并处理 AES Key，最后用 ffmpeg 合并。

对当前项目最重要的启示是：**登录、目录解析和播放地址捕获必须尽量在同一个持久浏览器会话内完成，不应登录后关闭浏览器，再另启一个无头会话。** 后一种做法即使复制了部分 Cookie，也可能丢失页面运行态、会话存储、设备特征或动态生成的播放参数。

## 主要项目及做法

### 1. Clearner1/xiaoetong-video-downloader

仓库：<https://github.com/Clearner1/xiaoetong-video-downloader>  
调研提交：`800a9507a753a4b3d241c2c944eafaf59ebd022a`

这是与当前需求最接近的实现，使用 Electron 的 `BrowserWindow`：

- 登录窗口和解析窗口共同使用 `session.fromPartition('persist:xiaoe')`，登录态通过同一个持久 partition 复用。
- 从 `ctx_user_id` Cookie、Vue store、页面全局变量或 `window.pushData.payload.userId` 自动取得 userId。
- 目录不只依赖单一接口，而是自动点击“目录”、滚动、展开更多，并读取 Vue 组件树中的章节数据。
- 对每节课依次导航到播放页，并用三种策略取得 m3u8：
  - `detail_info` + `getPlayUrl` 私有接口；
  - 深度遍历 Vue 组件状态，搜索 `.m3u8`；
  - 检查 `performance.getEntriesByType('resource')` 中的资源请求。
- 下载时保留 m3u8 URL 原始查询参数，避免重编码破坏服务端签名。
- AES Key 请求失败时追加 `uid=<userId>`；代码还会尝试将返回的 Key 与 userId 做 XOR，再进行 AES-128-CBC 解密。

局限：项目自身也说明，高度定制的店铺页面可能无法自动解析，必要时仍要从已打开的播放页捕获 m3u8。其实现虽然复用持久 partition，但仍会在登录后关闭登录窗口、另建解析窗口；对防自动化更严格的店铺，我们应进一步收敛为同一个长期驻留的浏览器上下文。

### 2. miaoyc666/xiaoetong-video-downloader

仓库：<https://github.com/miaoyc666/xiaoetong-video-downloader>  
调研提交：`e0996c4dfb78a6112e1be11f8631f135082e368c`

这是 Python + `requests.Session` 方案：

- 用户手工配置 `app_id`、`product_id` 和完整 Cookie。
- 调用 `column.items.get` 获取目录，调用 `video.detail_info.get` 取得 `play_sign`，再调用 `material-center.play/getPlayUrl` 获取不同清晰度的播放 URL。
- 后续解析 m3u8、下载分片并转码。

局限很明显：README 已标注项目正在重写、近期不建议使用；Cookie 需要人工提取且会过期；接口和 Host 写得较固定，对我们的 `pomoho.com` 定制域名及特殊课程模板不够稳健。

### 3. jiji262/xiaoetong-video-downloader

仓库：<https://github.com/jiji262/xiaoetong-video-downloader>

较早期的扫码登录 + API 下载方案。其说明中提到会话大约维持数小时，切换店铺时通常要重新扫码。它验证了“先拿会话，再调用目录/播放接口”的基本路线，但接口年代较早，不适合作为当前实现基础。

### 4. nemoTyrant/goose

仓库：<https://github.com/nemoTyrant/goose>

该工具不负责登录和目录自动化，而是让用户在 Chrome 开发者工具中自行捕获单个视频的 m3u8，然后完成 AES-128 解密、分片下载和 ffmpeg 合并。它适合作为失败兜底，但达不到“只告诉章节编号”的体验。

### 5. MediaGo

仓库：<https://github.com/Sophomoresty/mediago>  
调研提交：`145906e57e5b1e37272cab7aa28deb9d42379ee8`

这是通用流媒体提取器，值得借鉴的是“站点提取器”和“HLS 下载内核”分层，而不是把登录、目录识别、m3u8 捕获和分片下载揉在一个脚本里。当前仓库没有发现一个可直接复用、明确针对我们这个小鹅通页面的提取器，因此不建议直接替换现有实现。

## 对当前实现的诊断

目前出现“调试浏览器能看到目录，但后台 Playwright 跳回商品页/权限页”的核心原因，很可能是以下几项叠加：

1. **会话边界变化**：登录浏览器和后台下载浏览器不是同一进程、同一 context 或同一完整存储状态。
2. **过度依赖目录接口**：定制店铺可能没有走我们预期的 `resource_catalog_list` 请求，目录已经存在于 Vue 状态或渲染 DOM 中。
3. **播放凭证是动态链路**：只有 Cookie 不足以直接构造 m3u8；还需要从课程详情取得 `play_sign`，并带 `userId` 调用播放接口。
4. **下载端签名容易被破坏**：m3u8 和 TS URL 的查询参数可能有签名，重新编码或遗漏继承参数都会返回无效内容。
5. **可能存在二次密钥处理**：部分小鹅通视频不能仅交给 yt-dlp/ffmpeg；Key 可能需要附加 uid，并根据 userId 做 XOR 后才能解密。

## 推荐实现方案

### A. 驻留式浏览器捕获器

- 首次运行打开一个专用、有界面的 Chromium，使用固定的本地 profile。
- 用户只在真正失效时扫码；登录成功后浏览器不退出，只隐藏或最小化。
- 目录解析、逐课打开、网络监听和 JS 状态读取都在这个同一 context 中执行。
- 不连接用户日常 Chrome profile，避免 profile 锁、隐私暴露和 DevTools 调试端口风险；专用 profile 仍能达到一次登录、长期复用。

### B. 多路目录解析

按优先级组合，而非押注一个接口：

1. 已捕获的目录 API 响应；
2. Vue/Pinia 页面状态；
3. DOM 自动展开和滚动后的链接、标题及 `resource_id`；
4. 已保存 manifest 作为短期缓存。

目录先标准化成稳定的序号、章节名、资源 ID 和播放页 URL，再解释“17-23”这样的自然语言范围。

### C. 多路播放地址捕获

进入每节课后并行/轮询：

1. 浏览器网络事件中的 `.m3u8`；
2. Performance Resource Timing；
3. Vue 页面状态；
4. `detail_info` 取得 `play_sign`，再由当前页面上下文调用 `getPlayUrl`。

捕获后立即将播放 URL、Referer、User-Agent、必要 Cookie 和 userId 交给下载队列，避免短效签名过期。

### D. 小鹅通专用 HLS 下载内核

- 原样继承 m3u8 URL 的签名查询字符串，不对签名值二次编码。
- Key 请求支持追加 `uid`。
- 同时检测 raw key 和 `key XOR userId` 两种模式，以 TS 同步字节校验正确结果。
- 支持断点续传、失败分片重试、下载清单和最终 ffmpeg remux。
- yt-dlp 保留为普通 HLS 快速路径；检测到小鹅通加密时切换专用内核。

## 建议的用户体验

首次：

```text
请登录小鹅通 → 登录成功，专用会话已保存
```

以后：

```text
用户：下载小沫机器人的 17-23 章
工具：自动恢复会话 → 读取目录 → 捕获 7 节播放地址 → 后台下载
```

仅当小鹅通主动让会话失效、要求验证码或出现新的授权确认时，才再次显示浏览器让用户处理。正常情况下不应反复弹窗。

## 实施优先级

1. 将现有“登录后退出，再启动后台 Playwright”改为驻留式同 context 工作流。
2. 移植 Clearner1 项目的 Vue/DOM 目录提取思路和三路 m3u8 捕获策略。
3. 给下载器增加签名查询参数继承、uid Key 请求和 XOR Key 检测。
4. 增加章节范围选择、manifest 缓存、断点续传和会话失效自动回退。
5. 用当前“小沫机器人”课程的 17-23 章做端到端验收。

以上方案仅用于用户有权访问的已购课程及个人离线观看，并应遵守平台条款和内容权利限制。
