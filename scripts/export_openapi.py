import json
from pathlib import Path

from voice_studio_core.config import Settings
from voice_studio_service.api import create_app

root = Path(__file__).resolve().parents[1]
app = create_app(Settings(worker_enabled=False, data_dir=root / "data" / "schema-export"))
(root / "contracts" / "openapi.json").write_text(
    json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
)
