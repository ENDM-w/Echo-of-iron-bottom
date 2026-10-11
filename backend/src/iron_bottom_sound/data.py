from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .models import (
    FiringArc,
    GameOptions,
    GameState,
    GunMountState,
    HexCoord,
    MAP_COLUMNS,
    MAP_ROWS,
    MAX_MAP_COLUMNS,
    MAX_MAP_ROWS,
    Phase,
    PRINTED_MAP_COLUMNS,
    PRINTED_MAP_ROWS,
    ShipRecord,
    ShipState,
    Side,
    TorpedoLauncherState,
    WeaponMount,
)


ROOT = Path(__file__).resolve().parents[3]
STRUCTURED = ROOT / "resources" / "derived" / "structured"
_CUSTOM_SCENARIOS: dict[str, dict[str, Any]] = {}


def register_custom_scenario(definition: dict[str, Any]) -> None:
    """Register a validated local scenario for the running process."""
    scenario_id = str(definition["id"])
    _CUSTOM_SCENARIOS[scenario_id] = deepcopy(definition)


def unregister_custom_scenario(scenario_id: str) -> None:
    _CUSTOM_SCENARIOS.pop(scenario_id, None)


def read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def scenario_catalog() -> list[dict[str, Any]]:
    builtins = _builtin_scenario_catalog()
    custom = [
        {"id": item["id"], "title": item["title"], "turns": item["turns"], "status": "playable", "custom": True}
        for item in _CUSTOM_SCENARIOS.values()
    ]
    return deepcopy(builtins) + custom


@lru_cache(maxsize=1)
def _builtin_scenario_catalog() -> list[dict[str, Any]]:
    """Read the immutable built-in catalog once per worker process."""
    return read_yaml(STRUCTURED / "scenarios" / "catalog.yaml")["scenarios"]


def load_scenario(scenario_id: str) -> dict[str, Any]:
    if scenario_id in _CUSTOM_SCENARIOS:
        return deepcopy(_CUSTOM_SCENARIOS[scenario_id])
    entry = next((item for item in _builtin_scenario_catalog() if item["id"] == scenario_id), None)
    if entry is None:
        raise KeyError(f"Unknown scenario {scenario_id}")
    definition = entry.get("definition")
    if not definition:
        number = int(entry["number"])
        definition = f"scenario-{number:02d}.yaml"
    path = STRUCTURED / "scenarios" / str(definition)
    if not path.exists():
        raise KeyError(f"Scenario {scenario_id} is catalogued but not playable")
    return deepcopy(_load_builtin_scenario(path))


@lru_cache(maxsize=32)
def _load_builtin_scenario(path: Path) -> dict[str, Any]:
    """Cache parsed built-in scenario YAML while returning copies to callers."""
    return read_yaml(path)


@lru_cache(maxsize=1)
def _load_templates_cached() -> dict[str, dict[str, Any]]:
    return read_yaml(STRUCTURED / "ships" / "templates.yaml")["templates"]


def load_templates() -> dict[str, dict[str, Any]]:
    return deepcopy(_load_templates_cached())


def _broadside_firepower(record: ShipRecord, kind: str) -> int:
    return max(
        sum(mount.firepower for mount in record.guns if mount.kind == kind and arc in mount.arcs)
        for arc in FiringArc
    )


def make_ship(
    entry: dict[str, Any], templates: dict[str, dict[str, Any]], records: dict[str, ShipRecord] | None = None
) -> ShipState:
    record = (records or {}).get(entry["id"])
    if record:
        primary_mounts = [mount for mount in record.guns if mount.kind == "primary"]
        secondary_mounts = [mount for mount in record.guns if mount.kind == "secondary"]
        data = {
            "type": record.ship_type,
            "displacement_band": record.displacement_band,
            "hull": record.hull_boxes,
            "speed_track": record.maximum_speed_cycle,
            "speed_damage_track": record.speed_damage_track,
            "primary_gf": _broadside_firepower(record, "primary"),
            "primary_caliber": primary_mounts[0].caliber,
            "secondary_gf": _broadside_firepower(record, "secondary") if secondary_mounts else 0,
            "secondary_caliber": secondary_mounts[0].caliber if secondary_mounts else 0,
            "torpedoes": sum(launcher.torpedoes for launcher in record.torpedo_launchers),
            "torpedo_type": record.torpedo_type,
            "belt_armor": record.armour.belt or 0,
            "primary_armor": record.armour.primary or 0,
            "secondary_armor": record.armour.secondary or 0,
            "bridge_armor": record.armour.bridge or 0,
            "aircraft": record.aircraft,
            "radar": record.radar,
            "vp": record.vp,
        }
    else:
        data = deepcopy(templates[entry["template"]])
        data.setdefault("displacement_band", "A" if data["type"] in {"DD", "APD"} else "C")
        data.setdefault(
            "speed_damage_track",
            tuple(tuple(range(speed, 0, -1)) for speed in data["speed_track"]),
        )
    primary = WeaponMount(kind="primary", firepower=data.get("primary_gf", 0), caliber=data.get("primary_caliber", 0))
    secondary = None
    if data.get("secondary_gf", 0):
        secondary = WeaponMount(kind="secondary", firepower=data["secondary_gf"], caliber=data.get("secondary_caliber", 5))
    torpedo = None
    if data.get("torpedoes", 0):
        torpedo = WeaponMount(kind="torpedo", ammo=data["torpedoes"])
    return ShipState(
        id=entry["id"],
        name=entry["name"],
        side=Side(entry["side"]),
        ship_type=data["type"],
        displacement_band=data["displacement_band"],
        position=HexCoord.from_label(entry["position"]) if entry.get("position") else None,
        heading=entry.get("heading", 1),
        speed_track=tuple(data["speed_track"]),
        speed_damage_track=tuple(tuple(row) for row in data["speed_damage_track"]),
        initial_max_speed=max(data["speed_track"]),
        current_speed=entry.get("speed", 0),
        previous_speed=entry.get("speed", 0),
        hull=data["hull"],
        max_hull=data["hull"],
        primary=primary,
        secondary=secondary,
        torpedo=torpedo,
        torpedo_type=data.get("torpedo_type"),
        belt_armor=data.get("belt_armor", 0),
        primary_armor=data.get("primary_armor", 0),
        secondary_armor=data.get("secondary_armor", 0),
        bridge_armor=data.get("bridge_armor", 0),
        aircraft=data.get("aircraft", False),
        radar=data.get("radar", False),
        gun_mounts=[GunMountState.model_validate(mount.model_dump()) for mount in record.guns] if record else [],
        torpedo_launchers=[
            TorpedoLauncherState(
                **launcher.model_dump(),
                loaded=launcher.torpedoes,
                reloads_remaining=entry.get("torpedo_reloads", launcher.reloads),
            )
            for launcher in record.torpedo_launchers
        ] if record else [],
        vp=data.get("vp", 0),
        asset=entry.get("asset"),
        reinforcement_turn=entry.get("reinforcement_turn"),
    )


