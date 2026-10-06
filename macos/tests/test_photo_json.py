"""A picture's address and size in the JSON, every picture of an album included."""

from __future__ import annotations

import unittest

from kamado.model import photo_in


class PhotoJsonTests(unittest.TestCase):
    def test_a_single_photo_lists_itself(self) -> None:
        photo = photo_in({"url": "https://talk.kakaocdn.net/a.jpg", "w": 40, "h": 30}, 2)
        assert photo is not None
        self.assertEqual(
            photo.as_json()["images"],
            [{"url": "https://talk.kakaocdn.net/a.jpg", "width": 40, "height": 30}],
        )

    def test_an_album_lists_every_picture_in_order(self) -> None:
        album = {
            "imageUrls": [f"https://talk.kakaocdn.net/{n}.jpg" for n in range(3)],
            "wl": [40, 50, 60],
            "hl": [30, 30, 30],
            "thumbnailWidths": [4, 5, 6],
            "thumbnailHeights": [3, 3, 3],
        }
        photo = photo_in(album, 27)
        assert photo is not None
        rendered = photo.as_json()
        self.assertEqual(rendered["count"], 3)
        self.assertEqual(
            rendered["images"],
            [
                {"url": f"https://talk.kakaocdn.net/{n}.jpg", "width": w, "height": 30}
                for n, w in enumerate((40, 50, 60))
            ],
        )

    def test_a_size_the_list_lacks_is_zero_not_a_guess(self) -> None:
        album = {"imageUrls": ["https://talk.kakaocdn.net/0.jpg", "https://talk.kakaocdn.net/1.jpg"],
                 "wl": [40], "hl": [30]}
        photo = photo_in(album, 27)
        assert photo is not None
        self.assertEqual(photo.as_json()["images"][1],
                         {"url": "https://talk.kakaocdn.net/1.jpg", "width": 0, "height": 0})


if __name__ == "__main__":
    unittest.main()
