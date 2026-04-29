# Telegram 单活跃多账号 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在现有 Telegram 管理 Bot 中增加单活跃多账号能力，支持手机号验证码登录、扫码登录、账号切换热更新和掉线通知。

**Architecture:** 账号元数据存入 SQLite `tg_accounts` 表；每个账号使用独立 session 文件；`manager_bot.py` 始终只持有一个当前激活账号的 `TelegramClient`，切换账号时重建 `user_client` 与 `DownloaderRuntime`，不改动下载核心的单客户端模型。

**Tech Stack:** Python 3、Telethon、SQLite、unittest、python-dotenv

---

### Task 1: 账号数据库模型

**Files:**
- Modify: `database.py`
- Test: `tests/test_database_accounts.py`

**Step 1: 写数据库层测试**

- 为 `tg_accounts` 的创建、激活互斥、字段更新、删除编写 `unittest`

**Step 2: 运行测试确认当前失败**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`

Expected:
- 因 `tg_accounts` 相关 API 不存在而失败

**Step 3: 实现最小数据库能力**

- 新增 `tg_accounts` 表
- 增加以下方法：
  - `create_tg_account`
  - `get_tg_account`
  - `list_tg_accounts`
  - `update_tg_account`
  - `set_active_tg_account`
  - `get_active_tg_account`
  - `delete_tg_account`

**Step 4: 运行测试确认通过**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`

Expected:
- 新增数据库测试通过

### Task 2: 启动迁移与激活账号加载

**Files:**
- Modify: `manager_bot.py`

**Step 1: 增加账号启动辅助函数**

- 实现迁移和加载逻辑：
  - `_bootstrap_accounts()`
  - `_build_session_path()`
  - `_build_user_client_for_account()`
  - `_sync_account_profile_from_client()`

**Step 2: 将启动时硬编码 `sessions/user_session` 替换为“激活账号 session”**

- 管理 Bot 启动时按激活账号创建 `user_client`
- 若无账号则创建默认占位账号

**Step 3: 验证语法**

Run: `python -m py_compile manager_bot.py database.py`

Expected:
- 无语法错误

### Task 3: 账号切换与运行时热更新

**Files:**
- Modify: `manager_bot.py`

**Step 1: 实现切换账号辅助函数**

- 新增：
  - `_switch_active_account(account_id, ...)`
  - `_replace_user_client_for_account(account, ...)`

**Step 2: 调整旧的重登录逻辑**

- 废弃“切换账号时对旧 session 执行 `log_out()`”的行为
- 切换只断开，不注销

**Step 3: 接入运行时重建**

- 切换后根据授权状态决定：
  - 启动 `DownloaderRuntime`
  - 或保持停止并提示登录

**Step 4: 验证语法**

Run: `python -m py_compile manager_bot.py`

Expected:
- 无语法错误

### Task 4: 账号管理菜单

**Files:**
- Modify: `manager_bot.py`

**Step 1: 新增账号管理视图函数**

- 增加：
  - `_build_account_menu_text()`
  - `_build_account_menu_buttons()`
  - `_show_account_menu()`

**Step 2: 首页入口改为账号管理**

- 回复键盘从“账号登录”升级为“账号管理”
- 保留“账号登录”文本兼容入口

**Step 3: 增加账号操作回调**

- 支持：
  - 新建账号
  - 切换账号
  - 删除账号（带确认）
  - 刷新列表

**Step 4: 验证语法**

Run: `python -m py_compile manager_bot.py`

Expected:
- 无语法错误

### Task 5: 手机号验证码登录状态机

**Files:**
- Modify: `manager_bot.py`

**Step 1: 扩展 `PendingTextInput` 使用场景**

- 增加：
  - 输入手机号
  - 输入验证码
  - 输入 2FA 密码

**Step 2: 新增手机号登录流程**

- 当前激活账号点击“手机号登录”后：
  - 发送验证码
  - 接收验证码
  - 必要时接收 2FA
  - 成功后同步账号资料并恢复运行时

**Step 3: 失败路径补齐中文提示**

- 验证码错误
- 2FA 错误
- 当前账号已登录时的误操作提示

**Step 4: 验证语法**

Run: `python -m py_compile manager_bot.py`

Expected:
- 无语法错误

### Task 6: 扫码登录改造成“作用于当前激活账号”

**Files:**
- Modify: `manager_bot.py`

**Step 1: 复用现有二维码流程**

- 保留：
  - 二维码生成
  - 刷新
  - 取消
  - 2FA 补输

**Step 2: 改造扫码上下文**

- 让扫码登录作用于当前激活账号，而不是硬编码 `user_session`

**Step 3: 登录成功后同步账号资料**

- 回填：
  - `phone`
  - `username`
  - `user_id`
  - `last_login_at`
  - `status`

**Step 4: 验证语法**

Run: `python -m py_compile manager_bot.py`

Expected:
- 无语法错误

### Task 7: 掉线通知与状态展示

**Files:**
- Modify: `manager_bot.py`

**Step 1: 扩展状态文本**

- `状态` 页面增加：
  - 当前激活账号名称
  - 账号状态
  - 已保存账号数量

**Step 2: 掉线与恢复通知写回数据库**

- 在 `_set_user_ready()` / `_session_watchdog()` 中同步账号状态

**Step 3: 优化提示文案**

- 掉线通知包含当前账号标识
- 恢复通知包含当前账号标识

**Step 4: 验证语法**

Run: `python -m py_compile manager_bot.py`

Expected:
- 无语法错误

### Task 8: 文档与最终验证

**Files:**
- Modify: `README.md`
- Modify: `.env.example`（如有新增配置再改）

**Step 1: 更新 README**

- 说明 Bot 里支持：
  - 账号管理
  - 扫码登录
  - 手机号验证码登录
  - 多账号切换

**Step 2: 运行最终验证**

Run:
- `python -m unittest discover -s tests -p "test_*.py" -v`
- `python -m py_compile manager_bot.py bot.py database.py manage_sources.py setup_user_session.py`

Expected:
- 测试通过
- 关键文件无语法错误

**Step 3: 人工冒烟检查**

- 打开 Bot 菜单
- 验证账号管理入口
- 新建账号并切换
- 手机号登录链路
- 扫码登录链路
- 掉线提示链路

