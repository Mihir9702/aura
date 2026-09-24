import json
from pathlib import Path

from aura.api import create_app
from aura.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_openapi_matches_committed_contract():
    # Schema generation never connects; the placeholder URL and key are not credentials.
    app = create_app(
        Settings(
            database_url="postgresql+psycopg://schema@127.0.0.1/schema",
            owner_key="schema-generation-only-not-a-real-key",
        )
    )
    try:
        committed = json.loads(
            (ROOT / "packages/contracts/openapi.json").read_text(encoding="utf-8")
        )
        assert app.openapi() == committed, (
            "OpenAPI drifted from packages/contracts/openapi.json; run scripts/contracts.ps1"
        )
    finally:
        app.state.database.engine.dispose()
