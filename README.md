# Telegram 媒体组下载器（频道/Forum 话题）

本项目用于：
- 以 **用户账号（Telethon）** 读取频道/超级群 Forum 话题中的消息
- **仅下载媒体**（包含相册/媒体组 `grouped_id`）到本地目录
- 按 **媒体组** 记录下载完成与进度（最后消息 ID）
- 可选：下载完成后使用 `rclone copy` 上传到远端（如 PikPak），**上传成功后自动删除本地组目录**

> 本版本已移除：转发/推送到目标群、任何中转频道逻辑、标题润色/AI 相关能力。

## 依赖

- Python 3.8+
- `pip install -r requirements.txt`
- （可选）安装 `rclone` 并配置好 remote

## 配置

1) 复制 `.env.example` 为 `.env` 并填写 `API_ID` / `API_HASH` / `USER_PHONE` / `BOT_TOKEN` / `ADMIN_USER_IDS`。

常用参数说明（都在 `.env.example` 里）：  
- `MEDIA_GROUP_FLUSH_SECONDS`：相册（`grouped_id`）聚合等待时间  
- `MAX_DOWNLOAD_CONCURRENCY`：同时下载的媒体消息数量（消息级并发；相册/单条媒体都按“消息数”计）
- `MAX_GROUP_CONCURRENCY`：同时处理的媒体组数量（组级并发；控制同时创建多少组目录/同时跑多少组任务）

2) 首次登录生成会话：

```bash
python setup_user_session.py
```

会生成 `sessions/user_session.session`。

## 添加来源

你可以使用两种方式管理来源：Bot 交互（推荐）或命令行工具。

### 方式 A：管理 Bot（推荐）

1) 启动管理 Bot：

```bash
python manager_bot.py
```

如启动时出现“session 已有授权用户，bot_token 未生效”的提示，删除 `sessions/manager_bot.session` 后重试。

2) 在 Telegram 中对 Bot 操作（推荐直接用输入框下方的菜单键盘）：

- `/menu`（呼出菜单键盘）
- “频道列表”里每个来源都有按钮：`开始/暂停监听`、`克隆`（历史下载→完成后自动监听）、`补克隆`（从指定消息ID/链接开始）、`删除`

提示：运行 `python manager_bot.py` 会同时启动下载监听；来源“开始监听”后会自动下载新媒体。

### 方式 B：命令行工具

### 1) 添加频道

```bash
python manage_sources.py add-channel @channel_username
```

### 2) 添加超级群 Forum 话题

会拉取话题列表并选择加入工作列表：

```bash
python manage_sources.py add-group -1001234567890
```

如果是私密超级群：请确保 `setup_user_session.py` 登录的用户账号已加入该群，并在 Telegram 客户端里打开过一次该群（让它出现在会话列表），否则仅凭 `-100...` ID 可能无法解析实体。

也可直接指定选择序号：

```bash
python manage_sources.py add-group -1001234567890 --select "1,3,5"
```

### 3) 查看来源

```bash
python manage_sources.py list
python manage_sources.py list --active
```

## 运行下载器

如你不想启用管理 Bot，也可以只运行下载器（仅媒体）：

```bash
python bot.py
```

启动时先执行一次历史克隆下载（按媒体组），然后继续监听：

```bash
python bot.py --backfill
```

## 目录结构

下载到 `DOWNLOAD_ROOT_DIR` 下：

```
<DOWNLOAD_ROOT_DIR>/
  <来源标题>/
    2024-12-22/
      TG@BieLaoZai_7304/
      TG@BieLaoZai_7310/
```

- `YYYY-MM-DD` 优先从消息文本中提取 `YYYY年MM月DD日`，否则使用消息发送日期。
- `TG@BieLaoZai_<id>` 的前缀固定为 `TG@BieLaoZai`，`id` 为该媒体组中最小的消息 ID（便于从指定 ID 计算/定位，重启也不会“目录编号一直冲下去”）。

## rclone 上传与自动删除

在 `.env` 中设置：
- `RCLONE_ENABLED=true`
- `RCLONE_REMOTE=pikpak:`
- `RCLONE_REMOTE_BASE_DIR=TGDownloads`（可选）
- `RCLONE_DELETE_LOCAL_AFTER_UPLOAD=true`

下载完成后会执行：
- `rclone copy <本地组目录> <remote目标目录>`
- 成功后删除本地组目录
