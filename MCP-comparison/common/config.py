"""Load harness configuration from config.yaml / config.local.yaml / env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_DIR = REPO_ROOT / "MCP-comparison"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_raw() -> dict[str, Any]:
    config_path = HARNESS_DIR / "config.yaml"
    local_path = HARNESS_DIR / "config.local.yaml"
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if local_path.exists():
        with open(local_path, encoding="utf-8") as f:
            data = _deep_merge(data, yaml.safe_load(f) or {})
    return data


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    base_url: str
    api_key_env: str
    model_id: str
    temperature: float
    max_tokens: int
    system_prompt: str | None = None

    @property
    def api_key(self) -> str:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError(f"Environment variable {self.api_key_env} is not set")
        return key


@dataclass(frozen=True)
class BudgetsConfig:
    max_rounds: int
    problem_wall_cap_seconds: float
    tool_timeout_seconds: float
    repeats: int


@dataclass(frozen=True)
class PathsConfig:
    problems_dir: Path
    runs_dir: Path


@dataclass(frozen=True)
class MCPServerConfig:
    command: list[str]
    env: dict[str, str]


@dataclass(frozen=True)
class IsabelleMCPContainerConfig:
    container_name: str | None
    host_work_dir: Path | None
    container_work_dir: str


@dataclass(frozen=True)
class Config:
    model: ModelConfig
    budgets: BudgetsConfig
    paths: PathsConfig
    mcp_servers: dict[str, MCPServerConfig]
    isabelle_mcp_container: IsabelleMCPContainerConfig
    arbiter_isabelle_bin: str
    arbiter_cleanup: bool
    system_prompt: str | None = None


def load() -> Config:
    data = load_raw()
    model = ModelConfig(**data["model"])
    budgets = BudgetsConfig(**data["budgets"])
    paths_cfg = data["paths"]
    paths = PathsConfig(
        problems_dir=REPO_ROOT / paths_cfg["problems_dir"],
        runs_dir=REPO_ROOT / paths_cfg["runs_dir"],
    )
    mcp_servers = {
        name: MCPServerConfig(
            command=cfg["command"],
            env={k: os.path.expandvars(v) for k, v in cfg.get("env", {}).items()},
        )
        for name, cfg in data["mcp_servers"].items()
    }
    container_cfg = data.get("isabelle_mcp_container", {})
    isabelle_mcp_container = IsabelleMCPContainerConfig(
        container_name=container_cfg.get("container_name") or None,
        host_work_dir=Path(container_cfg["host_work_dir"]) if container_cfg.get("host_work_dir") else None,
        container_work_dir=container_cfg.get("container_work_dir", "/work"),
    )
    arbiter_cfg = data.get("arbiter", {})
    system_prompt = data.get("system_prompt") or None
    return Config(
        model=model,
        budgets=budgets,
        paths=paths,
        mcp_servers=mcp_servers,
        isabelle_mcp_container=isabelle_mcp_container,
        arbiter_isabelle_bin=arbiter_cfg.get("isabelle_bin", "isabelle"),
        arbiter_cleanup=arbiter_cfg.get("cleanup", True),
        system_prompt=system_prompt,
    )
