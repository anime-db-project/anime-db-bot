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


def selection_page_size() -> int:
    """Shared, validated page size for all Telegram selection menus."""
    return get_settings().telegram.pagination_page_size


def indexed_selection_rows(
    labels: Sequence[str],
    *,
    window: int,
    destination: Callable[[int], str],
    change_window: Callable[[int], str],
    current: int | None = None,
    page_size: int | None = None,
) -> tuple[list[list[InlineKeyboardButton]], int, int]:
    """Numbered single-column selection; no extra number-grid picker."""
    size = selection_page_size() if page_size is None else page_size
    if size < 1:
        raise ValueError("Page size must be positive")
    total = len(labels)
    pages = max(1, (total + size - 1) // size)
    window = max(0, min(int(window), pages - 1))
    start = window * size
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if current == index else ''}{index + 1}. {labels[index]}",
                callback_data=destination(index),
            )
        ]
        for index in range(start, min(total, start + size))
    ]
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


def _window_for(current: int) -> int:
    return max(0, int(current)) // selection_page_size()


def card_picker_callback(token: str, route: str, current: int, total: int) -> str:
    return PagePickerCallback(
        "cp",
        token,
        route,
        current,
        total,
        _window_for(current),
    ).encode()


def search_picker_callback(token: str, current: int, total: int) -> str:
    return PagePickerCallback(
        "sp",
        token,
        "sr",
        current,
        total,
        _window_for(current),
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
) -> InlineKeyboardMarkup:
    total = max(1, int(data.total))
    current = max(0, min(int(data.current), total - 1))
    names = [
        str(labels[index])
        if labels is not None and index < len(labels)
        else _default_label(data.route, index, locale)
        for index in range(total)
    ]
    rows, _, _ = indexed_selection_rows(
        names,
        window=data.window,
        destination=destination,
        change_window=lambda window: PagePickerCallback(
            data.prefix, data.token, data.route, current, total, window
        ).encode(),
        current=current,
    )
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
) -> InlineKeyboardMarkup:
    return _picker_keyboard(
        data,
        locale=locale,
        destination=lambda index: card_route_callback(data.token, data.route, index),
        labels=labels,
        back_callback=back_callback,
    )


def search_picker_keyboard(
    data: PagePickerCallback,
    *,
    locale: str,
    labels: Sequence[str] | None = None,
) -> InlineKeyboardMarkup:
    if data.route != "sr":
        raise ValueError(f"Unsupported search page-picker route: {data.route}")
    return _picker_keyboard(
        data,
        locale=locale,
        destination=lambda index: TokenIndexCallback("sr", data.token, index).encode(),
        labels=labels,
    )
