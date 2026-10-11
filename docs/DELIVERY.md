# DS Code 修复与重构交付

日期：2026-10-11。仓库：<https://github.com/zerobudian/DS-code>。

## 源码依据与记忆限制

本次基线为 GitHub 默认分支 `main` 的 `6d07c34629fa5a68a451055f68009e8d784e8be4`，提交时间 2026-07-12 09:59:32 UTC。通过 GitHub 连接器与本地 `git ls-remote` 独立核对。另一个分支 `DS-code-2.0.1-0711-NEXT` 位于 `36879be42eb9affb3318fa2b8bab5e564ace0488`，已合入 main；源码基线采用 main，不采用旧目录或历史压缩包。

当前会话的工具列表与资源列表未提供历史记忆读取接口，工作区与 Codex 目录也未发现 DS Code 分析记忆文件。因此本报告不声称恢复了此前对话中的问题清单。仓库没有独立 issues 清单，只有已合并的 PR #1；问题依据来自当前源码、复现与测试。源码中的 C/P/L 等审计编号是代码注释，不等同于已获取历史审计报告。

## 已复现并修复

| 问题 | 证据与修复 |
| --- | --- |
| 不完整的启动目录与重复实现 | README 指向缺少 Electron/Sidecar/engine 的 demo 副本。现在根目录为唯一源码入口，旧脚本转发，备用 Sidecar 入口转发到同一服务。 |
| 桌面进程管理缺失 | 原 Electron 定义 `startSidecar()` 却从未调用，脚本按端口强制杀进程。现在 Electron 启动自己持有的 child，校验 HTTP 服务身份与令牌，退出仅停止该 child。 |
| HTTP 分包与长度错误 | 分开发送请求头和 body 时返回 `400 OK / 无效 JSON`；单次 read 最多 64KB。现在按 Content-Length 完整读取，限制头/body、读取时间并返回正确状态码。 |
| 请求串流与重复发送 | 原所有请求消费同一全局缓冲，每次新请求清空。现在每次 POST 创建独立 run，拒绝并发、隔离事件，SSE 只消费指定 run，不自动重发模型请求。POSIX 下检测父进程消失并退出，避免异常退出残留。 |
| 停止、确认与计划缺乏闭环 | 前端未回复确认事件；错误后流未及时关闭。现在命令、编辑、问答使用请求级确认 ID；过期回复拒绝，停止唤醒确认门，断线/超时取消。计划接受后继续执行。 |
| 停止发生在发送初始化前被重置 | 引擎发送入口会重置 stop 标志。新增请求取消 Event，初始化也无法覆盖取消状态。 |
| 配置部分保存、供应商更新不生效 | 原逐字段落盘；active_provider 优先读旧值。现在先校验整批输入，一次保存，同步 active_provider，保存异常恢复内存状态；拒绝脱敏占位符。 |
| 自定义模型配置字段错配 | 前端发 context1m，后端读 context_1m。现在统一字段并兼容旧输入，模型列表与状态栏使用实际自定义名称。 |
| API 客户端替换后连接被关闭 | 原新旧 OpenAI client 共享诊断 HTTP session，关闭旧 client 会影响新 client。现在每个 SDK client 独立持有 transport。 |
| 模型/余额 URL 被忽略 | 原只取 hostname，丢失 scheme、port、base path。现在使用同一配置 URL 和 TLS 策略，诊断请求在线程中运行，不阻塞 HTTP/SSE 事件循环。 |
| 官方余额为零 | 引擎原只读取不存在的顶层余额字段。现在解析 balance_infos、优先 CNY，不把不同货币直接相加。 |
| 原生工具调用丢失 | 流末尾 usage chunk 覆盖已合并的 tool_calls。改为更新 usage 字段，保留参数与工具名分片；已验证真实 SDK 下创建文件及匹配 tool_call_id 的结果回传。历史转换只输出 API 标准字段，移除内部 index/工具执行字段与非标准消息 id，旧文本工具保持兼容。 |
| 历史不可见、清空不清引擎 | 原前端切换项目只清画面，/clear 只发 GUI 回调。现在有历史读取与恢复，/clear 清空并持久化引擎历史。 |
| Markdown 与流式内容损坏 | 原先把换行转成 br 后再匹配代码围栏，复现为反引号残留；每轮从渲染后的 textContent 拼回原文。现在单独保存原始流式文本，只转义一次，代码内不处理 Markdown。 |
| Electron prompt 不可用及失败时假完成 | 项目输入改为异步 DOM 对话框；引导保存失败保留向导，字段保存反馈等成功返回。 |
| 旧 Qt 运行时 NameError | Ruff 检出 15 处未定义名称，补齐 QEvent、Property、动画/控件/计时器与辅助函数导入。 |
| 构建不可复现 | 新增 npm lock、Python lock、回归测试与 CI；删除仓库中的零散 node_modules、演示配置及重复前端。 |

