from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from memecoin_bot.worker_lock import WorkerAlreadyRunningError, exclusive_worker_lock


def test_only_one_dashboard_can_own_a_paper_journal(tmp_path: Path) -> None:
    state = tmp_path / "paper.sqlite3"
    with (
        exclusive_worker_lock(state),
        pytest.raises(WorkerAlreadyRunningError),
        exclusive_worker_lock(state),
    ):
        pass
    with exclusive_worker_lock(state):
        pass


def test_lock_rejects_another_process_and_releases_after_exit(tmp_path: Path) -> None:
    state = tmp_path / "paper.sqlite3"
    probe = (
        "import sys; from pathlib import Path; "
        "from memecoin_bot.worker_lock import WorkerAlreadyRunningError, exclusive_worker_lock; "
        "\ntry:\n"
        "    with exclusive_worker_lock(Path(sys.argv[1])): pass\n"
        "except WorkerAlreadyRunningError:\n"
        "    sys.exit(17)\n"
    )

    def child_exit_code() -> int:
        # Fixed interpreter and source; the temporary path is an argument, not shell text.
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", probe, str(state)],
            capture_output=True,
            check=False,
            timeout=10,
        )
        return result.returncode

    with exclusive_worker_lock(state):
        assert child_exit_code() == 17
    assert child_exit_code() == 0
