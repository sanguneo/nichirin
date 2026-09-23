"""A picture's address and size in the JSON, shaped as the macOS build answers.

The client stores an address rather than the picture, so this is all a caller
needs to show one — a browser uses ``url`` directly — and nothing here fetches.
"""

from __future__ import annotations

import unittest

from kanao.model import Message, photo_in


def message(photo=None) -> Message:
    return Message(1, 2, 3, "someone", "", 2, 1_700_000_000, False, photo=photo)


class PhotoJsonTests(unittest.TestCase):
    def test_a_single_photo_is_one_picture(self) -> None:
        photo = photo_in(2, {"url": "https://talk.kakaocdn.net/a.jpg", "w": 40, "h": 30})
        assert photo is not None
        self.assertEqual(
            photo.as_json(),
            {
                "url": "https://talk.kakaocdn.net/a.jpg",
                "width": 40,
                "height": 30,
                "count": 1,
                "images": [{"url": "https://talk.kakaocdn.net/a.jpg", "width": 40, "height": 30}],
            },
        )

    def test_an_album_counts_its_pictures_and_leads_with_the_first(self) -> None:
        album = {
            "imageUrls": [f"https://talk.kakaocdn.net/{n}.jpg" for n in range(3)],
            "wl": [40, 50, 60],
            "hl": [30, 30, 30],
        }
        photo = photo_in(27, album)
        assert photo is not None
        self.assertEqual(photo.url, "https://talk.kakaocdn.net/0.jpg")
        self.assertEqual(photo.count, 3)
        self.assertEqual(
            photo.as_json()["images"],
            [
                {"url": f"https://talk.kakaocdn.net/{n}.jpg", "width": w, "height": 30}
                for n, w in enumerate((40, 50, 60))
            ],
        )

    def test_an_album_with_only_thumbnails_still_lists_every_picture(self) -> None:
        album = {
            "thumbnailUrls": [f"https://talk.kakaocdn.net/t{n}.jpg" for n in range(2)],
            "thumbnailWidths": [10, 20],
            "thumbnailHeights": [10, 20],
            "wl": [100, 200],
            "hl": [100, 200],
        }
        photo = photo_in(27, album)
        assert photo is not None
        self.assertEqual([one["url"] for one in photo.as_json()["images"]],
                         [f"https://talk.kakaocdn.net/t{n}.jpg" for n in range(2)])

    def test_a_size_the_list_lacks_is_zero_not_a_guess(self) -> None:
        album = {"imageUrls": ["https://talk.kakaocdn.net/0.jpg", "https://talk.kakaocdn.net/1.jpg"],
                 "wl": [40], "hl": [30]}
        photo = photo_in(27, album)
        assert photo is not None
        self.assertEqual(photo.as_json()["images"][1], {"url": "https://talk.kakaocdn.net/1.jpg", "width": 0, "height": 0})

    def test_the_key_appears_only_on_a_message_that_carries_one(self) -> None:
        self.assertNotIn("photo", message().as_json())
        photo = photo_in(2, {"url": "https://talk.kakaocdn.net/a.jpg", "w": 40, "h": 30})
        self.assertEqual(message(photo).as_json()["photo"]["url"], "https://talk.kakaocdn.net/a.jpg")


if __name__ == "__main__":
    unittest.main()
