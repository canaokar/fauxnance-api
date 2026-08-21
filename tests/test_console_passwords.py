from __future__ import annotations

import unittest

from src.console.passwords import hash_password, verify_password


class PasswordHashingTests(unittest.TestCase):
    def test_round_trip_verifies(self):
        encoded = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", encoded))

    def test_wrong_password_fails(self):
        encoded = hash_password("correct horse battery staple")
        self.assertFalse(verify_password("wrong password entirely", encoded))

    def test_malformed_encoded_strings_return_false_without_raising(self):
        password = "correct horse battery staple"
        cases = [
            "",
            "not-the-right-shape",
            "pbkdf2_sha256$notanumber$AA==$BB==",
            "pbkdf2_sha256$210000$not-base64!!!$BB==",
            "pbkdf2_sha256$210000$AA==$not-base64!!!",
            "bcrypt$210000$AA==$BB==",
            "pbkdf2_sha256$210000$AA==",
            None,
        ]
        for encoded in cases:
            with self.subTest(encoded=encoded):
                self.assertFalse(verify_password(password, encoded))  # type: ignore[arg-type]

    def test_out_of_range_iteration_counts_return_false_without_raising(self):
        encoded = hash_password("correct horse battery staple")
        _, _, salt, derived = encoded.split("$")
        for iterations in ("0", "-1", "99999999999"):
            with self.subTest(iterations=iterations):
                tampered = "$".join(["pbkdf2_sha256", iterations, salt, derived])
                self.assertFalse(verify_password("correct horse battery staple", tampered))


if __name__ == "__main__":
    unittest.main()
