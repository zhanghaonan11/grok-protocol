from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")

DEFAULT_MAX_WORKERS = 4


def run_ordered_parallel(
    items: Sequence[T],
    worker: Callable[[T], R],
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    on_item_done: Callable[[int, int, T], None] | None = None,
) -> list[R]:
    total = len(items)
    if total == 0:
        return []

    results: list[R | None] = [None] * total
    completed = 0
    workers = max(1, min(max_workers, total))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(worker, item): index for index, item in enumerate(items)
        }
        for future in as_completed(future_map):
            index = future_map[future]
            item = items[index]
            results[index] = future.result()
            completed += 1
            if on_item_done is not None:
                on_item_done(completed, total, item)

    return [result for result in results if result is not None]
