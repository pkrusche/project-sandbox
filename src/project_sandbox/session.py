import datetime as dt
from pathlib import Path
import subprocess


def default_log_path(project: Path, branch: str | None, agent: str) -> Path:
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{agent}-{branch.replace('/', '-') if branch else 'main'}-{ts}"
    log_dir = project / ".project-sandbox" / "sessions"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{stem}.log"


def run(argv: list[str], *, log_path: Path, timeout: int | None = None, dry_run: bool = False) -> int:
    cmd = argv
    if timeout:
        cmd = ["timeout", "--kill-after=30", str(timeout)] + cmd
    if dry_run:
        print(" ".join(cmd), "2>&1 | tee", str(log_path))
        return 0

    with log_path.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")
            handle.write(line)
        return proc.wait()
