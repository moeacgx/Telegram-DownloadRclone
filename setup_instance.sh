#!/bin/bash
# TG Bot 实例部署脚本
# 用法: ./setup_instance.sh <实例名> [源代码目录]
# 示例: ./setup_instance.sh instance1 /path/to/source

set -e

INSTANCE_NAME=$1
SOURCE_DIR=${2:-$(dirname "$0")}
BASE_DIR="/opt/tg-bots"

if [ -z "$INSTANCE_NAME" ]; then
    echo "❌ 用法: $0 <实例名> [源代码目录]"
    echo "   示例: $0 instance1"
    echo "   示例: $0 instance2 /home/user/TGChannel-Auto-Copy-Penmanboman"
    exit 1
fi

INSTANCE_DIR="$BASE_DIR/$INSTANCE_NAME"

echo "🚀 开始部署实例: $INSTANCE_NAME"
echo "📁 目标目录: $INSTANCE_DIR"
echo "📦 源代码目录: $SOURCE_DIR"
echo ""

# 创建目录结构
echo "📂 创建目录结构..."
mkdir -p "$INSTANCE_DIR/data"
mkdir -p "$INSTANCE_DIR/logs"
mkdir -p "$INSTANCE_DIR/sessions"

# 复制 Python 文件
echo "📄 复制代码文件..."
cp "$SOURCE_DIR"/*.py "$INSTANCE_DIR/" 2>/dev/null || true

# 复制依赖文件
if [ -f "$SOURCE_DIR/requirements.txt" ]; then
    cp "$SOURCE_DIR/requirements.txt" "$INSTANCE_DIR/"
    echo "✓ 已复制 requirements.txt"
fi

# 创建 .env 文件
if [ -f "$SOURCE_DIR/.env.example" ]; then
    cp "$SOURCE_DIR/.env.example" "$INSTANCE_DIR/.env"
    echo "✓ 已创建 .env 配置文件（从 .env.example）"
elif [ -f "$SOURCE_DIR/.env" ]; then
    cp "$SOURCE_DIR/.env" "$INSTANCE_DIR/.env"
    echo "✓ 已复制 .env 配置文件"
else
    echo "⚠️ 未找到 .env 模板，请手动创建"
fi

# 设置权限
chmod -R 755 "$INSTANCE_DIR"

echo ""
echo "✅ 实例 $INSTANCE_NAME 部署完成！"
echo ""
echo "📝 下一步操作:"
echo "   1. 编辑配置文件: nano $INSTANCE_DIR/.env"
echo "   2. 安装依赖: cd $INSTANCE_DIR && pip install -r requirements.txt"
echo "   3. 首次运行登录: cd $INSTANCE_DIR && python3 setup_user_session.py"
echo "   4. 启动下载器: cd $INSTANCE_DIR && python3 bot.py --backfill"
echo "   4. 使用 PM2 管理: pm2 start ecosystem.config.js --only tg-bot-X"
echo ""
echo "⚠️ 重要提醒:"
echo "   - 确保每个实例使用不同的 BOT_TOKEN"
echo "   - 确保每个实例使用不同的 USER_PHONE（或相同账号监听不同频道）"
echo "   - 确保每个实例使用不同的 TARGET_GROUP_ID 和 TEMP_CHANNEL_ID"
