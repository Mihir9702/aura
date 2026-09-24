from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]


def test_alembic_has_single_head():
    # Parallel slices that each add a migration must re-parent before merging; the
    # restore drill and `alembic upgrade head` both require one unambiguous head.
    config = Config(str(ROOT / "alembic.ini"))
    location = config.get_main_option("script_location")
    assert location
    config.set_main_option("script_location", str(ROOT / location).replace("%", "%%"))
    config.set_main_option("prepend_sys_path", "")  # revision metadata only; keep sys.path
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"Expected one Alembic head, found {sorted(heads)}"
