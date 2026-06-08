"""Connectivity check: authenticate to Odoo and run a couple of read-only queries.

Run: cd inventorymgr && PYTHONPATH=src uv run python scripts/smoke_odoo.py
"""

from inventorymgr.sources.odoo_client import OdooClient


def main() -> None:
    c = OdooClient()
    print("server_version:", c.version().get("server_version"))
    print("authenticated uid:", c.uid)
    print("product.product count:", c.search_count("product.product", []))
    print("purchase.order count:", c.search_count("purchase.order", []))
    sample = c.search_read("product.product", [], ["display_name", "default_code"], limit=3)
    print("sample products:", sample)


if __name__ == "__main__":
    main()
