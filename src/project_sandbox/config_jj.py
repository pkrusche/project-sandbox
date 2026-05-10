from pathlib import Path


def render(home: Path, *, name: str | None, email: str | None) -> Path:
    cfg_dir = home / ".config" / "jj"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    out = cfg_dir / "config.toml"
    out.write_text(
        "[user]\n"
        f"name = \"{(name or '').replace('\\"', '\\\\"')}\"\n"
        f"email = \"{(email or '').replace('\\"', '\\\\"')}\"\n",
        encoding="utf-8",
    )
    return out
