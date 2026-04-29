import tempfile
import unittest
from pathlib import Path

from database import Database


class TestDatabaseTelegramAccounts(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test.db")
        self.db = Database(self.db_path)

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_create_tg_account(self) -> None:
        account_id = self.db.create_tg_account(
            session_name="session_a",
            label="账号A",
            phone="+8613800000000",
            username="user_a",
            user_id=1001,
        )

        account = self.db.get_tg_account(account_id)
        self.assertIsNotNone(account)
        self.assertEqual(account["session_name"], "session_a")
        self.assertEqual(account["label"], "账号A")
        self.assertEqual(account["phone"], "+8613800000000")
        self.assertEqual(account["username"], "user_a")
        self.assertEqual(account["user_id"], 1001)
        self.assertFalse(account["is_active"])
        self.assertEqual(account["status"], "new")

    def test_set_active_tg_account_is_exclusive(self) -> None:
        first_id = self.db.create_tg_account(session_name="session_a", label="账号A", is_active=True)
        second_id = self.db.create_tg_account(session_name="session_b", label="账号B")

        self.assertEqual(self.db.get_active_tg_account()["id"], first_id)

        updated = self.db.set_active_tg_account(second_id)
        self.assertTrue(updated)

        active_account = self.db.get_active_tg_account()
        self.assertIsNotNone(active_account)
        self.assertEqual(active_account["id"], second_id)

        first_account = self.db.get_tg_account(first_id)
        second_account = self.db.get_tg_account(second_id)
        self.assertFalse(first_account["is_active"])
        self.assertTrue(second_account["is_active"])

        accounts = self.db.list_tg_accounts()
        self.assertEqual(sum(1 for item in accounts if item["is_active"]), 1)

    def test_update_tg_account_fields(self) -> None:
        account_id = self.db.create_tg_account(session_name="session_update", label="旧标签")

        updated = self.db.update_tg_account(
            account_id,
            label="新标签",
            username="new_user",
            status="authorized",
            status_message="登录成功",
            last_login_at="2026-04-29 18:30:00",
        )
        self.assertTrue(updated)

        account = self.db.get_tg_account(account_id)
        self.assertEqual(account["label"], "新标签")
        self.assertEqual(account["username"], "new_user")
        self.assertEqual(account["status"], "authorized")
        self.assertEqual(account["status_message"], "登录成功")
        self.assertEqual(account["last_login_at"], "2026-04-29 18:30:00")

    def test_delete_tg_account(self) -> None:
        account_id = self.db.create_tg_account(session_name="session_delete", label="待删除")

        deleted = self.db.delete_tg_account(account_id)
        self.assertTrue(deleted)
        self.assertIsNone(self.db.get_tg_account(account_id))
        self.assertFalse(self.db.delete_tg_account(account_id))

    def test_set_all_download_sources_active(self) -> None:
        first_id = self.db.add_download_source(
            source_type="channel",
            chat_id=-10010001,
            display_title="来源A",
            is_active=False,
        )
        second_id = self.db.add_download_source(
            source_type="channel",
            chat_id=-10010002,
            display_title="来源B",
            is_active=True,
        )

        changed = self.db.set_all_download_sources_active(True)
        self.assertEqual(changed, 2)
        self.assertTrue(self.db.get_download_source(first_id)["is_active"])
        self.assertTrue(self.db.get_download_source(second_id)["is_active"])

        changed = self.db.set_all_download_sources_active(False)
        self.assertEqual(changed, 2)
        self.assertFalse(self.db.get_download_source(first_id)["is_active"])
        self.assertFalse(self.db.get_download_source(second_id)["is_active"])


if __name__ == "__main__":
    unittest.main()
