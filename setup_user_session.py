#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
用户账号登录脚本
功能：初始化用户账号会话，支持 2FA 认证
"""

import os
import sys
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv
try:
    import socks
except ImportError:
    socks = None

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


def load_config():
    """加载 .env 或系统环境变量"""
    load_dotenv()

    def _env_int(name, default=None):
        value = os.environ.get(name)
        if value is None or value == '':
            return default
        try:
            return int(value)
        except ValueError:
            print(f"❌ 环境变量 {name} 必须是整数")
            sys.exit(1)

    def _env_bool(name, default=False):
        value = os.environ.get(name)
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    config = {
        'api_id': _env_int('API_ID'),
        'api_hash': os.environ.get('API_HASH'),
        'user_phone': os.environ.get('USER_PHONE'),
        'proxy': {
            'enabled': _env_bool('PROXY_ENABLED', False),
            'mode': os.environ.get('PROXY_MODE', 'manual'),
            'type': os.environ.get('PROXY_TYPE', 'socks5'),
            'host': os.environ.get('PROXY_HOST', '127.0.0.1'),
            'port': _env_int('PROXY_PORT', 1080),
            'username': os.environ.get('PROXY_USERNAME'),
            'password': os.environ.get('PROXY_PASSWORD'),
        }
    }

    required_fields = {
        'API_ID': config['api_id'],
        'API_HASH': config['api_hash'],
    }

    for env_name, value in required_fields.items():
        if not value:
            print(f"❌ 请在 .env 或环境变量中配置 {env_name}")
            sys.exit(1)

    return config


def build_proxy(config):
    """构建 Telethon 代理配置"""
    try:
        proxy_cfg = config.get('proxy', {})
        if not proxy_cfg or not proxy_cfg.get('enabled'):
            return None

        if socks is None:
            print("⚠️ 代理已启用但未安装 PySocks")
            print("运行: python -m pip install -r requirements.txt")
            sys.exit(1)

        def map_type(t):
            t = (t or '').lower()
            if t == 'socks5':
                return socks.SOCKS5
            elif t == 'socks4':
                return socks.SOCKS4
            elif t == 'http':
                return socks.HTTP
            return socks.SOCKS5

        mode = proxy_cfg.get('mode', 'manual').lower()

        if mode == 'env':
            # 从环境变量读取代理
            env_val = (
                os.environ.get('HTTPS_PROXY') or
                os.environ.get('https_proxy') or
                os.environ.get('HTTP_PROXY') or
                os.environ.get('http_proxy') or
                os.environ.get('ALL_PROXY') or
                os.environ.get('all_proxy')
            )
            if not env_val:
                print("⚠️ 代理模式为 env，但未检测到系统代理环境变量")
                return None

            parsed = urllib.parse.urlparse(env_val)
            p_type = map_type(parsed.scheme)
            host = parsed.hostname or '127.0.0.1'
            port = int(parsed.port or 1080)
            username = urllib.parse.unquote(parsed.username) if parsed.username else None
            password = urllib.parse.unquote(parsed.password) if parsed.password else None

            return (p_type, host, port, True, username, password)

        # manual 模式
        p_type = map_type(proxy_cfg.get('type', 'socks5'))
        host = proxy_cfg.get('host', '127.0.0.1')
        port = int(proxy_cfg.get('port', 1080))
        username = proxy_cfg.get('username')
        password = proxy_cfg.get('password')

        return (p_type, host, port, True, username, password)

    except Exception as e:
        print(f"⚠️ 构建代理配置失败: {e}")
        return None


async def login_user(config):
    """用户账号登录"""
    print("=" * 50)
    print("用户账号登录向导")
    print("=" * 50)

    # 确保 sessions 目录存在
    sessions_dir = Path('sessions')
    sessions_dir.mkdir(exist_ok=True)

    # 构建代理
    proxy = build_proxy(config)
    if proxy:
        print(f"✅ 使用代理: {proxy[1]}:{proxy[2]}")

    # 创建客户端
    client = TelegramClient(
        'sessions/user_session',
        config['api_id'],
        config['api_hash'],
        proxy=proxy
    )

    await client.connect()

    # 检查是否已登录
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"\n✅ 已登录: {me.first_name} (@{me.username})")
        print(f"📱 手机号: {me.phone}")
        print("\n会话文件: sessions/user_session.session")
        await client.disconnect()
        return

    # 开始登录流程
    phone = config['user_phone']
    if not phone:
        phone = input("\n请输入手机号（含国家码，如 +86...）：").strip()
        if not phone:
            print("❌ 手机号不能为空")
            sys.exit(1)
    print(f"\n📱 手机号: {phone}")
    print("正在发送验证码...")

    try:
        await client.send_code_request(phone)
        print("✅ 验证码已发送到您的 Telegram 账号")

        # 输入验证码
        code = input("\n请输入验证码: ").strip()

        try:
            await client.sign_in(phone, code)
            print("✅ 登录成功!")

        except SessionPasswordNeededError:
            # 需要两步验证密码
            print("\n🔐 检测到两步验证（2FA）")
            password = input("请输入两步验证密码: ").strip()
            await client.sign_in(password=password)
            print("✅ 登录成功!")

        # 显示用户信息
        me = await client.get_me()
        print(f"\n👤 用户信息:")
        print(f"   姓名: {me.first_name} {me.last_name or ''}")
        print(f"   用户名: @{me.username}")
        print(f"   用户ID: {me.id}")
        print(f"   手机号: {me.phone}")

        print(f"\n✅ 会话已保存: sessions/user_session.session")
        print("现在可以运行主程序: python bot.py")

    except Exception as e:
        print(f"\n❌ 登录失败: {e}")
        sys.exit(1)

    finally:
        await client.disconnect()


def main():
    """主函数"""
    import asyncio

    # 加载配置
    config = load_config()

    # 运行登录流程
    asyncio.run(login_user(config))


if __name__ == "__main__":
    main()
