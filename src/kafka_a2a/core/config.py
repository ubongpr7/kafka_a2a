from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _repo_root() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "docker-compose.ka2a-local.yml").exists():
            return candidate
        if (candidate / "agent_cards").exists() and (candidate / "prompts").exists():
            return candidate
    return current.parents[3]


def _default_data_dir(repo_root: Path) -> Path:
    if (repo_root / "docker-compose.ka2a-local.yml").exists():
        return repo_root / "kafka_a2a" / ".data"
    return repo_root / ".data"


@dataclass(slots=True)
class A2AAppSettings:
    repo_root: Path
    data_dir: Path
    control_plane_store_path: Path
    database_url: str | None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "A2AAppSettings":
        env_map = env or os.environ
        repo_root = _repo_root()
        data_dir = Path(env_map.get("KA2A_DATA_DIR") or _default_data_dir(repo_root))
        store_path = Path(
            env_map.get("KA2A_CONTROL_PLANE_STORE_PATH") or (data_dir / "control_plane.json")
        )
        return cls(
            repo_root=repo_root,
            data_dir=data_dir,
            control_plane_store_path=store_path,
            database_url=(env_map.get("DATABASE_URL") or env_map.get("KA2A_DATABASE_URL") or "").strip() or None,
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.control_plane_store_path.parent.mkdir(parents=True, exist_ok=True)