def _scenario_map_dims(scenario: dict[str, Any]) -> tuple[int, int, int, int]:
    """Resolve the scenario's playable map + printed region (defaults match the
    fixed IBS-R-MAP-01 46×39 board so every legacy scenario is byte-identical)."""
    from .models import INFINITE_MAP_START_COLUMNS, INFINITE_MAP_START_ROWS

    default_columns = INFINITE_MAP_START_COLUMNS if scenario.get("map_infinite") else MAP_COLUMNS
    default_rows = INFINITE_MAP_START_ROWS if scenario.get("map_infinite") else MAP_ROWS
    columns = int(scenario.get("map_columns", default_columns))
    rows = int(scenario.get("map_rows", default_rows))
    if not (0 < columns <= MAX_MAP_COLUMNS and 0 < rows <= MAX_MAP_ROWS):
        raise ValueError(f"Invalid scenario map size {columns}x{rows}")
    printed_columns = scenario.get("printed_columns")
    printed_rows = scenario.get("printed_rows")
    if printed_columns is None:
        printed_columns = PRINTED_MAP_COLUMNS if columns == MAP_COLUMNS and not scenario.get("map_infinite") else columns
    if printed_rows is None:
        printed_rows = PRINTED_MAP_ROWS if rows == MAP_ROWS and not scenario.get("map_infinite") else rows
    if printed_columns > columns or printed_rows > rows:
        raise ValueError(f"Printed region {printed_columns}x{printed_rows} exceeds the playable map")
    return columns, rows, printed_columns, printed_rows


def build_initial_state(game_id: str, scenario_id: str, seed: int, options: GameOptions) -> GameState:
    from .ship_records import load_ship_records

    scenario = load_scenario(scenario_id)
    templates = load_templates()
    records = load_ship_records()
    entries = list(scenario["ships"])
    reinforcement = scenario.get("reinforcements")
    if reinforcement:
        arrival_turn = int(reinforcement["arrival"]["turn"])
        entries.extend({**entry, "reinforcement_turn": arrival_turn} for entry in reinforcement["ships"])
    ships = {entry["id"]: make_ship(entry, templates, records) for entry in entries}
    for key in scenario.get("optional_rules", []):
        setattr(options.optional_rules, key, True)
    map_columns, map_rows, printed_columns, printed_rows = _scenario_map_dims(scenario)
    initial_phase = Phase(scenario.get("initial_phase", Phase.REINFORCEMENT.value))
    if initial_phase == Phase.CONTACT_SETUP:
        initial_phase = Phase.REINFORCEMENT
    state = GameState(
        game_id=game_id,
        scenario_id=scenario_id,
        scenario_title=scenario["title"],
        max_turns=scenario["turns"],
        phase=initial_phase,
        seed=seed,
        options=options,
        visibility=scenario["visibility"],
        ships=ships,
        map_columns=map_columns,
        map_rows=map_rows,
        printed_columns=printed_columns,
        printed_rows=printed_rows,
        map_infinite=bool(scenario.get("map_infinite", False)),
    )
    if reinforcement:
        state.reinforcement_trigger_turn = int(reinforcement["trigger"]["turn"])
        state.reinforcement_arrival_turn = int(reinforcement["arrival"]["turn"])
        state.reinforcement_succeeds_on = tuple(int(value) for value in reinforcement["trigger"]["succeeds_on"])
        start, end = reinforcement["arrival"]["entry_hex_range"]
        state.reinforcement_entry_start = HexCoord.from_label(start)
        state.reinforcement_entry_end = HexCoord.from_label(end)
    if options.realistic_command:
        from .realistic_command import SUPPORTED_SCENARIOS
        if scenario_id not in SUPPORTED_SCENARIOS and scenario_id not in _CUSTOM_SCENARIOS:
            raise ValueError(f"Realistic command is not available for scenario {scenario_id}")
        state.formation_resume_phase = state.phase
        state.phase = Phase.FORMATION_SETUP
    return state
