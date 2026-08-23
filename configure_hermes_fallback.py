from pathlib import Path
import shutil
import yaml

path = Path("/opt/streetsmart-hermes/.hermes/config.yaml")
backup = path.with_suffix(".yaml.pre-fallback")
if not backup.exists():
    shutil.copy2(path, backup)
config = yaml.safe_load(path.read_text()) or {}
config["fallback_providers"] = [
    {"provider": "vertex", "model": "google/gemini-2.5-flash"}
]
path.write_text(yaml.safe_dump(config, sort_keys=False))
print("configured Vertex fallback: google/gemini-2.5-flash")
