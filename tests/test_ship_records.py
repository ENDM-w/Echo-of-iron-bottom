from pathlib import Path
import sqlite3

import yaml

from iron_bottom_sound.engine import IronBottomEngine
from iron_bottom_sound.counter_assets import all_asset_bindings, validate_asset_bindings
from iron_bottom_sound.data import make_ship
from iron_bottom_sound.models import FiringArc
from iron_bottom_sound.ship_records import load_ship_catalog, load_ship_records
from iron_bottom_sound.storage import GameRepository


ROOT = Path(__file__).resolve().parents[1]
STRUCTURED = ROOT / "resources" / "derived" / "structured"


def test_repository_migrates_imported_ships_to_allow_missing_artwork(tmp_path: Path) -> None:
    db_path = tmp_path / "ships.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """CREATE TABLE imported_ship_records (
             ship_id TEXT PRIMARY KEY, catalog_json TEXT NOT NULL, record_json TEXT NOT NULL,
             asset_mime TEXT NOT NULL, asset_data BLOB NOT NULL,
             imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
    )
    connection.execute(
        "INSERT INTO imported_ship_records (ship_id, catalog_json, record_json, asset_mime, asset_data) "
        "VALUES ('existing', '{}', '{}', 'image/png', ?) ",
        (sqlite3.Binary(b"existing-art"),),
    )
    connection.commit()
    connection.close()

    repository = GameRepository(db_path)
    try:
        constraints = {
            row[1]: row[3]
            for row in repository.connection.execute("PRAGMA table_info(imported_ship_records)")
        }
        assert constraints["asset_mime"] == constraints["asset_data"] == 0
        assert repository.connection.execute(
            "SELECT asset_data FROM imported_ship_records WHERE ship_id = 'existing'"
        ).fetchone()[0] == b"existing-art"
        repository.connection.execute(
            "INSERT INTO imported_ship_records "
            "(ship_id, catalog_json, record_json, asset_mime, asset_data) VALUES ('blank', '{}', '{}', NULL, NULL)"
        )
        repository.connection.commit()
    finally:
        repository.connection.close()


def test_all_playable_and_reinforcement_ships_have_verified_records() -> None:
    records = load_ship_records()
    required: set[str] = set()
    for number in (1, 3):
        scenario = yaml.safe_load((STRUCTURED / "scenarios" / f"scenario-{number:02d}.yaml").read_text(encoding="utf-8"))
        required.update(ship["id"] for ship in scenario["ships"])
        required.update(ship["id"] for ship in scenario.get("reinforcements", {}).get("ships", []))
    assert len(records) == 180
    assert required <= set(records)
    assert all(1 <= record.source_page <= 24 for record in records.values())


def test_complete_ship_list_catalog_is_migrated_without_promoting_unverified_stats() -> None:
    catalog = load_ship_catalog()
    assert len(catalog) == 204
    assert len(catalog) == len(set(catalog))
    assert {entry["ship_type"] for entry in catalog.values()} >= {"BB", "BC", "CA", "CL", "DD", "APD", "AV"}
    assert sum(1 for entry in catalog.values() if entry.get("unresolved")) == 4


def test_every_complete_record_has_a_verified_counter_asset_binding() -> None:
    records = load_ship_records()
    bindings = all_asset_bindings(records)
    assert len(bindings) == len(records) == 180
    assert not validate_asset_bindings(records)


