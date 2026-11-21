# 简历项目封装 · Character Chat（多 Agent 角色协作对话平台）

> 用法：本文件是**可直接抄进简历/项目经历页的成稿**。含①精简版 bullet ②详版 STAR ③架构与技术点 ④量化口径 ⑤面试追问应答 ⑥技术栈关键词。
> 所有数字均可在仓库内自查复现（命令见第 4 节），不写"提升 X%"这类无法测量的结论。

---

## 0. 项目抬头（三种写法）

**A. 一行版（简历表格窄栏）**
> AI 角色协作对话平台 · 独立开发 · FastAPI + LangGraph + LlamaIndex + Vue3 · 多 Agent 调度 / 分层记忆 RAG / 前缀缓存与 Token 预算 / 工具调用式多模态（文生图·语音）/ MCP 对外赋能

**B. 两行版（项目经历标题）**
> **Character Chat｜多 Agent AI 角色协作对话平台**（个人项目 / 全栈独立开发）
> 技术栈：Python 3.13 · FastAPI · LangGraph 1.x（LangChain Core 消息层）· LlamaIndex · BM25 · SQLAlchemy/Alembic · MCP 1.x · Vue 3 + TypeScript + Vite · SQLite / PostgreSQL

**C. 定位句（面试开场 20 秒）**
> 一个"三个角色 + 一个用户"的长期陪伴式聊天系统：不是把三个人设塞进一个 prompt，而是让每个角色作为独立 Agent 拥有自己的模型配置、私有记忆与发言决策权，由一个只读公开上下文的调度 Agent 逐条决定"下一句谁来接、要不要接"，全程跑在带持久化 checkpoint 的 LangGraph 图上，对外再通过 MCP Server 暴露成可被任意客户端调用的角色能力。

---

## 1. 精简版 bullet（5 条，最推荐）

- **多 Agent 调度而非单 prompt 拼装**：拆分为 1 个公开调度 Agent + N 个角色 Agent（各自模型/密钥/人设/记忆），实现"调度提候选顺序 → 角色自行判断发言或沉默 → 真实消息落库 → 后续角色读到这条真实发言再重判"的逐条重入循环；单请求以 8 条消息 / 12 次生成作为空转保护，允许 0~3 人参与、同一角色可二次接话。
- **权限先于相关性的 RAG**：近期原文投影 + 历史摘要归档（40~60 条/批 → 约 200 字）+ BM25Plus 词法检索，可在不动数据的前提下切换 LlamaIndex 向量检索并与词法结果融合；**候选集先按 用户/故事/通道/角色 四级权限过滤，再做相关性排序与预算分配**，从架构上杜绝"向量召回绕过私聊隔离"。
- **上下文与成本工程（KV/前缀缓存友好）**：把固定写作规则 + 核心档案 + 角色指令固定放在 prompt 前缀，按角色与通道复用，使支持自动前缀缓存的供应商接口命中 KV Cache；动态话题档案与回忆证据后置；全链路按字符/Token 双口径计量，并按模型维度统计 `prompt_cache_hit_tokens` → 输出 `hit_rate`，把"缓存是否真的生效"变成可观测指标而非口号。
- **LangGraph 持久化执行图**：外层图只保存 run_id/阶段这类标识（不落提示词、聊天原文、密钥），checkpoint 复用官方 SQLite/PostgreSQL Saver，并按数据库维度复用图实例与连接池；图内节点只做"校验 → 角色交互"，幂等、租约、事务投递仍由业务库负责，做成"图负责编排、库负责一致性"的分层。
- **MCP 对外赋能 + 多租户安全**：以 Streamable HTTP + Bearer 凭据把角色能力封装为标准 MCP Server（5 工具 + 1 资源），凭据级独立故事世界、人物/读写/有效期/每日请求数与 **Token 预算**（预留-结算-回收）双重限额；DB 只存 token 摘要，站点 Cookie 不能替代 MCP 凭据。
- **工具调用式多模态输出（文生图 + 语音）**：不做前端关键词路由，而是给流式对话挂 `generate_image` / `send_voice` 两个 Function Tool，由角色模型结合上下文自行决定"该不该发照片、该不该说话"；生成走**持久化后台任务**（图片/音频二进制入库、租约 + 请求 ID 幂等、可刷新恢复、失败可重试），与消息在同一事务提交，避免临时外链失效与云端磁盘重启丢失；生图按 20MB / PNG·JPEG·WebP 校验，语音绑定原始角色消息并支持情绪与停顿风格参数。

## 2. 详版（六段式，适合一页项目经历）

