from __future__ import annotations

from collections.abc import Callable, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bots.telegram.callbacks import (
    KeyedPageCallback,
    PagePickerCallback,
    SectionSourceCallback,
    TokenIndexCallback,
    ViewCallback,
)
from localization import text
from core.settings import get_settings

PICKER_PAGE_SIZE = 10  # compatibility default; actual size is configured
MAX_CARD_PICKER_CHOICES = 5000


def selection_page_size(block: str | None = None) -> int:
    """Block override when present; otherwise the shared Telegram page size."""
    settings = get_settings().telegram
    if block is None:
        return settings.pagination_page_size
    return settings.pagination_block_page_sizes.get(block, settings.pagination_page_size)


def picker_block_for_route(route: str) -> str | None:
    """A single route-to-settings mapping for all card selector callbacks."""
    parts = route.split("~")
    kind = parts[0]
    if kind in {"ap", "apg"}:
        return "episodes"
    if kind == "ar" and len(parts) > 1:
        return parts[1] if parts[1] in {"related", "recommendations"} else None
    if kind in {"ac"}:
        return "characters"
    if kind == "at":
        return "trailers"
    if kind == "ad":
        return "descriptions"
    if kind == "sr":
        return "search"
    if kind == "gx" and len(parts) >= 3:
        if parts[1] == "ac":
            return "characters"
        if parts[2] in {"characters", "staff", "voices"}:
            return parts[2]
    if kind == "pv" and len(parts) >= 3:
        if parts[1] == "ac":
            return "characters"
        if parts[2] in {"characters", "staff", "voices"}:
            return parts[2]
    if kind in {"av", "as"} and len(parts) >= 2:
        section = parts[1]
        if section == "all_ids":
            return "source_ids"
        if section in {"titles", "popularity", "screenshots", "staff", "voices"}:
            return section
    return None


def indexed_selection_rows(
    labels: Sequence[str],
    *,
    window: int,
    destination: Callable[[int], str],
    change_window: Callable[[int], str],
    current: int | None = None,
    page_size: int | None = None,
    block: str | None = None,
    total: int | None = None,
    fallback_label: Callable[[int], str] | None = None,
) -> tuple[list[list[InlineKeyboardButton]], int, int]:
    """Numbered single-column selection; resolve labels only for visible rows."""
    size = selection_page_size(block) if page_size is None else page_size
    if size < 1:
        raise ValueError("Page size must be positive")
    count = len(labels) if total is None else total
    if count < 0 or (count > len(labels) and fallback_label is None):
        raise ValueError("Missing labels for selection entries")
    pages = max(1, (count + size - 1) // size)
    window = max(0, min(int(window), pages - 1))
    start = window * size
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(start, min(count, start + size)):
        label = (
            labels[index]
            if index < len(labels)
            else fallback_label(index)
            if fallback_label is not None
            else ""
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{'✓ ' if current == index else ''}{index + 1}. {label}",
                    callback_data=destination(index),
                )
            ]
        )
    if pages > 1:
        navigation: list[InlineKeyboardButton] = []
        if window:
            navigation.append(
                InlineKeyboardButton(text="◀️", callback_data=change_window(window - 1))
            )
        navigation.append(
            InlineKeyboardButton(text=f"{window + 1}/{pages}", callback_data="noop")
        )
        if window + 1 < pages:
            navigation.append(
                InlineKeyboardButton(text="▶️", callback_data=change_window(window + 1))
            )
        rows.append(navigation)
    return rows, window, pages


def _default_label(route: str, index: int, locale: str) -> str:
    number = index + 1
    if route.startswith("apg~"):
        try:
            number += max(0, int(route.split("~")[2]))
        except (IndexError, ValueError):
            pass
    if route.startswith(("ap~", "apg~")):
        return f"{number} серия" if locale == "ru" else f"Episode {number}"
    if route.startswith("ar~"):
        return f"Аниме {number}" if locale == "ru" else f"Anime {number}"
    if route.startswith(("pv~", "ac~")):
        return f"Персона {number}" if locale == "ru" else f"Person {number}"
    if route.startswith("at~"):
        return f"Трейлер {number}" if locale == "ru" else f"Trailer {number}"
    if "screenshots" in route:
        return f"Скриншоты {number}" if locale == "ru" else f"Screenshots {number}"
    return f"Страница {number}" if locale == "ru" else f"Page {number}"


def _window_for(current: int, route: str) -> int:
    return max(0, int(current)) // selection_page_size(picker_block_for_route(route))


def card_picker_callback(token: str, route: str, current: int, total: int) -> str:
    return PagePickerCallback(
        "cp",
        token,
        route,
        current,
        total,
        _window_for(current, route),
    ).encode()


def search_picker_callback(token: str, current: int, total: int) -> str:
    return PagePickerCallback(
        "sp",
        token,
        "sr",
        current,
        total,
        _window_for(current, "sr"),
    ).encode()


