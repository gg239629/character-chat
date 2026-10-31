# 数据库存储与后续登录

当前阶段保留 FastAPI 和 OpenAI 兼容 SDK，使用 SQLAlchemy 2.x 管理 SQLite / PostgreSQL。检索仍采用本地词片匹配；LangGraph 和语义向量检索尚未引入。

## 本地运行

安装 requirements.txt 后启动 server:app。本地默认在 data/chat.db 保存数据库，已加入 Git 和 Docker 忽略列表。

首次启动将 memory/记忆包.json 的场景、历史，以及可用的 memory/long_term 下的旧归档和摘要，导入 owner 的 default 会话。后续重启不覆盖已有数据库；原文件保留作为迁移来源，此后修改原 JSON 不会影响数据库里的当前场景。

聊天仍需点击“保存到记忆”持久保存对话；每轮生成后的场景更新直接保存到数据库。最近窗口最多200条，独立归档保留保存过的原文，摘要在达到原阈值后触发。没有保存过的浏览器消息不保证可从服务端恢复。

## Render 持久化

1. 使用外部 PostgreSQL，例如自行注册 Neon 或 Supabase 的可用免费方案。
2. 在 Render Environment 添加 DATABASE_URL，填写该服务商提供的完整连接串（包含必要的 SSL 参数）。不要把密码或完整连接串提交到 Git。
3. requirements.txt 已包括 psycopg 驱动；render.yaml 已声明 DATABASE_URL，由用户手动填写。不会自动创建付费数据库。
4. Render 环境缺少 DATABASE_URL 时启动会报错，避免误用会随部署丢失的本地存储。

PostgreSQL 连接串可用 postgres://、postgresql:// 或 postgresql+psycopg:// 前缀。首次部署会从仓库快照导入旧记忆；已经产生在旧 Render 实例里的独有记忆，需要在旧实例被替换前另外导出，不能靠本地仓库恢复。

本地 SQLite 不会自动同步到外部 PostgreSQL；首次切换将导入旧 JSON 快照。若需要搬运当前 SQLite 中的新增记忆，应先实现并执行数据库迁移/导出导入，而不是直接切换后丢弃旧库。

## 用户与会话边界

现阶段仍为单个 owner，不包含注册或登录页面。当前部署入口的共享访问密码不等于多用户登录。

用户表 chat_users；会话表 chat_conversations 包含 user_id 外键、版本号与 JSON 数据列，场景/近期历史/归档/摘要/模型设置都按会话归属隔离。版本比较更新避免不同字段并发保存相互覆盖。该结构适合当前规模；大量归档后可继续拆分独立消息表和向量索引。

API：GET/POST /api/conversations；GET /api/memory/export。请求头 X-Conversation-ID 只选择当前用户已拥有的会话，不接受浏览器指定 user_id。界面暂不显示会话管理入口，现有聊天继续使用 default。

后续接登录时，应在 bind_conversation 运行前通过验证的 session/JWT 注入 request.state.user_id，并将默认会话映射到该用户独有 ID。还需要处理注册流程、会话管理界面、浏览器 localStorage 的用户隔离、CSRF/会话退出、密码哈希等。未接完认证前不要将当前共享密码入口作为多人隔离产品开放。

模型设置按会话保存到数据库，接口只返回密钥掩码；数据库备份包含敏感配置，须私密保存。未来可用加密字段或密钥管理服务进一步保护数据库中的 API 密钥。

## 验证

python -m unittest discover -s tests -v

显式真实模型测试（使用当前配置，产生 API 费用，仅操作临时数据库）：
python tests/live_storage_check.py

已实现 SQLite 自动测试；真实外部 PostgreSQL 的连通性与持久性需配置连接后验证。
