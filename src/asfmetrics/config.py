"""Configuration loading for asfmetrics.

Lookup order:
1. ./config.yml (project-local)
2. ~/.asfmetrics/config.yml (user-level)
3. /etc/asfmetrics/config.yml (system-level)
"""

from pathlib import Path

import yaml


CONFIG_SEARCH_PATHS = [
    Path("./config.yml"),
    Path.home() / ".asfmetrics" / "config.yml",
    Path("/etc/asfmetrics/config.yml"),
]


def find_config() -> Path | None:
    """Find the first config file that exists."""
    for path in CONFIG_SEARCH_PATHS:
        if path.exists():
            return path
    return None


def load_config(path: Path | None = None) -> dict:
    """Load configuration from YAML file.

    Args:
        path: Explicit path to config. If None, searches default locations.

    Returns:
        Parsed config dict.

    Raises:
        FileNotFoundError: If no config file is found.
    """
    if path is None:
        path = find_config()
    if path is None:
        raise FileNotFoundError(
            "No config.yml found. Searched:\n"
            + "\n".join(f"  - {p}" for p in CONFIG_SEARCH_PATHS)
            + "\nCopy config.example.yml to config.yml to get started."
        )
    with open(path) as f:
        return yaml.safe_load(f)
