from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sqlite3
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from iron_bottom_sound.ship_imports import ShipImportEnvelope, ShipImportRequest  # noqa: E402
from iron_bottom_sound.storage import GameRepository  # noqa: E402


def load_bundle(bundle_path: Path):
    document = yaml.safe_load(bundle_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("Unsupported ship import bundle")
    entries = document.get("records")
    if not isinstance(entries, dict) or not entries:
        raise ValueError("The bundle must contain a non-empty records mapping")
    validated = []
    seen: set[str] = set()
    for ship_id, entry in entries.items():
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid bundle entry for {ship_id}")
        if ship_id in seen:
            raise ValueError(f"Duplicate ship id {ship_id}")
        seen.add(ship_id)
        request = ShipImportRequest.model_validate({
            "catalog": entry["catalog"],
            "record": entry["record"],
        })
        if request.record.id != ship_id:
            raise ValueError(f"Bundle key does not match record id: {ship_id}")
        artwork = (ROOT / entry["artwork"]).resolve()
        if not artwork.is_relative_to(ROOT):
            raise ValueError(f"Artwork path escapes repository root: {entry['artwork']}")
        if not artwork.is_file():
            raise FileNotFoundError(artwork)
        envelope = ShipImportEnvelope(
            definition=request,
            asset_image_base64=base64.b64encode(artwork.read_bytes()).decode("ascii"),
        )
        mime, asset = envelope.image_payload()
        validated.append((ship_id, request.catalog_payload(), request.record_payload(), mime, asset))
    return document, validated


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or seed the local SQLite ship-record table")
    parser.add_argument(
        "--bundle",
        type=Path,
        default=ROOT / "resources/derived/structured/ships/import-bundle.yaml",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=ROOT / "backend/iron-bottom-sound.sqlite3",
        help="local runtime database; this file is gitignored",
    )
    parser.add_argument("--check-only", action="store_true", help="validate records and artwork without writing SQLite")
    args = parser.parse_args()
    bundle_path = args.bundle.resolve()
    if not bundle_path.is_relative_to(ROOT):
        raise ValueError("Bundle path must be inside the repository")
    document, ships = load_bundle(bundle_path)
    print(f"Validated {len(ships)} ship records from {document.get('source', {}).get('description', bundle_path.name)}")
    if args.check_only:
        print("Check only; SQLite was not modified.")
        return 0

    db_path = args.db.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Reuse the repository initializer to apply the same table shape as the app.
    repository = GameRepository(db_path)
    try:
        with repository.connection:
            for ship_id, catalog, record, mime, asset in ships:
                repository.connection.execute(
                    """INSERT INTO imported_ship_records
                       (ship_id, catalog_json, record_json, asset_mime, asset_data)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(ship_id) DO UPDATE SET
                         catalog_json=excluded.catalog_json,
                         record_json=excluded.record_json,
                         asset_mime=excluded.asset_mime,
                         asset_data=excluded.asset_data,
                         imported_at=CURRENT_TIMESTAMP""",
                    (
                        ship_id,
                        json.dumps(catalog, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                        mime,
                        sqlite3.Binary(asset),
                    ),
                )
    finally:
        repository.connection.close()
    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM imported_ship_records").fetchone()[0]
        check = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if check != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {check}")
    print(f"Imported {len(ships)} ships; local imported_ship_records now has {count} rows; SQLite integrity: {check}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
