"""Write collected metrics to JSON data store."""

import json
from pathlib import Path


def write_json(project: str, stats: dict, config: dict) -> Path:
    """Write project stats to a JSON file.

    Args:
        project: Project name.
        stats: Collected stats dict.
        config: Full config dict.

    Returns:
        Path to written JSON file.
    """
    output_dir = Path(
        config.get("output", {}).get("json_dir", "./site/data/")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    out_path = output_dir / f"{project}.json"
    with open(out_path, "w") as f:
        json.dump(stats, f, indent=2, default=str)

    return out_path
