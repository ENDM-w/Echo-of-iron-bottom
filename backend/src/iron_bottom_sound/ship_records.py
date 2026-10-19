from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from .data import STRUCTURED, read_yaml
from .models import ShipRecord


def _imported_ship_rows() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    default_db = Path(__file__).resolve().parents[3] / "backend" / "iron-bottom-sound.sqlite3"
    path = Path(os.environ.get("IBS_DB_PATH", str(default_db)))
    if not path.exists():
        return []
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='imported_ship_records'"
        ).fetchone()
        if table is None:
            return []
        return [
            (str(row[0]), json.loads(row[1]), json.loads(row[2]))
            for row in connection.execute(
                "SELECT ship_id, catalog_json, record_json FROM imported_ship_records ORDER BY ship_id"
            )
        ]
    finally:
        connection.close()


def _open_imported_db():
    default_db = Path(__file__).resolve().parents[3] / "backend" / "iron-bottom-sound.sqlite3"
    path = Path(os.environ.get("IBS_DB_PATH", str(default_db)))
    if not path.exists():
        return None
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


@lru_cache(maxsize=1)
def _imported_asset_ids_cached() -> frozenset[str]:
    connection = _open_imported_db()
    if connection is None:
        return frozenset()
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(imported_ship_records)")}
        if not {"ship_id", "asset_data"}.issubset(columns):
            return frozenset()
        return frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT ship_id FROM imported_ship_records WHERE asset_data IS NOT NULL"
            )
        )
    finally:
        connection.close()


def imported_ship_has_asset(ship_id: str) -> bool:
    return ship_id in _imported_asset_ids_cached()


def clear_imported_ship_asset_cache() -> None:
    _imported_asset_ids_cached.cache_clear()
    _load_ship_records_merged.cache_clear()


def load_imported_ship_asset(ship_id: str) -> tuple[str, bytes] | None:
    connection = _open_imported_db()
    if connection is None:
        return None
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(imported_ship_records)")}
        if not {"ship_id", "asset_mime", "asset_data"}.issubset(columns):
            return None
        row = connection.execute(
            "SELECT asset_mime, asset_data FROM imported_ship_records WHERE ship_id = ?",
            (ship_id,),
        ).fetchone()
        if row is None or row[1] is None:
            return None
        return str(row[0]), bytes(row[1])
    finally:
        connection.close()


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


@lru_cache(maxsize=1)
def _load_ship_records_cached() -> dict[str, ShipRecord]:
    paths = [STRUCTURED / "ships" / "ship-records.yaml"]
    extension_root = STRUCTURED / "ships" / "extensions"
    if extension_root.exists():
        paths.extend(sorted(extension_root.glob("*.yaml")))
    documents = [read_yaml(path) for path in paths]
    families: dict[str, dict[str, Any]] = {}
    for document in documents:
        overlap = set(families) & set(document.get("families", {}))
        if overlap:
            raise ValueError(f"Duplicate ship-record families: {sorted(overlap)}")
        families.update(document.get("families", {}))
    records: dict[str, ShipRecord] = {}
    for document in documents:
        for record_id, override in document.get("records", {}).items():
            if record_id in records:
                raise ValueError(f"Duplicate ship record {record_id}")
            family_id = override["family"]
            payload = _merge(families[family_id], {key: value for key, value in override.items() if key != "family"})
            payload["id"] = record_id
            records[record_id] = ShipRecord.model_validate(payload)
    return records


@lru_cache(maxsize=1)
def _load_ship_records_merged() -> dict[str, ShipRecord]:
    """静态名录 + 数据库导入记录的合并结果（按进程缓存）。"""
    records = _load_ship_records_cached().copy()
    for ship_id, _, payload in _imported_ship_rows():
        if ship_id in records:
            raise ValueError(f"Imported ship record collides with static ship record {ship_id}")
        records[ship_id] = ShipRecord.model_validate(payload)
    return records


def load_ship_records() -> dict[str, ShipRecord]:
    """Return verified static records plus referee-imported database records.

    结果按进程缓存：`/view` 等热路径每请求都会调用，冷启动需解析 102 条导入
    记录（369ms）。返回浅拷贝，调用方可安全增删，不会污染缓存。
    """
    return _load_ship_records_merged().copy()


def load_ship_catalog() -> dict[str, dict[str, Any]]:
    """Load the complete name/class catalog without making unverified stats playable.

    The catalog is deliberately separate from ``load_ship_records``: the ship-list
    document identifies ships, while only manually verified record sheets may
    provide combat data to the engine.
    """
    document = read_yaml(STRUCTURED / "ships" / "catalog.yaml")
    catalog = document.get("records", {})
    if not isinstance(catalog, dict):
        raise ValueError("Ship catalog records must be a mapping")
    required = {"name", "english_name", "class_name", "ship_type", "source_table", "source_row"}
    for record_id, entry in catalog.items():
        if not record_id.startswith("IBS-U-"):
            raise ValueError(f"Invalid ship catalog id {record_id}")
        missing = required - set(entry)
        if missing:
            raise ValueError(f"Ship catalog entry {record_id} missing {sorted(missing)}")
    for ship_id, imported_catalog, _ in _imported_ship_rows():
        if ship_id in catalog:
            raise ValueError(f"Imported ship catalog collides with static ship {ship_id}")
        catalog[ship_id] = imported_catalog
    return catalog
