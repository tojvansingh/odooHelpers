"""Google Sheets output via a service account (creates sheets in a shared Drive folder).

Drive calls use supportsAllDrives so they work whether the folder is in My Drive or a
Shared Drive.
"""

from __future__ import annotations

import gspread
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials

from ..settings import GoogleSettings, google_settings

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
_DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
_SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"


class GSheets:
    def __init__(self, settings: GoogleSettings | None = None):
        self.s = settings or google_settings()
        self.creds = Credentials.from_service_account_file(str(self.s.sa_json_path), scopes=SCOPES)
        self.gc = gspread.authorize(self.creds)
        self._session = AuthorizedSession(self.creds)  # raw Drive API (supportsAllDrives)

    # ---- Drive helpers ----
    def list_in_folder(self) -> list[dict]:
        params = {
            "q": f"'{self.s.folder_id}' in parents and trashed=false",
            "fields": "files(id,name,mimeType)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "pageSize": "1000",
        }
        r = self._session.get(_DRIVE_FILES, params=params)
        r.raise_for_status()
        return r.json().get("files", [])

    def trash(self, file_id: str) -> None:
        """Move a file to the Drive trash (recoverable for 30 days)."""
        r = self._session.patch(
            f"{_DRIVE_FILES}/{file_id}", params={"supportsAllDrives": "true"}, json={"trashed": True}
        )
        r.raise_for_status()

    # ---- Spreadsheet helpers ----
    def create(self, title: str):
        return self.gc.create(title, folder_id=self.s.folder_id)

    def open_or_create(self, title: str):
        """Reuse the spreadsheet with this exact name in the folder, else create it."""
        for f in self.list_in_folder():
            if f.get("name") == title and f.get("mimeType") == _SPREADSHEET_MIME:
                return self.gc.open_by_key(f["id"])
        return self.create(title)

    def open_by_key(self, key: str):
        return self.gc.open_by_key(key)
