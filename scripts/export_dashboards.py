#!/usr/bin/env python3
"""Export the live Grafana dashboards back into the provisioning JSON files.

Grafana UI edits live in Grafana's DB, not in the provisioned files. Run this after
tweaking dashboards in the UI to capture that state into version control, so another
deployment looks identical.

Usage:
    python3 scripts/export_dashboards.py

Auth/URL from env (sensible defaults; source your .env first):
    GRAFANA_URL                 default http://localhost:3000
    GF_SECURITY_ADMIN_USER      default admin
    GF_SECURITY_ADMIN_PASSWORD  default admin
"""

import base64
import json
import os
import pathlib
import sys
import urllib.request

URL = os.environ.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
USER = os.environ.get("GF_SECURITY_ADMIN_USER", "admin")
PW = os.environ.get("GF_SECURITY_ADMIN_PASSWORD", "admin")

# Stable uid -> provisioning file mapping (keeps filenames stable across exports).
MAPPING = {
    "skalna-overview": "grafana/dashboards/01_overview.json",
    "skalna-science": "grafana/dashboards/02_spring_science.json",
    "skalna-health": "grafana/dashboards/03_station_health.json",
}

ROOT = pathlib.Path(__file__).resolve().parent.parent
_hdr = {"Authorization": "Basic " + base64.b64encode(f"{USER}:{PW}".encode()).decode()}


def _get(path):
    req = urllib.request.Request(f"{URL}{path}", headers=_hdr)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def main():
    rc = 0
    for uid, rel in MAPPING.items():
        try:
            dash = _get(f"/api/dashboards/uid/{uid}")["dashboard"]
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {uid}: {e}", file=sys.stderr)
            rc = 1
            continue
        # Drop DB id + version so files are portable and deterministic (otherwise
        # re-provisioning bumps the version and every export produces a noisy diff).
        dash["id"] = None
        dash.pop("version", None)
        out = ROOT / rel
        with out.open("w", encoding="utf-8") as f:
            json.dump(dash, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"  saved {uid} -> {rel}  (version={dash.get('version')}, "
              f"panels={len(dash.get('panels', []))})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