**① 背景与目标**
长期陪伴型对话的两个真实痛点：一是多人设共用一份上下文会互相串戏、私聊秘密被"相关性"召回泄露；二是长聊必然撞上上下文膨胀与调用成本。目标是把"像三个真人"和"成本可控、可审计"同时做到。

**② 角色与技术选型**
独立负责架构设计、后端全部业务模块、前端 Vue3 页面与设置体系、迁移与部署。后端 80 个 Python 模块 ≈8.0k 行，52 个 HTTP 路由，33 张表、6 个 Alembic 版本迁移，307 个单测方法 + Vue 组件测试 + Playwright E2E。

**③ 关键动作（承接第 1 节 5 条）**

**④ 工程可靠性**
- 用户发送 = 原文 + 近期记忆投影 + 待回复任务 **同事务提交**，失败三者同时回滚；角色动作的消息写入与已发送状态同事务，隐藏私聊在该事务内创建答复任务。
- 固定消息 ID + 幂等校验保证重试不重复投递；回复任务带执行令牌与 30 秒续租，旧工作进程不能覆盖新结果；会话忙碌只暂缓不计失败。
- 内存缓冲批量落库（每分钟一次合并事务）+ 正常关闭补存，让"上下文/未读/预览"零延迟可读。
- Alembic 启动自动升级，PostgreSQL 迁移用事务级锁避免多进程并发；SQLite 关停前收拢 WAL。
- 故障只记阶段/异常类型/耗时，不存正文与密钥；`/api/usage` 提供 1~30 天按调用类别的次数、失败、耗时、字符/Token、缓存命中。

**⑤ 多模块对外输出与多模态**
- **多模态**：文字 → 图片 → 语音三条输出通道共用一套角色服务与权限检查。生图不是 UI 上的按钮逻辑，而是模型可自主调用的 `generate_image(prompt, actor, size)` 工具（"发张自拍""重拍一张""让我看看你"这类修正型指令都必须真的调用工具，不允许只在台词里假装拍了）；语音为 `send_voice(text, style)` 工具，只放真正说出口的台词，括号动作不朗读，情绪/语速/停顿走 style 而基础音色保持人物身份。
- **异步与持久化**：两类生成都是 durable job（`chat_image_jobs` / `chat_voice_jobs`），二进制入 DB、租约防并发重复、请求 ID 幂等（网络重发不二次计费）、进程重启后待执行任务由维护工作器接管、失败可单独重试；用户不必等图片/语音就能继续发文字。
- **对外**：MCP Server（角色卡片 / 单角色对话 / 群聊对话 / 任务查询 / 消息读取 + `characters://cards` 资源）→ 任意支持 Streamable HTTP 的客户端可把三个角色接入自有工作流；另有 Web Push 主动消息（APScheduler 调度 + VAPID）、SSE 增量事件流。

**⑥ 结果**
形成一套"多人设长期记忆对话"的可复用骨架：新增角色 = 新增一份配置 + 记忆分区，无需改调度与检索路径；对外能力通过 MCP 复用同一套角色服务与权限检查，不存在第二套角色提示词。

## 3. 架构速览（面试画图用）

```
Vue3 / 外部 MCP 客户端
        │  (SSE 增量事件 / Streamable HTTP + Bearer)
        ▼
FastAPI  API 层（52 路由）── 统一 CharacterService
        │
        ├─ 权限与通道：channel_policy（群聊 / 用户私聊 / 隐藏角色私聊）
        ├─ 消息与任务：message_service + chat_inbox（事务 + 幂等 + 租约）
        ├─ 编排：LangGraph 持久化图（StateGraph + checkpointer）
        │     ├─ validate        任务状态校验（只读 id/status）
        │     └─ character_exchange → 群聊子图逐条提名 / 单人直接回复
        │            └─ 角色 Agent：自己的模型配置 + 自己可见的记忆
        ├─ 记忆：role_memory（权限过滤→召回→预算）· long_memory(BM25Plus)
        │        · retrieval(LlamaIndex + 向量指纹缓存) · memory_autosave
        ├─ 世界状态：world_time / scene_labels / event_memory / continuity
        ├─ 主动与自主：autonomy / initiative_control（租约 + 到期判断）
        └─ 基础设施：storage · schema_migrations(Alembic) · model_usage · task_health
```

## 3.5 多模态输出链路（面试重点讲这段）

