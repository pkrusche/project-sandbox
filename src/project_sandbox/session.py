import datetime as dt
from pathlib import Path
import subprocess
import threading


def default_log_path(project: Path, branch: str | None, agent: str, *, create: bool = True) -> Path:
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{agent}-{branch.replace('/', '-') if branch else 'main'}-{ts}"
    log_dir = project / ".project-sandbox" / "sessions"
    if create:
        log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{stem}.log"


def run(argv: list[str], *, log_path: Path, timeout: int | None = None, dry_run: bool = False) -> int:
    if dry_run:
        print(" ".join(argv), "2>&1 | tee", str(log_path))
        if timeout:
            print(f"# timeout: {timeout}s")
        return 0

    with log_path.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert proc.stdout is not None
        output_thread = threading.Thread(target=_tee_output, args=(proc.stdout, handle), daemon=True)
        output_thread.start()
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return 124
        finally:
            output_thread.join(timeout=5)
            proc.stdout.close()


def _tee_output(stream, handle) -> None:
    for line in stream:
        print(line, end="")
        handle.write(line)
        handle.flush()
