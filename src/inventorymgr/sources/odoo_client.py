"""Thin XML-RPC client for Odoo (works against local Docker and Odoo.sh alike)."""

from __future__ import annotations

import xmlrpc.client

from ..settings import OdooSettings, odoo_settings


class OdooClient:
    def __init__(self, settings: OdooSettings | None = None):
        self.s = settings or odoo_settings()
        self._common = xmlrpc.client.ServerProxy(f"{self.s.url}/xmlrpc/2/common")
        self._models = xmlrpc.client.ServerProxy(f"{self.s.url}/xmlrpc/2/object")
        self._uid: int | None = None

    @property
    def uid(self) -> int:
        if self._uid is None:
            uid = self._common.authenticate(self.s.db, self.s.user, self.s.api_key, {})
            if not uid:
                raise RuntimeError("Odoo authentication failed — check inventorymgr/.env")
            self._uid = uid
        return self._uid

    def version(self) -> dict:
        return self._common.version()

    def execute_kw(self, model: str, method: str, args: list, kwargs: dict | None = None):
        return self._models.execute_kw(
            self.s.db, self.uid, self.s.api_key, model, method, args, kwargs or {}
        )

    def search_read(self, model: str, domain: list, fields: list[str], **kw) -> list[dict]:
        return self.execute_kw(model, "search_read", [domain], {"fields": fields, **kw})

    def search_count(self, model: str, domain: list) -> int:
        return self.execute_kw(model, "search_count", [domain])

    def read_group(self, model: str, domain: list, fields: list[str], groupby: list[str], **kw):
        return self.execute_kw(model, "read_group", [domain, fields, groupby], kw)

    def fields_get(self, model: str, attributes: list[str] | None = None) -> dict:
        return self.execute_kw(
            model, "fields_get", [], {"attributes": attributes or ["string", "type"]}
        )
