"""Connectivity check: authenticate to Odoo and run a couple of read-only queries.

Run: cd odooHelpers && PYTHONPATH=src uv run python scripts/smoke_odoo.py
"""

import argparse

from inventorymgr.sources.odoo_client import OdooClient


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod", action="store_true", help="use the production Odoo profile")
    args = ap.parse_args()
    profile = "prod" if args.prod else "local"
    c = OdooClient(profile=profile)
    print("profile:", profile)
    print("server_version:", c.version().get("server_version"))
    print("authenticated uid:", c.uid)
    print("product.product count:", c.search_count("product.product", []))
    print("purchase.order count:", c.search_count("purchase.order", []))
    sample = c.search_read("product.product", [], ["display_name", "default_code"], limit=3)
    print("sample products:", sample)


if __name__ == "__main__":
    main()
