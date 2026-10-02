from concurrent.futures import Executor, Future, ThreadPoolExecutor


class _InlineExecutor(Executor):
    """Keep batch Future/error handling without a thread for serial work."""

    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        future.set_running_or_notify_cancel()
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
        return future


def batch_executor(max_workers, item_count):
    """Use only useful batch workers; upstream calls retain their own deadlines."""
    worker_count = min(max(1, max_workers), item_count)
    if worker_count <= 1:
        return _InlineExecutor()
    # Orchestration must stay outside the upstream executor: these tasks submit
    # upstream work and wait for it, so sharing that pool would deadlock.
    return ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ytmusic-batch")