def test_ship_record_mounts_and_tracks_match_source_examples() -> None:
    records = load_ship_records()
    aoba = records["IBS-U-IJN-AOBA"]
    helena = records["IBS-U-USN-HELENA"]
    akizuki = records["IBS-U-IJN-AKIZUKI"]
    karl = records["IBS-U-KM-KARL-GALSTER"]
    javelin = records["IBS-U-RN-JAVELIN"]
    washington = records["IBS-U-USN-WASHINGTON"]
    south_dakota = records["IBS-U-USN-SOUTH-DAKOTA"]
    assert aoba.hull_boxes == 11
    assert aoba.maximum_speed_cycle == (6, 5, 5)
    assert sum(mount.firepower for mount in aoba.guns if mount.kind == "primary") == 16
    assert aoba.torpedo_type == "jp-24-type93"
    assert [launcher.torpedoes for launcher in aoba.torpedo_launchers] == [2, 2]
    assert sum(launcher.reloads for launcher in aoba.torpedo_launchers) == 2
    assert helena.hull_boxes == 14
    assert helena.radar
    assert sum(mount.firepower for mount in helena.guns if mount.kind == "primary") == 32
    assert helena.armour.secondary == 1
    assert akizuki.hull_boxes == 7 and akizuki.maximum_speed_cycle == (6, 5, 5)
    assert [launcher.torpedoes for launcher in akizuki.torpedo_launchers] == [2]
    assert karl.hull_boxes == 6 and karl.maximum_speed_cycle == (6, 6, 6)
    assert [launcher.torpedoes for launcher in karl.torpedo_launchers] == [2, 2]
    assert javelin.hull_boxes == 5 and javelin.maximum_speed_cycle == (6, 6, 5)
    assert [launcher.torpedoes for launcher in javelin.torpedo_launchers] == [2, 2]
    assert [launcher.torpedoes for launcher in records["IBS-U-USN-FARENHOLT"].torpedo_launchers] == [2, 1]
    assert [launcher.torpedoes for launcher in records["IBS-U-IJN-FUBUKI"].torpedo_launchers] == [2, 2]
    assert [launcher.torpedoes for launcher in records["IBS-U-IJN-ASAGUMO"].torpedo_launchers] == [3, 3]
    assert sum(mount.kind == "primary" for mount in washington.guns) == 6
    assert sum(mount.firepower for mount in washington.guns if mount.kind == "primary") == 18
    washington_secondaries = [mount for mount in washington.guns if mount.kind == "secondary"]
    assert [(mount.id, mount.position, mount.firepower, mount.caliber, mount.arcs) for mount in washington_secondaries] == [
        ("S1", "port", 1, 5, (FiringArc.PORT,)),
        ("S2", "starboard", 1, 5, (FiringArc.STARBOARD,)),
    ]
    assert washington.armour.secondary == south_dakota.armour.secondary == 2

    ship = make_ship(
        {"id": "IBS-U-USN-WASHINGTON", "name": "华盛顿", "side": "allies", "position": "U40"},
        {},
        records,
    )
    assert ship.secondary is not None and ship.secondary.firepower == 1 and ship.secondary.caliber == 5
    assert [(mount.id, mount.firepower) for mount in ship.gun_mounts if mount.kind == "secondary"] == [
        ("S1", 1),
        ("S2", 1),
    ]


def test_every_mount_and_launcher_has_a_legal_source_arc() -> None:
    for record in load_ship_records().values():
        ids = [mount.id for mount in record.guns] + [launcher.id for launcher in record.torpedo_launchers]
        assert len(ids) == len(set(ids))
        assert all(mount.arcs for mount in record.guns)
        assert all(set(launcher.arcs) <= {FiringArc.PORT, FiringArc.STARBOARD} for launcher in record.torpedo_launchers)


