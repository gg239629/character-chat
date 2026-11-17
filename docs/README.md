# Character Chat 文档

首次使用见[项目首页与启动步骤](../README.md#本地启动)。

## 使用与部署

| 主题 | 文档 |
| --- | --- |
| 账号 | [登录与邀请注册](guides/ACCOUNTS.md) |
| 聊天 | [角色、群聊与私聊](guides/CHAT_CHANNELS.md) · [主动消息与通知](guides/ACTIVE_MESSAGES.md) · [主页设置](guides/HOME_SETTINGS.md) |
| 扩展 | [聊天生图](guides/IMAGES.md) · [MCP 接入](guides/MCP.md) |
| 部署 | [数据库与持久化](guides/DATABASE.md) |

## 架构与设计

| 主题 | 文档 |
| --- | --- |
| 项目架构 | [目录、运行方式与测试](design/ARCHITECTURE.md) · [核心流程](design/CORE_OPTIMIZATION.md) |
| 聊天与记忆 | [异步聊天](design/ASYNC_CHAT.md) · [角色记忆与群聊调度](design/MEMORY_AGENTS_PLAN.md) · [角色私下联系规划](design/PEER_CHAT_PLAN.md) |
| 状态与效率 | [现实时间与行动](design/REALTIME.md) · [世界状态与事件](design/WORLD_STATE.md) · [模型用量](design/EFFICIENCY.md) |

## 维护命令

在仓库根目录使用已安装项目依赖的 Python 执行。

| 命令 | 用途 |
| --- | --- |
| `python -m scripts.manage_accounts --help` | 查看管理员绑定、密码重置和邀请码生成命令。 |
| `python -m scripts.configure_background` | 生成后台推送部署配置。 |
| `python -m scripts.bind_database` | 配置手动启动时使用的 PostgreSQL 连接。 |
| `python -m scripts.migrate_to_postgres` | 将本地 SQLite 数据复制到指定的 PostgreSQL 数据库。 |
