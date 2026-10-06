"""Which words of a conversation can say what it was about.

Both extractive summaries draw on these -- the `/sum` digest and the topic
panel -- in both builds, and a word one of them counts and the other drops
would make the two describe the same hour differently. So the lists live once,
here, and each build's ``tui/digest.py`` keeps its old private names for them.
"""

from __future__ import annotations

import re
from typing import Final

WORD: Final = re.compile(r"[0-9A-Za-z가-힣]{2,}")
"""A word worth counting: two or more Hangul syllables, Latin letters or digits."""

STOPWORDS: Final = frozenset(
    """
    그리고 그래서 그런데 하지만 그러면 그러니까 근데 진짜 정말 아니 우리 저기 여기 거기
    이거 그거 저거 뭐가 뭔가 이런 저런 그런 하는 하고 해서 하면 한데 있는 있다 없는 없다
    같은 같이 때문 지금 오늘 내일 어제 그냥 조금 많이 다시 아직 이제 계속 혹시 일단
    the and for you are not but with this that have from was were will can all
    네네 ㅇㅇ ㅋㅋ ㅋㅋㅋ ㅎㅎ ㅎㅎㅎ ㅠㅠ ㅜㅜ
    """.split()
)
"""Particles and fillers carry no topic, and in Korean they dominate any raw
frequency count, so they are removed before ranking."""

PLACEHOLDERS: Final = frozenset(
    "사진 동영상 이모티콘 파일 음성메시지 보이스톡 페이스톡 선물 링크 삭제된 지도 연락처".split()
)
"""The client writes a placeholder word into the text column for attachments,
so without this a photo-heavy hour reports "사진" as what it was about."""
