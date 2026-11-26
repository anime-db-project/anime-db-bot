"""Reject known provider placeholder images before caching or rendering."""

from urllib.parse import urlsplit


def usable_image_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return False
    path = parsed.path.casefold()
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.hostname)
        and not any(
            marker in path
            for marker in (
                "/assets/globals/missing_",
                "/missing_original.",
                "/missing_preview.",
                "/no_image.",
                "/no-image.",
                "/no_picture.",
                "/questionmark_",
            )
        )
    )
