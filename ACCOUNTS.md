# 账号登录与邀请注册

登录页为 `/login`。开启账号功能后，未登录者先登录；主页左下角「设置 → 账号」可以修改密码、退出登录。注册使用用户名和密码，不依赖邮件或短信服务。忘记密码由管理员通过命令重置。

## 首次启用（先绑定旧数据，再开放登录）

在项目目录、**连接到目标数据库**的环境执行。默认本地是 `data/chat.db`；云端是 Render 的 `DATABASE_URL`。在本地创建账号不会自动修改 Neon。保持现有 `APP_ENCRYPTION_KEY`，不要重新生成。

```powershell
cd D:\AIWork\Github\character-chat
& "D:\Work\AI_Python\AI\.venv\Scripts\python.exe" manage_accounts.py create-owner admin
```

按提示私密输入密码两次。此命令把 `admin` 绑定到已有 `owner` 身份，保留 default 会话、三人私聊、隐藏私聊、聊天原文、场景、归档、头像、模型配置及后台任务；不复制或重写聊天数据。第一个普通注册用户不会获得这些数据。重复绑定会报错，不会覆盖旧账号。若要更新 Neon，请在配置了对应 `DATABASE_URL` 的终端执行，完成后清除临时连接变量。不要把连接串贴到聊天或提交 Git。

生成一份邀请码：

```powershell
& "D:\Work\AI_Python\AI\.venv\Scripts\python.exe" manage_accounts.py invite-code
```

将生成值保存在环境变量 `REGISTRATION_INVITE_CODE`，不是代码文件。该邀请码是共享邀请口令，可以多次注册，轮换后旧码失效，不是一次性邀请。

本地启用（当前 PowerShell 窗口有效）：

```powershell
$env:ENABLE_USER_ACCOUNTS = "true"
$env:REGISTRATION_MODE = "invite"
$env:REGISTRATION_INVITE_CODE = Read-Host "粘贴刚才生成的邀请码"
& "D:\Work\AI_Python\AI\.venv\Scripts\python.exe" -m uvicorn server:app --host 127.0.0.1 --port 8788 --reload
```

在 Render Environment 设置同样三个变量，然后重新部署。先在 Neon 上完成 `create-owner`，再开启账号开关。`REGISTRATION_MODE` 支持 `invite`（默认）、`closed`（关闭注册）、`open`（公开注册）。邀请模式缺少至少16字符的邀请码时拒绝注册。若有代理导致请求来源识别不同，可设置 `PUBLIC_ORIGIN=https://你的实际域名`，不带末尾 `/`。

`ENABLE_USER_ACCOUNTS` 默认保持关闭，避免更新后管理员突然无法访问。关闭时延续个人空间及部署入口的旧共享密码；开启后 `deploy.py` 自动跳过旧 Basic Auth，不再要求用户重复登录两次。健康检查保留。

## 账号与模型边界

新用户获得独立的群聊、三人私聊及后台角色世界，从第一天08:00开始。角色模板共用；聊天、状态、记忆、亲密度、模型配置及密钥不共用。新用户默认关闭主动消息，且不会继承服务器管理员的 MODEL_API_KEY，需先在主页设置自己的模型。

数据库依据服务端登录身份检查归属，客户端不能通过 user_id 或会话 ID 越权。浏览器聊天缓存、状态栏偏好和未发送草稿按账号分区；启用账号后不会自动导入原来未分区的浏览器聊天缓存。账号切换广播到其他标签页，旧页面请求带原账号指纹时会被拒绝。登录失效时保存尚在输入框中的草稿，重新登录同一账号后恢复，其他账号读不到该草稿。

密码保存为独立随机盐的 PBKDF2-SHA256 哈希，数据库只存会话令牌的 SHA256。Cookie 为 HttpOnly、SameSite=Strict，HTTPS 下加 Secure，7天到期。退出撤销本次登录，修改或管理员重置密码撤销所有登录及该账号所有设备的通知订阅。退出时清理当前设备订阅和本账号浏览器聊天缓存；通知需要用户重新开启。登录注册有共享数据库限流，写请求校验 Origin / Sec-Fetch-Site。生产网站应使用 HTTPS。

## 管理员重置密码

```powershell
& "D:\Work\AI_Python\AI\.venv\Scripts\python.exe" manage_accounts.py reset-password 用户名
```

需要数据库操作权限，密码不会出现在命令参数中。此功能没有公开 HTTP 重置接口，不提供任何默认万能密码。

新增表 `chat_auth_limits` 由 SQLAlchemy 自动建立，原账号表继续使用；无须清库或手工执行 SQL。更新数据库结构不会将本地聊天上传到 Neon。

也可通过管理员操作将邀请登录策略保存到 `chat_account_policy`：须先绑定 owner，才允许启用。邀请码只保存 SHA256 摘要，不保存原文；服务启动时读取策略。显式环境变量优先于数据库策略，因此数据库策略启用时无需另填 Render 环境变量。修改策略后需重新部署或重启生效，不改变聊天数据。

## 验证

`python -m unittest discover -s tests -p test_account_flows.py`

另有浏览器脚本回归覆盖会话初始化、缓存分区、失效草稿、跨标签页切换及退出行为。身份功能不需要调用聊天模型。
