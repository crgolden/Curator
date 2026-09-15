"""The rotation window that ``GET /me/ps-plus-rotation`` measures ``added`` and ``leaving`` against."""

from datetime import datetime, timedelta, timezone

from curator.catalog.ps_plus_repository import _earliest_only_when_no_value_is_missing


def _completions(count: int) -> list[datetime]:
    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [first + timedelta(days=offset) for offset in range(count)]


def test_the_window_opens_at_the_earliest_completion_when_every_category_reports_one() -> None:
    earliest, latest = _completions(2)

    assert _earliest_only_when_no_value_is_missing([latest, earliest]) == earliest


def test_one_category_without_a_previous_completion_withholds_the_window_from_all_of_them() -> None:
    (only_completion,) = _completions(1)

    assert _earliest_only_when_no_value_is_missing([only_completion, None]) is None


def test_no_categories_at_all_withholds_the_window() -> None:
    assert _earliest_only_when_no_value_is_missing([]) is None
