"""Google Sheets output via a service account (creates sheets in a shared Drive folder)."""

from __future__ import annotations

import gspread
from google.oauth2.service_account import Credentials

from ..settings import GoogleSettings, google_settings

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class GSheets:
    def __init__(self, settings: GoogleSettings | None = None):
        self.s = settings or google_settings()
        creds = Credentials.from_service_account_file(str(self.s.sa_json_path), scopes=SCOPES)
        self.gc = gspread.authorize(creds)

    def create(self, title: str):
        """Create a new spreadsheet inside the configured Drive folder."""
        return self.gc.create(title, folder_id=self.s.folder_id)

    def open_by_key(self, key: str):
        return self.gc.open_by_key(key)