```
用户自然语言（"发张自拍" / "用语音跟我说晚安"）
        │
        ▼
同一角色文字模型（流式 + tools=[generate_image, send_voice])
        │  流式增量里累积 tool_calls（id/name/arguments 分片重组，参数上限 12000 字符）
        ├─ 未调用工具 → 正常文字回复，且禁止出现 [[image:...]] 之类的伪标记
        └─ 调用工具 → 校验参数 → 建 durable job（pending）→ 立刻返回"排队中"
                              │
                     ThreadPoolExecutor（每通道 2 worker + 租约）
                              ▼
              生图：OpenAI 兼容 /v1/images/generations（沿用或独立配置接口/密钥）
              语音：TTS（per-actor 音色 + style 情绪/停顿）
                              ▼
              二进制写库（LargeBinary）：succeeded / failed，同一事务写回消息
                              │
                              ▼
       前端轮询任务状态 + SSE 通知；模型收到真实工具结果后自然续答
```

设计判断（这是加分点，不是实现细节）：

1. **不用前端关键词路由 / 独立意图分类请求**——"你好像会画画？""别发照片"这类句子用规则必然误判，交给模型在真实上下文里判断；代价是需要流式工具调用支持，故在能力探测失败时降级为纯文字。
2. **不把历史图片回灌视觉模型**——当前是文生图，参考信息来自角色视觉资料 + 本人状态 + 本窗口最近消息，天然避免了"看图说话"幻觉与多模态上下文膨胀。
3. **二进制入库而非对象存储外链**——SQLite/PostgreSQL 双栈一致、云盘重启不丢、随故事重置一起备份/删除；代价是单张限 20MB 并做 deferred 列（列表查询不加载字节）。
4. **幂等 = 省钱**：工具调用按原用户请求标识去重，一条消息最多一个任务；SDK 自动重试显式关闭，重启中断标记 failed 而不自动二次付费；只有用户主动"重新生成"才是新计费请求。
5. **失败不影响已保存的用户消息**，生成状态与图片消息也不展示内部提示词——对外只暴露"角色在拍 / 拍好了"。

## 4. 量化口径（可复现，避免面试被追问翻车）

| 指标 | 数值 | 自查命令 |
| --- | --- | --- |
| 后端模块 / 行数 | 80 个模块 / ≈8.0k 行（不含测试） | `Get-ChildItem backend -Recurse -Filter *.py \| Get-Content \| Measure-Object -Line` |
| HTTP 路由 | 52 | `Select-String backend/app/api/*.py -Pattern "@router\.(get\|post\|put\|delete\|patch)"` |
| 数据表 / 迁移 | 33 张 / 6 个版本 | `Select-String backend/**/*.py -Pattern __tablename__`；`ls migrations/versions` |
| 测试 | 307 个 unittest 方法 + Playwright E2E | `Select-String tests/test_*.py -Pattern "def test_"` |
| 前端 | 13 个 Vue SFC（Vue3 + TS + Vite + vue-router） | `ls frontend/src` |
| MCP | 5 工具 + 1 资源 | `backend/app/mcp/server.py` |
| 多模态 | 2 个 Function Tool（`generate_image` / `send_voice`）+ 2 张 durable job 表；单图 ≤20MB，PNG/JPEG/WebP | `backend/app/characters/image_tools.py`、`voice_generation.py` |
| 角色规模 | 3 个角色 Agent，单请求 0~3 人参与 ≤8 条消息 / ≤12 次生成 | `docs/design/MEMORY_AGENTS_PLAN.md` |
| 上下文预算 | 默认输入 24576 token，按完整轮次批量压缩，冻结摘要与追加式历史同步维护 | `docs/design/EFFICIENCY.md` |
| 调度输入 | 最近 ≤6 条公开消息（≤4200 字符）+ ≤600 字符话题起点 | 同上 |
| 归档粒度 | 40~60 条原文 → 约 200 字摘要（带来源/参与者/时间窗/知情范围） | `docs/design/MEMORY_AGENTS_PLAN.md` |
| 实时性 | SSE 增量事件，后台消息 5 秒内到达；30 秒兜底轮询 | `docs/design/REALTIME.md` |
| 用量留存 | 30 天，1 分钟批量落库 | `docs/design/EFFICIENCY.md` |

> 口径提醒：不要在简历里写"节省 40% Token / 命中率 90%"之类数字——仓库文档明确只报可观测值（hit_rate 由供应商返回的缓存命中 Token ÷ 输入 Token 计算，供应商不上报时为 null）。面试时把"我把它做成**可测量**"讲成卖点，比编造百分比更硬。

## 5. 面试高频追问（12 问，含应答要点）

1. **为什么不用一个 Agent 一次生成三个人的回复？**
   三个人各说各话会互不可见，第二个人无法回应第一个人刚说的事实；且三人共用一份 prompt 时私聊语义必然串。改为逐条重入：每条真实发言落库后，后续角色读到这条真实消息再判断，才能出现"小研先提议→小柳改口→小婉认为不必说则沉默"。
