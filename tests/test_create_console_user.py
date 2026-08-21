from __future__ import annotations

import argparse
import unittest

from src.admin.repository import RepositoryConflict
from scripts.create_console_user import _parser, _run, create_console_user


PASSWORD = "correct-horse-battery"


class FakeRepository:
    def __init__(self, *, conflict: bool = False) -> None:
        self.items: list[dict] = []
        self._conflict = conflict

    def create_user(self, item: dict) -> None:
        if self._conflict:
            raise RepositoryConflict("email already registered")
        self.items.append(dict(item))


class CreateConsoleUserTests(unittest.TestCase):
    def test_password_is_never_accepted_from_argv(self):
        with self.assertRaises(SystemExit):
            _parser().parse_args(
                ["--email", "a@b.com", "--name", "A", "--password", PASSWORD]
            )

    def test_duplicate_email_exits_non_zero(self):
        args = argparse.Namespace(email="a@b.com", name="A", role="admin")
        code = _run(
            args, FakeRepository(conflict=True), password=PASSWORD, generated=False
        )
        self.assertEqual(code, 1)

    def test_written_item_has_hashed_not_plaintext_password(self):
        repository = FakeRepository()
        item = create_console_user(
            repository,
            email="Instructor@Example.com",
            name="Ada",
            role="instructor",
            password=PASSWORD,
        )
        self.assertEqual(repository.items, [item])
        self.assertEqual(item["email"], "instructor@example.com")
        self.assertEqual(item["role"], "instructor")
        self.assertEqual(item["status"], "active")
        self.assertNotEqual(item["passwordHash"], PASSWORD)
        self.assertTrue(item["passwordHash"].startswith("pbkdf2_sha256$"))


if __name__ == "__main__":
    unittest.main()
