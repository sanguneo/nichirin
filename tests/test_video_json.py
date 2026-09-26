"""A video's address and what the client stored about it, in the JSON."""

from __future__ import annotations

import unittest

from kamado.model import video_in

ATTACHMENT = {
    "url": "https://talk.kakaocdn.net/synthetic/clip.mp4",
    "w": 1280,
    "h": 720,
    "d": 51,
    "s": 7290068,
    "expire": 1_791_010_544_863,
}


class VideoJsonTests(unittest.TestCase):
    def test_a_video_carries_its_address_and_metadata(self) -> None:
        video = video_in(3, ATTACHMENT)
        assert video is not None
        self.assertEqual(
            video.as_json(),
            {
                "url": "https://talk.kakaocdn.net/synthetic/clip.mp4",
                "width": 1280,
                "height": 720,
                "duration": 51,
                "size": 7290068,
                "expiresAt": "2026-10-03T06:55:44.863000+00:00",
            },
        )

    def test_an_expiry_the_client_did_not_store_is_absent(self) -> None:
        plain = {key: value for key, value in ATTACHMENT.items() if key != "expire"}
        video = video_in(3, plain)
        assert video is not None
        self.assertNotIn("expiresAt", video.as_json())

    def test_only_a_video_is_a_video(self) -> None:
        self.assertIsNone(video_in(2, ATTACHMENT))
        self.assertIsNone(video_in(3, {"w": 1, "h": 1}))


if __name__ == "__main__":
    unittest.main()