def _people_page(display_index: int, without_portrait_count: int) -> int:
    shown = max(1, int(display_index) + 1)
    without = max(0, int(without_portrait_count))
    return 0 if shown <= without else shown - without


def card_route_callback(token: str, route: str, index: int) -> str:
    """Build the existing callback for one picker selection.

    Routes deliberately stay compact because Telegram limits callback_data to 64 bytes.
    ``pv`` routes translate the visible "shown N/total" position used by people and
    character galleries back to their portrait-page number.
    """

    parts = route.split("~")
    kind = parts[0] if parts else ""
    index = max(0, int(index))

    if kind == "av" and len(parts) == 2:
        return ViewCallback(token, parts[1], index).encode()
    if kind == "ad" and len(parts) == 2:
        return KeyedPageCallback("ad", token, parts[1], index).encode()
    if kind == "as" and len(parts) == 3:
        return SectionSourceCallback(token, parts[1], parts[2], index).encode()
    if kind in {"ap", "ac", "at"} and len(parts) == 2:
        return KeyedPageCallback(kind, token, parts[1], index).encode()
    if kind == "ar" and len(parts) == 3:
        return KeyedPageCallback("ar", token, f"{parts[1]}~{parts[2]}", index).encode()
    if kind == "apg" and len(parts) == 3:
        try:
            group_start = int(parts[2])
        except ValueError as exc:
            raise ValueError("Invalid episode-group page-picker route") from exc
        if group_start < 0:
            raise ValueError("Invalid episode-group page-picker route")
        return KeyedPageCallback("ap", token, parts[1], group_start + index).encode()

    if kind == "gx" and len(parts) in {4, 5}:
        # The initial list represents the unpictured group as ONE detail page,
        # not N different buttons all pointing to page 0.
        try:
            has_unpictured = int(parts[-1])
        except ValueError as exc:
            raise ValueError("Invalid grouped gallery selector") from exc
        if has_unpictured not in {0, 1}:
            raise ValueError("Invalid grouped gallery selector")
        page = index if has_unpictured else index + 1
        if parts[1] == "av" and len(parts) == 4:
            return ViewCallback(token, parts[2], page).encode()
        if parts[1] == "as" and len(parts) == 5:
            return SectionSourceCallback(token, parts[2], parts[3], page).encode()
        if parts[1] == "ac" and len(parts) == 4:
            return KeyedPageCallback("ac", token, parts[2], page).encode()
        raise ValueError("Unsupported grouped gallery selector")

    if kind == "pv" and len(parts) >= 4:
        try:
            without = int(parts[-1])
        except ValueError as exc:
            raise ValueError("Invalid people page-picker route") from exc
        page = _people_page(index, without)
        target = parts[1]
        if target == "av" and len(parts) == 4:
            return ViewCallback(token, parts[2], page).encode()
        if target == "as" and len(parts) == 5:
            return SectionSourceCallback(token, parts[2], parts[3], page).encode()
        if target == "ac" and len(parts) == 4:
            return KeyedPageCallback("ac", token, parts[2], page).encode()

    raise ValueError(f"Unsupported page-picker route: {route}")


def _picker_keyboard(
    data: PagePickerCallback,
    *,
    locale: str,
    destination: Callable[[int], str],
    labels: Sequence[str] | None = None,
    back_callback: str | None = None,
    show_back: bool = True,
    highlight_current: bool = True,
) -> InlineKeyboardMarkup:
    total = max(1, int(data.total))
    current = max(0, min(int(data.current), total - 1))
    rows, _, _ = indexed_selection_rows(
        labels if labels is not None else (),
        window=data.window,
        block=picker_block_for_route(data.route),
        total=total,
        fallback_label=lambda index: _default_label(data.route, index, locale),
        destination=destination,
        change_window=lambda window: PagePickerCallback(
            data.prefix, data.token, data.route, current, total, window
        ).encode(),
        current=current if highlight_current else None,
    )
    if show_back:
        rows.append(
            [
                InlineKeyboardButton(
                    text=text("common", "back", locale=locale),
                    callback_data=back_callback or destination(current),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_picker_keyboard(
    data: PagePickerCallback,
    *,
    locale: str,
    labels: Sequence[str] | None = None,
    back_callback: str | None = None,
    highlight_current: bool = True,
) -> InlineKeyboardMarkup:
    return _picker_keyboard(
        data,
        locale=locale,
        destination=lambda index: card_route_callback(data.token, data.route, index),
        labels=labels,
        back_callback=back_callback,
        highlight_current=highlight_current,
    )


def search_picker_keyboard(
    data: PagePickerCallback,
    *,
    locale: str,
    labels: Sequence[str] | None = None,
    show_back: bool = True,
) -> InlineKeyboardMarkup:
    if data.route != "sr":
        raise ValueError(f"Unsupported search page-picker route: {data.route}")
    return _picker_keyboard(
        data,
        locale=locale,
        destination=lambda index: TokenIndexCallback("sr", data.token, index).encode(),
        labels=labels,
        show_back=show_back,
        highlight_current=show_back,
    )
