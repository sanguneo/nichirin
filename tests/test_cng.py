"""AES layer tests: a known-answer vector, and proof the key handle is reusable."""

from __future__ import annotations

import unittest

from kakaoread.cng import AES_256_KEY_SIZE, PreparedKey, decrypt_cbc

from tests.support import encrypt

# NIST SP 800-38A, section F.2.6 (CBC-AES256.Decrypt).
NIST_KEY = bytes.fromhex(
    "603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4"
)
NIST_IV = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
NIST_CIPHERTEXT = bytes.fromhex("f58c4c04d6e5f1ba779eabfb5f7bfbd6")
NIST_PLAINTEXT = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")


class KnownAnswerTests(unittest.TestCase):
    def test_matches_the_nist_aes_256_cbc_vector(self) -> None:
        with PreparedKey(NIST_KEY) as key:
            self.assertEqual(key.decrypt(NIST_IV, NIST_CIPHERTEXT), NIST_PLAINTEXT)

    def test_one_shot_helper_agrees_with_a_prepared_key(self) -> None:
        with PreparedKey(NIST_KEY) as key:
            expected = key.decrypt(NIST_IV, NIST_CIPHERTEXT)
        self.assertEqual(decrypt_cbc(NIST_KEY, NIST_IV, NIST_CIPHERTEXT), expected)


class ReuseTests(unittest.TestCase):
    """The whole point of PreparedKey is that one handle serves many pages."""

    def test_many_sequential_decryptions_each_return_their_own_plaintext(self) -> None:
        key = bytes(range(32))
        cases = [
            (bytes([index] * 16), bytes([index + 1] * 32)) for index in range(1, 25)
        ]
        encrypted = [(iv, plaintext, encrypt(key, iv, plaintext)) for iv, plaintext in cases]

        with PreparedKey(key) as prepared:
            for iv, plaintext, ciphertext in encrypted:
                with self.subTest(iv=iv[:1].hex()):
                    self.assertEqual(prepared.decrypt(iv, ciphertext), plaintext)

    def test_repeating_one_input_keeps_returning_the_same_plaintext(self) -> None:
        with PreparedKey(NIST_KEY) as key:
            results = {key.decrypt(NIST_IV, NIST_CIPHERTEXT) for _ in range(10)}
        self.assertEqual(results, {NIST_PLAINTEXT})

    def test_decrypting_does_not_mutate_the_caller_iv(self) -> None:
        iv = bytes(NIST_IV)
        with PreparedKey(NIST_KEY) as key:
            key.decrypt(iv, NIST_CIPHERTEXT)
        self.assertEqual(iv, NIST_IV)

    def test_varying_ciphertext_length_reuses_the_output_buffer_safely(self) -> None:
        key = bytes(range(32))
        long_plaintext = bytes(range(256)) * 2
        short_plaintext = bytes(16)
        long_cipher = encrypt(key, NIST_IV, long_plaintext)
        short_cipher = encrypt(key, NIST_IV, short_plaintext)

        with PreparedKey(key) as prepared:
            self.assertEqual(prepared.decrypt(NIST_IV, long_cipher), long_plaintext)
            self.assertEqual(prepared.decrypt(NIST_IV, short_cipher), short_plaintext)
            self.assertEqual(prepared.decrypt(NIST_IV, long_cipher), long_plaintext)


class RejectionTests(unittest.TestCase):
    def test_rejects_keys_that_are_not_aes_256(self) -> None:
        for size in (0, 16, AES_256_KEY_SIZE - 1, AES_256_KEY_SIZE + 1):
            with self.subTest(size=size), self.assertRaises(ValueError):
                PreparedKey(bytes(size))

    def test_rejects_an_iv_that_is_not_one_block(self) -> None:
        with PreparedKey(NIST_KEY) as key:
            for size in (0, 15, 17):
                with self.subTest(size=size), self.assertRaises(ValueError):
                    key.decrypt(bytes(size), NIST_CIPHERTEXT)

    def test_rejects_ciphertext_that_is_not_block_aligned(self) -> None:
        with PreparedKey(NIST_KEY) as key:
            for size in (1, 15, 17, 31):
                with self.subTest(size=size), self.assertRaises(ValueError):
                    key.decrypt(NIST_IV, bytes(size))

    def test_empty_ciphertext_decrypts_to_nothing(self) -> None:
        with PreparedKey(NIST_KEY) as key:
            self.assertEqual(key.decrypt(NIST_IV, b""), b"")

    def test_refuses_to_decrypt_after_close(self) -> None:
        key = PreparedKey(NIST_KEY)
        key.close()
        with self.assertRaises(ValueError):
            key.decrypt(NIST_IV, NIST_CIPHERTEXT)

    def test_closing_twice_is_harmless(self) -> None:
        key = PreparedKey(NIST_KEY)
        key.close()
        key.close()


if __name__ == "__main__":
    unittest.main()