2. **调度者会不会泄密？**
   调度节点只读公开群聊、公开场景、各角色自填的 `public_persona`，代码层面不加载私人 persona / relationship / role_states / 私聊记录；输出严格校验为 0~3 个去重角色 ID，超时或非法输出回退"各人自行判断"，空名单是合法沉默，不兜底造发言。
3. **RAG 怎么防越权？**
   顺序是死的：先按 user_id/story_id/channel/actor_id 过滤候选集，再算相关性与预算。绝不"全库排序后过滤"，因为排序阶段就可能把别人的私聊算成高相关；向量索引也只在获准候选集合上构建。
4. **KV Cache / 前缀缓存具体怎么做？**
   稳定内容（固定写作规则、核心档案、角色指令）固定在 prompt 头部，按角色 + 通道维度复用，动态片段（话题档案、回忆证据、当前轮）全部后置，保证前缀不被随机内容打断；同时以 `prompt_cache_hit_tokens` 统计命中率，并把"未上报"与"命中 0"区分开。
5. **LangGraph 存了什么、不存什么？**
   图状态只存 run_id、status、stage 与 thread 哈希；提示词、原文、密钥一律不进图。checkpoint 用官方 SqliteSaver / PostgresSaver 并按 engine 复用实例，图只做"校验→执行"，投递一致性与幂等由业务库事务 + 租约保证——避免把业务真相分散到两套存储。
6. **失败重试会不会重复发言？**
   固定消息 ID + 请求 ID 幂等；回复任务有执行令牌 + 续租，旧进程写入被判为过期丢弃；会话忙碌走暂缓不计失败，只有真故障才指数退避。
7. **长上下文怎么控？**
   请求以角色规则和冻结摘要开头，同频道原文按时间追加。达到 token 警戒线后压缩最旧的完整轮次。当前事实、跨窗口证据和当前输入后置，最新明确更正优先于旧摘要。
8. **成本怎么控？**
   能规则判的绝不调模型（时间推进、动作证据、插话分类、去重收尾都用规则）；只有角色真的获得发言机会才构建完整记忆上下文；辅助调用统一走当前故事的群聊模型配置；MCP 侧再加每日请求数 + Token 预算（预留-结算-回收）。
9. **并发与多进程怎么办？**
   当前是单 worker 部署：进程内事件总线 + 每分钟批量落库 + 30 秒兜底查询。多进程扩展时事件总线需换共享实现，兜底查询保证不丢；这一点在架构文档里写明边界，不假装已经支持水平扩容。
10. **MCP 的安全边界？**
    每凭据独立故事世界、独立授权人物/读写/有效期/额度；DB 只存 token 摘要，请求级重校验；模型费用记在凭据持有人的配置上；预算按 UTC 日计数，中断遗留预留会被后续请求回收为估算消耗，不会静默清零。
11. **测试怎么做的？**
    307 个单测覆盖权限隔离、动态预算、事务回滚、幂等、迁移升级、缓存指纹、事实替代等；另有 `tests/live_*.py` 真实模型验收，全部在临时数据库 + 中性人设跑，不碰真实聊天；前端 Playwright 验证后台消息 5 秒内到达。
12. **这个项目最难/最有价值的判断是什么？**
    "让 Agent 有权沉默。" 强制三人逐个回应是最省事的实现，但会立刻暴露成复读与抢话；把发言权交回角色、把提名权交给只读公开上下文的调度者，才换来自然的多人对话——代价是必须把逐条重入、去重、幂等、预算全部做扎实。

## 6. 技术栈关键词（ATS / 简历标签）

Python · FastAPI · Pydantic · SQLAlchemy 2.0 · Alembic · PostgreSQL · SQLite · LangGraph · LangChain Core · Checkpointer · StateGraph · LlamaIndex · BM25Plus · RAG · Hybrid Retrieval · Embedding Cache · Prompt Prefix Cache / KV Cache · Token Budget · SSE · APScheduler · Web Push (VAPID) · MCP (Model Context Protocol) · Multi-Agent Orchestration · 多租户隔离 · RBAC · PBKDF2-SHA256 · Docker · Render · Vue 3 · TypeScript · Vite · Vitest · Playwright · unittest

## 7. 一句话收尾（自我介绍里可用）

> 我最想强调的是这套系统的**边界感**：角色之间、通道之间、图状态与业务真相之间、外部凭据与站内账号之间，每一处都做了显式隔离，并且把这些隔离写成了可回归的测试——多 Agent 系统真正难的不是让它说话，而是让它在该闭嘴的地方闭嘴、在该隔离的地方隔离。
