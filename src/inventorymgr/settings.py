from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path = PROJECT_ROOT / ".env") -> None:
    """Minimal .env loader (no dependency). Existing env vars take precedence."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class OdooSettings:
    url: str
    db: str
    user: str
    api_key: str


def odoo_settings(profile: str = "local") -> OdooSettings:
    """Load Odoo connection settings for a profile.

    'local' reads ODOO_URL/ODOO_DB/ODOO_USER/ODOO_API_KEY.
    'prod'  reads ODOO_PROD_URL/ODOO_PROD_DB/ODOO_PROD_USER/ODOO_PROD_API_KEY.
    """
    load_dotenv()
    prefix = "" if profile == "local" else "PROD_"
    try:
        return OdooSettings(
            url=os.environ[f"ODOO_{prefix}URL"],
            db=os.environ[f"ODOO_{prefix}DB"],
            user=os.environ[f"ODOO_{prefix}USER"],
            api_key=os.environ[f"ODOO_{prefix}API_KEY"],
        )
    except KeyError as exc:
        raise RuntimeError(
            f"Missing Odoo setting {exc} for profile {profile!r} — set it in inventorymgr/.env"
        ) from exc


@dataclass(frozen=True)
class GoogleSettings:
    sa_json_path: Path
    folder_id: str


def google_settings() -> GoogleSettings:
    load_dotenv()
    try:
        raw_path = os.environ["GOOGLE_SA_JSON"]
        folder_id = os.environ["GDRIVE_FOLDER_ID"]
    except KeyError as exc:
        raise RuntimeError(f"Missing Google setting {exc} — set it in inventorymgr/.env") from exc
    path = Path(raw_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return GoogleSettings(sa_json_path=path, folder_id=folder_id)