def test_workbook_centerline_torpedo_launchers_are_not_split_by_firing_side() -> None:
    bundle = yaml.safe_load(
        (STRUCTURED / "ships" / "import-bundle.yaml").read_text(encoding="utf-8")
    )
    records = bundle["records"]
    both_sides = [FiringArc.PORT.value, FiringArc.STARBOARD.value]
    one_centerline_pair = {
        ship_id: [
            ("TT-M1", "midships", both_sides, 2),
            ("TT-M2", "midships", both_sides, 1),
        ]
        for ship_id in (
            "IBS-U-RN-FAULKNOR",
            "IBS-U-RN-FOXHOUND",
            "IBS-U-RN-FEARLESS",
            "IBS-U-RN-FORESIGHT",
            "IBS-U-RN-FORESTER",
            "IBS-U-RN-WRESTLER",
            "IBS-U-RN-VIDETTE",
            "IBS-U-RN-VORTIGERN",
            "IBS-U-FRN-LYNX",
            "IBS-U-FRN-TIGRE",
        )
    }
    one_centerline_and_two_side_mounts = {
        "IBS-U-FRN-LE-TRIOMPHANT": "凯旋",
        "IBS-U-FRN-L-INDOMPTABLE": "不屈",
    }
    for ship_id, expected in one_centerline_pair.items():
        record = records[ship_id]["record"]
        actual = [
            (item["id"], item["position"], item["arcs"], item["torpedoes"])
            for item in record["torpedo_launchers"]
        ]
        assert actual == expected
        assert sum(item["torpedoes"] for item in record["torpedo_launchers"]) == 3

    for ship_id, name in one_centerline_and_two_side_mounts.items():
        record = records[ship_id]["record"]
        assert record["name"] == name
        assert [
            (item["id"], item["position"], item["arcs"], item["torpedoes"])
            for item in record["torpedo_launchers"]
        ] == [
            ("TT-M1", "midships", both_sides, 1),
            ("TT-M2-P1", "port", [FiringArc.PORT.value], 2),
            ("TT-M2-S1", "starboard", [FiringArc.STARBOARD.value], 2),
        ]
        assert sum(item["torpedoes"] for item in record["torpedo_launchers"]) == 5


def test_torpedo_characteristics_table_matches_source_cells() -> None:
    data = yaml.safe_load((STRUCTURED / "rules" / "torpedoes.yaml").read_text(encoding="utf-8"))
    types = data["types"]
    assert len(types) == 12
    assert types["us-21-mk15-1942"]["settings"][-1] == {"speed": [5, 4, 4], "range": 25}
    assert types["jp-24-type90"]["settings"] == [
        {"speed": [8, 7, 7], "range": 12},
        {"speed": [7, 7, 7], "range": 18},
        {"speed": [6, 6, 6], "range": 27},
    ]
    assert types["jp-24-type93"]["damage_modifier"] == 1
    assert types["jp-21"]["damage_modifier"] == -1
    assert types["de-nl-21"]["settings"][1] == {"speed": [7, 7, 7], "range": 14}
    assert types["fr-21-6"] == {
        "nation": "FR",
        "caliber_in": 21.6,
        "settings": [
            {"speed": [7, 7, 6], "range": 18},
            {"speed": [6, 6, 5], "range": 26},
        ],
        "damage_modifier": 0,
    }
    assert types["it-21"] == {
        "nation": "IT",
        "caliber_in": 21,
        "settings": [
            {"speed": [9, 9, 9], "range": 8},
            {"speed": [5, 5, 5], "range": 24},
        ],
        "damage_modifier": 0,
    }
    referenced = {record.torpedo_type for record in load_ship_records().values() if record.torpedo_type}
    assert referenced <= set(types)


def test_engine_initial_state_uses_individual_records_not_generic_templates() -> None:
    scenario_one = IronBottomEngine().reset("IBS-S-01", seed=11)
    assert scenario_one.ships["IBS-U-IJN-AOBA"].max_hull == 11
    assert scenario_one.ships["IBS-U-USN-SAN-FRANCISCO"].max_hull == 12
    assert scenario_one.ships["IBS-U-USN-BOISE"].max_hull == 13
    assert scenario_one.ships["IBS-U-USN-HELENA"].max_hull == 14
    assert scenario_one.ships["IBS-U-USN-HELENA"].primary.firepower == 32
    scenario_three = IronBottomEngine().reset("IBS-S-03", seed=11)
    assert scenario_three.ships["IBS-U-KM-KARL-GALSTER"].max_hull == 6
    assert scenario_three.ships["IBS-U-RN-JAVELIN"].max_hull == 5
