# Character Chat

网页与外部 MCP 客户端共用人物服务，消息接收与回复任务分离。后端按账号、人物、聊天、世界状态、记忆和运行时分层；前端使用 Vue 3、TypeScript 和 Vite。

结构、LangGraph 调度、可选 LlamaIndex 检索及数据库迁移见 [ARCHITECTURE.md](ARCHITECTURE.md)；外部人物调用见 [MCP.md](MCP.md)。

人物状态、空间关系、持久事件历史和发言上下文的更新规则见 [WORLD_STATE.md](WORLD_STATE.md)。

聊天图片生成、独立图片模型配置和持久保存见 [IMAGES.md](IMAGES.md)。

## 本地启动

现有本地账号配置仍有效。在项目目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_local.ps1
```

启动脚本使用本地 SQLite，首次或前端源代码变化后自动构建网页，然后启动 http://127.0.0.1:8788 。前端构建需要 Node.js 22 和 npm。自定义环境可单独执行 `npm ci`、`npm run build`（在 frontend 目录），再用 `python -m uvicorn backend.app.main:app` 启动。旧的 `server:app` 入口保留兼容。

本地或局域网 IP（如 `http://192.168.95.1:56828/v1`）及 localhost 模型接口自动直连，避免 `HTTP_PROXY`、`HTTPS_PROXY` 等环境代理拦截请求而返回 502。公网模型接口继续沿用环境代理设置；该规则同时用于连接测试、角色聊天、场景维护、历史摘要和向量检索，不需要关闭系统代理。

## Render 免费部署

将本目录上传到 GitHub 私有仓库，然后在 Render 使用 New → Blueprint 连接此仓库。render.yaml 已固定 plan: free。

创建时填写 MODEL_API_KEY、MODEL_BASE_URL、MODEL_MODEL。APP_PASSWORD 自动随机生成，可在 Render Environment 中查看，登录用户名默认 owner。

部署完成后访问 Render 分配的 HTTPS 地址。/healthz 为健康检查。个人模式使用共享密码；启用账号模式后改为网站登录，不再使用浏览器 Basic Auth。登录注册、邀请和旧数据绑定见 [ACCOUNTS.md](ACCOUNTS.md)。

注意：免费实例闲置可能休眠，恢复访问有冷启动。免费实例本地磁盘非持久，重启或重新部署可能丢失在线保存的聊天和设置。模型 API 调用费用另算。

部署包包含原有角色资料及聊天记忆，仅上传私有仓库。配置文件中的 API 密钥和本机 env_file 路径已移除，密钥通过 Render 环境变量配置。


## 数据库存储更新

本地 SQLite 与外部 PostgreSQL 配置、旧记忆导入规则、账号隔离和测试方法见 [DATABASE.md](DATABASE.md)。Render 更新前必须配置 DATABASE_URL；未配置时拒绝使用临时磁盘保存记忆。

角色代理、消息自动入库、群聊顺序发言和 API Key 加密说明见 [CHAT_CHANNELS.md](CHAT_CHANNELS.md)。更新需安装 requirements.txt；Render 还需配置稳定的 APP_ENCRYPTION_KEY 用于加密数据库中的模型密钥。

跨会话传话、角色自主联系、角色间有限回应、后台调度及 Web Push 部署见 [ACTIVE_MESSAGES.md](ACTIVE_MESSAGES.md)。主动消息可在主页左下角“设置”中统一开关；明确要求角色在群聊发消息时，她可以接受、拒绝或追问。

模型用量统计、上下文去重、按需检索和群聊统一辅助模型路由见 [EFFICIENCY.md](EFFICIENCY.md)。


当前提示词由 prompt/system.md、角色档案、backend/app/characters/actor_instructions.py 和受权限过滤的动态上下文组成。本次结构迁移保留已有角色资料。

本地启动脚本使用项目内 `.venv`，首次启动或锁文件变化时通过 uv 安装锁定依赖，避免共享环境升级 MCP 等包影响本项目。无需手动激活环境。