## 验证范围

最终本地结果：32 项 Python 测试通过、9 项 JavaScript 测试通过，Ruff `F821/F822/F823/E9`、Python compileall、Node 语法检查、Bash 语法检查和 git diff --check 通过。

完整 Electron 窗口在 Linux/Xvfb 下通过 Playwright 冒烟：真实窗口加载、Electron 持有的 Sidecar 启动、preload IPC 与认证、新建项目、原生 create_file 执行、流式代码块、刷新后历史与工具卡片恢复、零页面脚本错误、退出后端口关闭。额外测试验证 POSIX 父进程异常退出时 Sidecar 自动退出。测试脚本 `tests/electron_smoke.py` 已随源码交付，截图生成于 `test-results/electron.png`。

本地环境：Linux x86_64、Python 3.12.14、Node.js 24.19.0、Electron 33.4.11、OpenAI SDK 2.54.0、httpx 0.28.1、Playwright 1.64.0。Electron 延续原仓库的 33 主版本并锁定补丁版本；本次未升级 Electron 主版本。

- Python 回归覆盖 HTTP 分包、大 UTF-8 body、无效 JSON/长度、Origin/认证、并发拒绝、单流接收、过期确认、取消、配置回滚/加密、SDK transport 替换、工具分片、余额、历史清空。
- 本地模拟供应商集成覆盖真实 Sidecar + ChatEngine + OpenAI SDK：对话/SSE、代码围栏、历史落盘/恢复/清空、规范项目名、原生工具执行、模型/余额的自定义端口与 base path。
- JavaScript 覆盖流终止/错误、重复发送、旧流事件、POST 等待时停止、过期对话框回复、Markdown、Electron child 启动/失败/退出。
- 没有可用真实供应商凭据，未验证实际 DeepSeek 服务响应、价格或模型能力。

## 剩余重构与能力边界

这些内容没有被本次测试证明可用，不能把本次交付解读为整个 7 万行仓库已经全覆盖：

1. 文件拖放仍只把文件名附到输入，文件管理按钮仍是路径提示；需要专门的附件上传、文件树和预览 API。Todo 仍为前端内存状态，Pin 尚未接入后端；应补持久化及 UI。
2. context_1m 目前保存为模型元数据，不代表供应商真实支持 1M，也未形成自动 token 预算/压缩策略。自定义供应商是否兼容 thinking、reasoning_effort、tools 需能力协商与真实供应商契约测试。
3. MCP、LSP、子代理、钩子、向量索引、图片/文档及所有内置工具未逐项集成验证。旧 Qt 仅做语法与未定义名称检查，不是 Qt 运行验证。
4. ChatEngine 仍有大型 mixin、通配导入和跨模块共享状态；后续应逐步收敛资源关闭接口、明确状态机，并在独立回归下拆分。此次侧重连接桌面与引擎的可运行链路。
5. 当前仍使用固定配置端口；同时运行多个独立安装需要手动配置不同端口。平台安装包、签名、自动更新以及 Windows/macOS 的原生运行未在本 Linux 环境验证。

若后续提供历史分析记忆，应把其条目逐一映射到基线、复现用例和交付提交，避免把未读取的历史结论当成已修复事实。
