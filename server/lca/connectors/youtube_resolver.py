"""Small, dependency-free YouTube live resolver for channel handles."""

from __future__ import annotations

import re
from urllib.request import Request, urlopen


VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


def resolve_live_video(handle: str) -> str:
    """Resolve a channel @handle to its currently live video ID.

    YouTube redirects /@handle/live to the active broadcast when one exists.
    We only accept IDs found in live-related page data, avoiding stale uploads.
    """
    clean = handle.removeprefix("@").strip()
    request = Request(
        f"https://www.youtube.com/@{clean}/live",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"},
    )
    try:
        with urlopen(request, timeout=12) as response:
            body = response.read(3_000_000).decode("utf-8", "ignore")
            final_url = response.geturl()
    except Exception as exc:
        raise RuntimeError("Unable to check YouTube Live for this handle") from exc

    final_match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", final_url)
    if final_match:
        return final_match.group(1)

    # Live pages commonly mark the active renderer with one of these flags.
    for marker in ("isLiveNow", "isLive", "liveBroadcastDetails"):
        for match in re.finditer(marker, body):
            window = body[max(0, match.start() - 600):match.start() + 900]
            ids = VIDEO_ID.findall(window)
            if ids:
                return ids[0]
    raise RuntimeError(f"YouTube channel @{clean} is not live or no active chat was found")
