# Telegram 多账号单活跃设计文档

**目标**

在当前 Bot 菜单中增加 Telegram 账号管理能力，支持：

- 保存多个用户账号
- 通过手机号验证码或扫码登录当前激活账号
- 在多个已保存账号之间切换
- 切换后热更新下载监听运行时
- 当前激活账号掉线后由 Bot 主动通知管理员

**约束**

- 同一时间只允许 1 个工作账号在线参与监听/克隆
- 复用现有 `DownloaderRuntime` 单客户端模型，不做多账号并行监听
- 保留现有 `sessions/user_session.session`，升级后自动迁移为第一个账号

**总体方案**

1. 在 `database.py` 新增 `tg_accounts` 表，保存账号元数据与当前激活账号标记。
2. 每个账号使用独立的 Telethon session 文件：
   - 迁移账号：`sessions/user_session.session`
   - 新建账号：`sessions/user_account_<id>.session`
3. `manager_bot.py` 启动时按“当前激活账号”构造 `user_client`。
4. 账号切换时停止旧 `DownloaderRuntime`，基于目标账号 session 重建 `user_client` 和运行时，再刷新来源缓存。
5. Bot 菜单新增“账号管理”页，统一提供：
   - 账号列表
   - 新建账号
   - 切换账号
   - 手机号验证码登录
   - 扫码登录
   - 删除未激活账号
6. 手机号登录和扫码登录都仅作用于“当前激活账号”，从而避免多客户端并发登录状态机冲突。

**为什么不做并行多账号**

- 当前下载核心 `DownloaderRuntime` 明确绑定单个 `TelegramClient`
- 来源表 `download_sources` 不含账号绑定字段
- 多账号并行需要重构运行时调度、来源归属、状态展示和掉线策略，改动面过大

本次采用“多账号保存 + 单活跃账号”可以在最小改动下满足需求。

**数据设计**

新增表 `tg_accounts`：

- `id`
- `label`
- `phone`
- `username`
- `user_id`
- `session_name`
- `is_active`
- `status`
- `status_message`
- `last_login_at`
- `last_logout_at`
- `created_at`
- `updated_at`

状态建议：

- `new`：新建占位账号，尚未登录
- `ready`：当前 session 可用
- `offline`：当前激活账号掉线或未授权
- `error`：最近一次登录失败

**启动迁移**

启动管理 Bot 时执行：

1. 若 `tg_accounts` 为空：
   - 如果存在 `sessions/user_session.session`，创建迁移账号并标记激活
   - 如果不存在，也创建一个默认激活占位账号，session 名仍为 `user_session`
2. 若已有账号但没有激活账号，则选择最新一条作为激活账号
3. 若激活账号 session 已授权，回填 `phone / username / user_id / label`

**运行时切换**

切换账号时流程如下：

1. 停止当前 `DownloaderRuntime`
2. 断开当前 `user_client`（不主动 `log_out`，避免删除旧账号 session）
3. 基于目标账号 `session_name` 创建新的 `TelegramClient`
4. 连接后检测 `is_user_authorized()`
5. 若已授权：
   - 标记账号 `ready`
   - 设置为激活账号
   - 重建 `DownloaderRuntime`
   - 刷新来源缓存
6. 若未授权：
   - 标记账号 `offline`
   - 设置为激活账号
   - 运行时保持停止
   - 提示管理员继续扫码或手机号验证码登录

**登录流程**

手机号验证码登录：

1. 管理员在账号管理页点击“手机号登录”
2. Bot 提示输入手机号
3. `user_client.send_code_request(phone)`
4. Bot 提示输入验证码
5. 若遇到 `SessionPasswordNeededError`，再提示输入 2FA 密码
6. 成功后更新账号资料与状态，并启动/恢复运行时

扫码登录：

1. 管理员在账号管理页点击“扫码登录”
2. Bot 对当前激活账号的 `user_client.qr_login()` 生成二维码
3. 若扫码后需要 2FA，则沿用文本输入补密码
4. 成功后更新账号资料与状态，并启动/恢复运行时

**菜单调整**

首页将“账号登录”升级为“账号管理”。

账号管理页展示：

- 当前激活账号
- 当前登录状态
- 账号列表
- 可用操作按钮

兼容性处理：

- 保留“账号登录”文字入口，跳转到账户管理
- 原 `/login` 命令保留，但行为改为“对当前激活账号发起扫码登录”

**掉线通知**

`_session_watchdog()` 继续保留，但通知文案改为包含当前账号信息：

- 掉线：通知“当前账号已掉线/未登录”
- 恢复：通知“当前账号已恢复登录”

同时把状态同步写回 `tg_accounts.status/status_message`。

**删除策略**

- 只允许删除“未激活账号”
- 使用二次确认按钮
- 删除数据库记录后尝试删除对应 `.session` 文件

**风险点**

1. Telethon session 文件在切换与登录过程中的连接释放必须干净，否则可能出现 SQLite 锁或 session 占用问题。
2. 当前 `manager_bot.py` 使用大量闭包变量，改造时需要严格控制状态同步，避免 `user_client/user_ready/runtime` 三者不一致。
3. 手机号验证码登录是新增路径，需要补最基本的状态机测试或至少做语法/流程验证。

**验收标准**

- Bot 菜单可查看账号列表
- 可新建账号并切换为当前激活账号
- 当前激活账号支持扫码登录
- 当前激活账号支持手机号验证码 + 2FA 登录
- 已保存账号之间可切换，切换后运行时无需重启进程
- 账号掉线能触发 Bot 通知
- 迁移后的 `sessions/user_session.session` 可继续工作
