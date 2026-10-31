# Render 免费部署

将本目录上传到 GitHub 私有仓库，然后在 Render 使用 New → Blueprint 连接此仓库。render.yaml 已固定 plan: free。

创建时填写 MODEL_API_KEY、MODEL_BASE_URL、MODEL_MODEL。APP_PASSWORD 自动随机生成，可在 Render Environment 中查看，登录用户名默认 owner。

部署完成后访问 Render 分配的 HTTPS 地址。/healthz 为健康检查，其余页面和 API 均需密码。

注意：免费实例闲置可能休眠，恢复访问有冷启动。免费实例本地磁盘非持久，重启或重新部署可能丢失在线保存的聊天和设置。模型 API 调用费用另算。

部署包包含原有角色资料及聊天记忆，仅上传私有仓库。配置文件中的 API 密钥和本机 env_file 路径已移除，密钥通过 Render 环境变量配置。


## 数据库存储更新

本地 SQLite 与外部 PostgreSQL 配置、旧记忆导入规则、后续登录接口和测试方法见 [DATABASE.md](DATABASE.md)。Render 更新前必须配置 DATABASE_URL；未配置时拒绝使用临时磁盘保存记忆。
