"""The byte-budgeted cache both builds keep pictures in."""

from __future__ import annotations

import unittest

from kakaocommon.budget import BudgetedCache


class BudgetedCacheTests(unittest.TestCase):
    def test_the_oldest_entries_go_once_the_bytes_pass_the_limit(self) -> None:
        cache: BudgetedCache[int, bytes] = BudgetedCache(10)
        for key in range(4):
            cache.put(key, b"xxxx")
        self.assertLessEqual(cache.total, 10)
        self.assertEqual([key for key in range(4) if key in cache], [2, 3])

    def test_reading_an_entry_makes_it_the_newest(self) -> None:
        cache: BudgetedCache[int, bytes] = BudgetedCache(8)
        cache.put(1, b"xxxx")
        cache.put(2, b"xxxx")
        cache.get(1)
        cache.put(3, b"xxxx")
        self.assertIn(1, cache)
        self.assertNotIn(2, cache)

    def test_kept_keys_and_the_new_one_survive_past_the_limit(self) -> None:
        cache: BudgetedCache[int, bytes] = BudgetedCache(4)
        cache.put(1, b"xxxx")
        cache.put(2, b"yyyy", keep={1})
        self.assertIn(1, cache)
        self.assertIn(2, cache)
        self.assertEqual(cache.total, 8)

    def test_an_entry_larger_than_the_limit_is_still_kept_while_new(self) -> None:
        cache: BudgetedCache[int, bytes] = BudgetedCache(4)
        cache.put(1, b"x" * 100)
        self.assertEqual(cache.get(1), b"x" * 100)

    def test_the_entry_count_bounds_too(self) -> None:
        cache: BudgetedCache[int, str] = BudgetedCache(1 << 20, max_entries=2)
        for key in range(5):
            cache.put(key, "e")
        self.assertEqual(len(cache), 2)

    def test_replacing_and_clearing_keep_the_total_true(self) -> None:
        cache: BudgetedCache[int, bytes] = BudgetedCache(100)
        cache.put(1, b"xxxx")
        cache.put(1, b"xx")
        self.assertEqual(cache.total, 2)
        cache.clear()
        self.assertEqual((cache.total, len(cache)), (0, 0))


if __name__ == "__main__":
    unittest.main()
