from __future__ import annotations

from copy import deepcopy
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iron_bottom_sound.engine import IronBottomEngine
from iron_bottom_sound.data import load_scenario, register_custom_scenario, unregister_custom_scenario
from iron_bottom_sound.custom_scenarios import with_engine_default_formations
from iron_bottom_sound.models import DiceRoll, GunMountOrder, GunneryOrder, HexCoord, IlluminationOrder, MarkerState, MovementOrder, OrderBatch, Phase, SearchlightOrder, Side, TorpedoOrder, TorpedoTrack, WreckState
from iron_bottom_sound.platform.auth import PasswordHasher
from iron_bottom_sound.platform.models import (
    ControllerType,
    JudgeOrderOverrideRequest,
    JudgeRerollRequest,
    JudgeShipPatchRequest,
    JudgeVisibilityPatchRequest,
    MatchStage,
    SeatOrderSubmission,
    TelegramSubmission,
)
from iron_bottom_sound.platform.service import PlatformService
from iron_bottom_sound.platform.repository import PlatformRepository
from iron_bottom_sound.platform.router import build_router
from iron_bottom_sound.storage import GameRepository


def make_service(tmp_path):
    database = tmp_path / "platform.sqlite3"
    engine = IronBottomEngine()
    service = PlatformService(engine, GameRepository(database))
    judge = service.create_user("judge", "judge-password", is_referee=True)
    axis = service.create_user("axis-player", "axis-password")
    allies = service.create_user("allies-player", "allies-password")
    match = service.create_match("IBS-S-03", 7, judge.user_id)
    state = engine.get(match.game_id)
    axis_ship = next(ship for ship in state.ships.values() if ship.side == Side.AXIS and ship.position)
    allies_ship = next(ship for ship in state.ships.values() if ship.side == Side.ALLIES and ship.position)
    axis_seat = service.add_seat(
        match.match_id,
        user_id=axis.user_id,
        side=Side.AXIS,
        controlled_ship_ids=[axis_ship.id],
    )
    allies_seat = service.add_seat(
        match.match_id,
        user_id=allies.user_id,
        side=Side.ALLIES,
        controlled_ship_ids=[allies_ship.id],
    )
    proxy_seats = []
    for ship in state.ships.values():
        if ship.id in {axis_ship.id, allies_ship.id}:
            continue
        proxy_seats.append(
            service.add_seat(
                match.match_id,
                user_id=None,
                side=ship.side,
                controlled_ship_ids=[ship.id],
                controller_type=ControllerType.REFEREE_PROXY,
            )
        )
    return service, engine, judge, axis, allies, match, axis_seat, allies_seat, proxy_seats, axis_ship, allies_ship


def test_password_hash_is_not_plaintext_and_verifies() -> None:
    encoded = PasswordHasher.hash("correct horse battery staple")
    assert encoded != "correct horse battery staple"
    assert PasswordHasher.verify("correct horse battery staple", encoded)
    assert not PasswordHasher.verify("wrong", encoded)


def test_custom_formation_scenario_starts_online_match_in_formation_setup(tmp_path) -> None:
    service, engine, judge, *_ = make_service(tmp_path)
    scenario_id = "IBS-CUSTOM-PLATFORM-FORMATION-TEST"
    definition = deepcopy(load_scenario("IBS-S-03"))
    definition["id"] = scenario_id
    definition["formations"] = {}
    for side in (Side.AXIS.value, Side.ALLIES.value):
        ship_ids = [ship["id"] for ship in definition["ships"] if ship["side"] == side]
        definition["formations"][side] = [{
            "formation_id": f"{side}-test-column",
            "name": f"{side} test column",
            "ship_ids": ship_ids,
            "leader_id": ship_ids[0],
            "flagship_id": ship_ids[0],
            "reserve_flagship_id": ship_ids[1],
            "spacing": 1,
            "heading": 1,
        }]
    definition = with_engine_default_formations(definition)
    register_custom_scenario(definition)
    service.game_repository.save_custom_scenario(
        scenario_id,
        json.loads(json.dumps(definition, default=str)),
    )
    try:
        match = service.create_match(scenario_id, 11, judge.user_id)
        state = engine.get(match.game_id)
        assert state.options.realistic_command
        assert state.phase == Phase.FORMATION_SETUP
        assert state.formation_resume_phase == Phase.REINFORCEMENT
    finally:
        unregister_custom_scenario(scenario_id)


def test_seat_view_includes_other_friendly_ships_as_anonymous_contacts(tmp_path) -> None:
    service, engine, _, axis, _, match, axis_seat, _, _, axis_ship, _ = make_service(tmp_path)
    token, _ = service.issue_token("axis-player", "axis-password")
    user = service.authenticate(token)
    assert user
    view = service.observation(match.match_id, axis_seat.seat_id, user)
    assert view.own_ship_ids == [axis_ship.id]
    other_axis = [
        ship for ship in engine.get(match.game_id).ships.values()
        if ship.side == Side.AXIS and ship.id != axis_ship.id and ship.position
    ]
    assert axis_ship.position
    near_friend, distant_friend = other_axis[:2]
    near_friend.position = HexCoord(q=axis_ship.position.q + 1, r=axis_ship.position.r)
    distant_friend.position = HexCoord(q=axis_ship.position.q + 12, r=axis_ship.position.r + 12)
    view = service.observation(match.match_id, axis_seat.seat_id, user)
    contact = next(ship for ship in view.ships if ship.name[:2] in {"驱逐", "巡洋", "战列"} and ship.id != axis_ship.id)
    assert contact.name[:2] in {"驱逐", "巡洋", "战列"}
    assert contact.id == contact.name
    assert contact.side == Side.AXIS
    assert contact.name != near_friend.name
    assert contact.asset in {"DD标记.png", "CA-CL标记.png", "BB标记.png"}
    assert contact.ship_type in {"destroyer", "cruiser", "battleship"}
    assert near_friend.id not in view.model_dump_json()
    assert not view.visible_enemy_ship_ids
    assert distant_friend.id not in {ship.id for ship in view.ships}


def test_seat_field_of_fire_preview_is_limited_to_controlled_ship(tmp_path) -> None:
    service, engine, _, axis, _, match, axis_seat, _, _, axis_ship, allies_ship = make_service(tmp_path)
    token, _ = service.issue_token("axis-player", "axis-password")
    user = service.authenticate(token)
    assert user

    preview = service.seat_field_of_fire(match.match_id, axis_seat.seat_id, axis_ship.id, user)
    assert preview["viewer"] == Side.AXIS.value
    assert preview["sides"][Side.AXIS.value]["ships"][0]["ship_id"] == axis_ship.id
    assert preview["sides"][Side.ALLIES.value]["ships"] == []
    assert allies_ship.id not in str(preview)

    another_axis = next(
        ship for ship in engine.get(match.game_id).ships.values()
        if ship.side == Side.AXIS and ship.position and ship.id != axis_ship.id
    )
    with pytest.raises(PermissionError, match="本席位控制"):
        service.seat_field_of_fire(match.match_id, axis_seat.seat_id, another_axis.id, user)
    with pytest.raises(PermissionError):
        service.seat_field_of_fire(match.match_id, axis_seat.seat_id, allies_ship.id, user)


def test_seat_gunnery_candidates_are_scoped_and_use_public_contact_ids(tmp_path) -> None:
    service, engine, _, _, _, match, axis_seat, _, _, axis_ship, _ = make_service(tmp_path)
    token, _ = service.issue_token("axis-player", "axis-password")
    user = service.authenticate(token)
    assert user
    view = service.observation(match.match_id, axis_seat.seat_id, user)
    public_ids = {ship.id for ship in view.ships}
    assert view.gunnery_candidates
    for candidate in view.gunnery_candidates:
        assert candidate["ship_id"] in axis_seat.controlled_ship_ids
        for target in candidate["targets"]:
            assert target["target_id"] in public_ids
            assert target["target_id"] not in {
                ship_id for ship_id in engine.get(match.game_id).ships
                if ship_id not in axis_seat.controlled_ship_ids
            }
            assert set(target["mount_ids"]) <= {
                mount.id for mount in engine.get(match.game_id).ships[axis_ship.id].gun_mounts
            }


def test_player_view_confirms_only_its_own_submission_and_clears_after_rejection(tmp_path) -> None:
    service, _, _, _, _, match, axis_seat, allies_seat, _, _, _ = make_service(tmp_path)
    axis_user = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    allies_user = service.authenticate(service.issue_token("allies-player", "allies-password")[0])
    judge_user = service.authenticate(service.issue_token("judge", "judge-password")[0])
    assert axis_user and allies_user and judge_user

    assert not service.observation(match.match_id, axis_seat.seat_id, axis_user).seat_has_submitted
    service.submit_order(
        match.match_id,
        axis_seat.seat_id,
        axis_user,
        SeatOrderSubmission(
            phase=match.phase,
            batch=OrderBatch(side=Side.AXIS, phase=match.phase),
            request_id="own-seat-order",
        ),
    )
    own_view = service.observation(match.match_id, axis_seat.seat_id, axis_user)
    other_view = service.observation(match.match_id, allies_seat.seat_id, allies_user)
    assert own_view.seat_has_submitted
    assert not other_view.seat_has_submitted

    service.reject(match.match_id, judge_user, service.summary(match.match_id).match.version, "请修改后重交")
    assert not service.observation(match.match_id, axis_seat.seat_id, axis_user).seat_has_submitted


@pytest.mark.parametrize("hidden_contacts", [False, True])
def test_radar_contact_has_anonymous_counter_only_in_ordinary_mode(tmp_path, hidden_contacts) -> None:
    service, engine, _, _, _, match, axis_seat, _, _, axis_ship, _ = make_service(tmp_path)
    state = engine.get(match.game_id)
    state.visibility[Side.AXIS.value] = 4
    state.options.optional_rules.hidden_contacts = hidden_contacts
    state.options.optional_rules.radar = True
    state.options.optional_rules.squalls = False
    state.options.optional_rules.silhouettes = False
    axis_ship.position = HexCoord(q=5, r=5)
    axis_ship.radar = True
    signal_ship = next(
        ship for ship in state.ships.values()
        if ship.side == Side.ALLIES and ship.id != axis_ship.id
    )
    signal_ship.position = HexCoord(q=14, r=5)

    token, _ = service.issue_token("axis-player", "axis-password")
    user = service.authenticate(token)
    assert user
    view = service.observation(match.match_id, axis_seat.seat_id, user)

    contacts = [ship for ship in view.ships if ship.position == signal_ship.position]
    if hidden_contacts:
        assert not contacts
        return
    contact, = contacts
    assert not contact.signal_only
    assert contact.asset in {"DD标记.png", "CA-CL标记.png", "BB标记.png"}
    assert contact.name.startswith(("驱逐", "巡洋", "战列"))
    assert signal_ship.id not in view.model_dump_json()
    assert service._resolve_contact_aliases(
        match.match_id,
        axis_seat.seat_id,
        OrderBatch(
            side=Side.AXIS,
            phase=Phase.GUNNERY,
            gunnery=[GunneryOrder(ship_id=axis_ship.id, primary_target=contact.id)],
        ),
    ).gunnery[0].primary_target == signal_ship.id


def test_seat_view_clips_previous_turn_contact_trajectory(tmp_path) -> None:
    service, engine, _, axis, _, match, axis_seat, _, _, axis_ship, _ = make_service(tmp_path)
    state = engine.get(match.game_id)
    friendly = next(
        ship for ship in state.ships.values()
        if ship.side == Side.AXIS and ship.id != axis_ship.id and ship.position
    )
    assert axis_ship.position and friendly.position
    visible_position = HexCoord(q=axis_ship.position.q + 1, r=axis_ship.position.r)
    hidden_position = HexCoord(q=axis_ship.position.q + 12, r=axis_ship.position.r + 12)
    friendly.position = visible_position
    state.turn = 2
    state.last_trajectory_turn = 1
    state.last_turn_trajectories = {
        friendly.id: [hidden_position, visible_position],
    }
    token, _ = service.issue_token("axis-player", "axis-password")
    user = service.authenticate(token)
    assert user
    view = service.observation(match.match_id, axis_seat.seat_id, user)
    contact = next(ship for ship in view.ships if ship.id in view.ship_trajectories)
    assert contact.id == contact.name
    assert view.ship_trajectories[contact.id] == [visible_position.label]


def test_seat_view_anonymizes_visible_enemy_identity(tmp_path) -> None:
    service, engine, _, _, _, match, axis_seat, _, _, axis_ship, allies_ship = make_service(tmp_path)
    assert axis_ship.position
    allies_ship.position = HexCoord(q=axis_ship.position.q + 1, r=axis_ship.position.r)
    token, _ = service.issue_token("axis-player", "axis-password")
    user = service.authenticate(token)
    assert user
    view = service.observation(match.match_id, axis_seat.seat_id, user)
    enemy = next(ship for ship in view.ships if ship.name[:2] in {"驱逐", "巡洋", "战列"})
    assert enemy.name[:2] in {"驱逐", "巡洋", "战列"}
    expected_assets = {"destroyer": "DD标记.png", "cruiser": "CA-CL标记.png", "battleship": "BB标记.png"}
    assert enemy.asset == expected_assets[enemy.ship_type]
    assert enemy.name != allies_ship.name


def test_hidden_contacts_uses_two_range_anonymous_signal_band(tmp_path) -> None:
    service, engine, _, _, _, match, axis_seat, _, _, axis_ship, allies_ship = make_service(tmp_path)
    state = engine.get(match.game_id)
    state.options.optional_rules.hidden_contacts = True
    state.visibility[Side.AXIS.value] = 5
    axis_ship.position = HexCoord(q=5, r=5)
    axis_ship.heading = 1
    for ship in state.ships.values():
        if ship.id != axis_ship.id:
            ship.position = HexCoord(q=30, r=30)

    friendly_at_radius = next(
        ship for ship in state.ships.values()
        if ship.side == Side.AXIS and ship.id != axis_ship.id
    )
    friendly_signal = next(
        ship for ship in state.ships.values()
        if ship.side == Side.AXIS and ship.id not in {axis_ship.id, friendly_at_radius.id}
    )
    beyond_light = next(
        ship for ship in state.ships.values()
        if ship.side == Side.ALLIES and ship.id != allies_ship.id
    )
    unlit_outer = next(
        ship for ship in state.ships.values()
        if ship.side == Side.ALLIES and ship.id not in {allies_ship.id, beyond_light.id}
    )
    state.options.optional_rules.radar = True
    friendly_at_radius.position = HexCoord(q=10, r=5)
    friendly_at_radius.radar = True
    friendly_signal.position = HexCoord(q=11, r=5)
    friendly_signal.fire_markers = 1
    friendly_signal.fired = True
    state.markers.append(MarkerState(
        id=f"GUNFIRE-{state.turn}-{friendly_signal.id}",
        kind="gunfire",
        position=HexCoord(q=10, r=5),
        ship_id=friendly_signal.id,
        expires_turn=state.turn + 1,
    ))
    allies_ship.position = HexCoord(q=15, r=5)
    beyond_light.position = HexCoord(q=16, r=5)
    unlit_outer.position = HexCoord(q=12, r=5)
    state.markers.extend([
        MarkerState(
            id=f"STAR-secret-{allies_ship.id}",
            kind="star_shell",
            position=allies_ship.position,
            expires_turn=state.turn,
            ship_id=beyond_light.id,
            secret_side=Side.ALLIES,
        ),
        MarkerState(
            id=f"SEARCH-secret-{beyond_light.id}",
            kind="searchlight",
            target_ship_id=beyond_light.id,
            expires_turn=state.turn,
            ship_id=allies_ship.id,
        ),
        MarkerState(
            id=f"SEARCH-visible-{allies_ship.id}",
            kind="searchlight",
            target_ship_id=allies_ship.id,
            expires_turn=state.turn,
            ship_id=axis_ship.id,
        ),
        MarkerState(id="SMOKE-visible", kind="smoke", position=friendly_signal.position),
        MarkerState(id="TORPEDO-HIT-visible", kind="torpedo_hit", position=friendly_signal.position),
        MarkerState(id="SUNK-visible", kind="sunk", position=friendly_signal.position),
    ])
    state.torpedo_tracks.append(TorpedoTrack(
        id="TORP-HIDDEN-TEST",
        side=Side.AXIS,
        launcher_ship_id=axis_ship.id,
        torpedo_type="Type 93",
        position=HexCoord(q=8, r=5),
        heading=1,
        speed_cycle=(1, 1, 1),
        range_remaining=4,
        launched_turn=state.turn,
        launch_position=axis_ship.position,
        traversed_hexes=[axis_ship.position, HexCoord(q=7, r=5), HexCoord(q=8, r=5)],
    ))
    state.wrecks.extend([
        WreckState(id=f"WRECK-{allies_ship.id}", position=HexCoord(q=14, r=5), source_ship_id=allies_ship.id),
        WreckState(id=f"WRECK-{beyond_light.id}", position=HexCoord(q=16, r=5), source_ship_id=beyond_light.id),
    ])

    token, _ = service.issue_token("axis-player", "axis-password")
    user = service.authenticate(token)
    assert user
    view = service.observation(match.match_id, axis_seat.seat_id, user)
    visible_ids = {ship.id for ship in view.ships}
    assert axis_ship.id in visible_ids
    assert friendly_at_radius.id not in visible_ids
    assert allies_ship.id not in visible_ids
    assert unlit_outer.id not in visible_ids
    assert not any(ship.position == HexCoord(q=12, r=5) for ship in view.ships)
    illuminated = next(ship for ship in view.ships if ship.position == HexCoord(q=15, r=5))
    assert illuminated.name[:2] in {"驱逐", "巡洋", "战列"}
    assert illuminated.signal_only is True
    assert illuminated.illuminated is True
    assert illuminated.asset in {"DD标记.png", "CA-CL标记.png", "BB标记.png"}
    assert illuminated.heading == allies_ship.heading
    assert illuminated.gun_mounts == []
    target_order = service._resolve_contact_aliases(
        match.match_id,
        axis_seat.seat_id,
        OrderBatch(
            side=Side.AXIS,
            phase=Phase.GUNNERY,
            gunnery=[GunneryOrder(ship_id=axis_ship.id, primary_target=illuminated.id)],
        ),
    )
    assert target_order.gunnery[0].primary_target == allies_ship.id
    assert not view.torpedo_tracks
    assert "TORP-HIDDEN-TEST" not in view.model_dump_json()
    assert beyond_light.id not in visible_ids

    signal_kinds = {marker.kind for marker in view.markers}
    assert {"fire", "gunfire", "star_shell", "searchlight", "smoke", "torpedo_hit", "sunk"}.issubset(signal_kinds)
    assert sum(
        marker.kind == "gunfire" and marker.position == friendly_signal.position
        for marker in view.markers
    ) == 1
    assert not any(
        marker.kind == "gunfire" and marker.position == HexCoord(q=10, r=5)
        for marker in view.markers
    )
    assert not any(marker.kind == "searchlight" and marker.position == beyond_light.position for marker in view.markers)
    assert all("IBS-U-" not in marker.id for marker in view.markers)
    assert all(marker.ship_id is None and marker.target_ship_id is None for marker in view.markers)
    assert len(view.wrecks) == 1
    serialized = view.model_dump_json()
    assert beyond_light.id not in serialized
    assert beyond_light.name not in serialized


def test_hidden_contact_star_shell_reveals_ship_type_within_two_hexes_only(tmp_path) -> None:
    service, engine, _, _, _, match, axis_seat, _, _, axis_ship, allies_ship = make_service(tmp_path)
    state = engine.get(match.game_id)
    state.options.optional_rules.hidden_contacts = True
    state.options.optional_rules.star_shells = True
    state.visibility[Side.AXIS.value] = 5
    axis_ship.position = HexCoord(q=5, r=5)
    axis_ship.heading = 1
    unlit = next(
        ship for ship in state.ships.values()
        if ship.side == Side.ALLIES and ship.id != allies_ship.id
    )
    for ship in state.ships.values():
        if ship.id not in {axis_ship.id, allies_ship.id, unlit.id}:
            ship.position = HexCoord(q=30, r=30)
    allies_ship.position = HexCoord(q=13, r=5)
    unlit.position = HexCoord(q=14, r=5)
    unlit.fire_markers = 1
    unlit.fired = True
    state.markers.append(MarkerState(
        id="STAR-R2-ILLUMINATION",
        kind="star_shell",
        position=HexCoord(q=11, r=5),
        expires_turn=state.turn,
        ship_id=axis_ship.id,
        secret_side=Side.AXIS,
    ))

    token, _ = service.issue_token("axis-player", "axis-password")
    user = service.authenticate(token)
    assert user
    view = service.observation(match.match_id, axis_seat.seat_id, user)
    illuminated = next(ship for ship in view.ships if ship.position == allies_ship.position)

    assert illuminated.name[:2] in {"驱逐", "巡洋", "战列"}
    assert illuminated.name != allies_ship.name
    assert illuminated.ship_type in {"destroyer", "cruiser", "battleship"}
    assert illuminated.signal_only is True
    assert illuminated.illuminated is True
    assert illuminated.asset in {"DD标记.png", "CA-CL标记.png", "BB标记.png"}
    signal_target = next(ship for ship in view.ships if ship.position == unlit.position)
    assert signal_target.signal_only is True
    assert signal_target.illuminated is False
    assert signal_target.ship_type == "unknown"
    assert signal_target.asset is None
    assert signal_target.name == "某标记"
    serialized = view.model_dump_json()
    assert unlit.id not in serialized
    assert unlit.name not in serialized
    target_order = service._resolve_contact_aliases(
        match.match_id,
        axis_seat.seat_id,
        OrderBatch(
            side=Side.AXIS,
            phase=Phase.GUNNERY,
            gunnery=[GunneryOrder(ship_id=axis_ship.id, primary_target=signal_target.id)],
        ),
    )
    assert target_order.gunnery[0].primary_target == unlit.id
    assert any(marker.kind == "star_shell" and marker.position == HexCoord(q=11, r=5) for marker in view.markers)


def test_hidden_contact_illumination_cannot_reveal_beyond_two_ranges(tmp_path) -> None:
    service, engine, _, _, _, match, axis_seat, _, _, axis_ship, allies_ship = make_service(tmp_path)
    state = engine.get(match.game_id)
    state.options.optional_rules.hidden_contacts = True
    state.visibility[Side.AXIS.value] = 4
    axis_ship.position = HexCoord(q=4, r=4)
    allies_ship.position = HexCoord(q=13, r=4)
    state.markers.append(MarkerState(
        id=f"SEARCH-{allies_ship.id}",
        kind="searchlight",
        target_ship_id=allies_ship.id,
        expires_turn=state.turn,
    ))
    token, _ = service.issue_token("axis-player", "axis-password")
    user = service.authenticate(token)
    assert user
    view = service.observation(match.match_id, axis_seat.seat_id, user)
    assert allies_ship.id not in {ship.id for ship in view.ships}
    assert not any(marker.kind == "searchlight" for marker in view.markers)
    assert allies_ship.id not in view.model_dump_json()


def test_legacy_contact_setup_migrates_to_scenario_positions(tmp_path) -> None:
    service, engine, _, _, _, match, axis_seat, _, _, axis_ship, _ = make_service(tmp_path)
    state = engine.get(match.game_id)
    original_positions = {ship.id: ship.position for ship in state.ships.values() if ship.position}
    state.options.optional_rules.hidden_contacts = True
    state.contact_reserve_positions = original_positions.copy()
    for ship in state.ships.values():
        if ship.position:
            ship.position = None
    state.markers.extend(
        MarkerState(
            id=f"CONTACT-{side.value}-{index}",
            kind="contact",
            secret_side=side,
            contact_truth="real" if index <= 2 else "decoy",
        )
        for side in Side
        for index in range(1, 5)
    )
    state.phase = Phase.CONTACT_SETUP
    match.phase = Phase.CONTACT_SETUP
    service.repository.save_match(match)

    token, _ = service.issue_token("axis-player", "axis-password")
    player = service.authenticate(token)
    assert player
    observation = service.observation(match.match_id, axis_seat.seat_id, player)

    assert state.phase == Phase.REINFORCEMENT
    assert service.repository.match(match.match_id).phase == Phase.REINFORCEMENT
    assert axis_ship.position == original_positions[axis_ship.id]
    assert state.contact_reserve_positions == {}
    assert not any(marker.kind == "contact" for marker in state.markers)
    assert axis_ship.id in observation.own_ship_ids
    assert "CONTACT-axis-1" not in observation.model_dump_json()


def test_seat_view_does_not_extend_to_another_friendly_ship_radar(tmp_path) -> None:
    service, engine, _, _, _, match, axis_seat, _, _, axis_ship, allies_ship = make_service(tmp_path)
    state = engine.get(match.game_id)
    state.options.optional_rules.radar = True
    axis_ship.radar = False
    friendly = next(
        ship
        for ship in state.ships.values()
        if ship.side == Side.AXIS and ship.id != axis_ship.id and ship.position
    )
    assert axis_ship.position and friendly.position and allies_ship.position
    axis_ship.position = HexCoord(q=0, r=0)
    friendly.position = HexCoord(q=20, r=20)
    allies_ship.position = HexCoord(q=20, r=21)
    friendly.radar = True
    token, _ = service.issue_token("axis-player", "axis-password")
    user = service.authenticate(token)
    assert user
    view = service.observation(match.match_id, axis_seat.seat_id, user)
    assert allies_ship.id not in {ship.id for ship in view.ships}


def test_referee_observation_includes_full_tactical_map(tmp_path) -> None:
    service, engine, judge, _, _, match, _, _, _, axis_ship, _ = make_service(tmp_path)
    state = engine.get(match.game_id)
    assert axis_ship.position
    expected_marker_kinds = {"fire", "gunfire", "smoke", "star_shell", "searchlight", "squall", "torpedo_hit", "sunk"}
    state.markers.extend(
            MarkerState(
                id=f"judge-{kind}",
                kind=kind,
                position=axis_ship.position,
                ship_id=axis_ship.id if kind == "gunfire" else None,
            )
        for kind in expected_marker_kinds
    )
    state.markers.append(MarkerState(id="judge-contact", kind="contact", position=axis_ship.position))
    state.wrecks.append(WreckState(id="judge-wreck", position=axis_ship.position, source_ship_id=axis_ship.id))
    judge_user = service.authenticate(service.issue_token("judge", "judge-password")[0])
    assert judge_user
    view = service.judge_observation(match.match_id, judge_user)
    assert view.seat_id == "referee"
    assert set(view.own_ship_ids) == {ship.id for ship in service.engine.get(match.game_id).ships.values()}
    assert len(view.ships) == len(view.own_ship_ids)
    assert {ship["id"] for ship in view.referee_ship_states} == set(view.own_ship_ids)
    full_state = next(ship for ship in view.referee_ship_states if ship["id"] == axis_ship.id)
    assert {"primary", "gun_mounts", "torpedo_launchers", "speed_damage_crossed", "fire_markers", "mfc_destroyed", "rudder_destroyed", "captain_status", "command_status"} <= set(full_state)
    assert {marker.kind for marker in view.markers} >= expected_marker_kinds
    assert not any(marker.kind == "contact" for marker in view.markers)
    assert len(view.wrecks) == 1
    torpedo_ships = {
        candidate["ship_id"]: candidate
        for candidate in view.torpedo_candidates
    }
    assert set(torpedo_ships) == {
        ship.id for ship in state.ships.values()
        if ship.position and ship.torpedo and not ship.torpedo.destroyed and not ship.sunk
    }
    assert all("settings" in candidate and "launchers" in candidate for candidate in torpedo_ships.values())


def test_torpedo_candidates_preview_uses_unsubmitted_movement_draft(tmp_path) -> None:
    service, engine, _, _, _, match, axis_seat, _, _, axis_ship, _ = make_service(tmp_path)
    state = engine.get(match.game_id)
    template = next(
        ship for ship in state.ships.values()
        if ship.side == Side.AXIS and ship.position and ship.torpedo
        and ship.torpedo_launchers and any(not launcher.destroyed and launcher.loaded for launcher in ship.torpedo_launchers)
    )
    ship = axis_ship
    ship.torpedo = template.torpedo.model_copy(deep=True)
    ship.torpedo_type = template.torpedo_type
    ship.torpedo_launchers = [launcher.model_copy(deep=True) for launcher in template.torpedo_launchers]
    user = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    assert user and ship.position
    original_position = ship.position.model_copy(deep=True)
    movement = MovementOrder(ship_id=ship.id, plan="1")

    candidates = service.torpedo_candidates_for_movement_draft(
        match.match_id, axis_seat.seat_id, user, [movement]
    )

    candidate = next(item for item in candidates if item["ship_id"] == ship.id)
    assert [item["mf"] for item in candidate["launch_positions"]] == [0, 1]
    assert candidate["blocked_reason"] is None
    assert ship.position == original_position
    assert f"{state.turn}:{Phase.MOVEMENT_PLANNING.value}" not in state.sealed_orders


def test_torpedo_candidate_preview_rejects_other_seat_ship_orders(tmp_path) -> None:
    service, _, _, _, _, match, axis_seat, _, _, _, _ = make_service(tmp_path)
    user = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    assert user

    with pytest.raises(PermissionError, match="controlled by this seat"):
        service.torpedo_candidates_for_movement_draft(
            match.match_id,
            axis_seat.seat_id,
            user,
            [MovementOrder(ship_id="not-controlled", plan="1")],
        )


def test_resolved_gunfire_marker_reaches_referee_and_only_observing_player(tmp_path) -> None:
    service, engine, _, _, _, match, axis_seat, allies_seat, _, axis_ship, allies_ship = make_service(tmp_path)
    state = engine.get(match.game_id)
    state.phase = Phase.GUNNERY
    match.phase = Phase.GUNNERY
    axis_ship.position = HexCoord(q=8, r=8)
    axis_ship.heading = 1
    for ship in state.ships.values():
        if ship.id not in {axis_ship.id, allies_ship.id}:
            ship.position = HexCoord(q=35, r=30)
    target_position = None
    mount = None
    for q in range(5, 12):
        for r in range(5, 12):
            candidate = HexCoord(q=q, r=r)
            allies_ship.position = candidate
            mount = next((
                item for item in axis_ship.gun_mounts
                if not item.destroyed
                and engine._mount_can_bear(axis_ship, allies_ship, item.arcs)
                and engine._can_see(state, axis_ship, allies_ship)
            ), None)
            if mount:
                target_position = candidate
                break
        if mount:
            break
    assert mount and target_position
    state.sealed_orders[f"{state.turn}:{Phase.GUNNERY.value}"] = {
        Side.AXIS.value: OrderBatch(
            side=Side.AXIS,
            phase=Phase.GUNNERY,
            gunnery=[GunneryOrder(
                ship_id=axis_ship.id,
                mounts=[GunMountOrder(mount_id=mount.id, target_id=allies_ship.id)],
            )],
        ),
    }

    engine._resolve_gunnery(state)
    marker = next(
        item for item in state.markers
        if item.kind == "gunfire" and item.ship_id == axis_ship.id
    )
    assert marker.position == axis_ship.position
    firing_position = marker.position
    axis_ship.position = HexCoord(q=axis_ship.position.q + 1, r=axis_ship.position.r)
    assert marker.position == firing_position

    player = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    opponent = service.authenticate(service.issue_token("allies-player", "allies-password")[0])
    judge = service.authenticate(service.issue_token("judge", "judge-password")[0])
    assert player and opponent and judge
    player_view = service.observation(match.match_id, axis_seat.seat_id, player)
    opponent_view = service.observation(match.match_id, allies_seat.seat_id, opponent)
    judge_view = service.judge_observation(match.match_id, judge)
    assert any(item.kind == "gunfire" and item.position == axis_ship.position for item in player_view.markers)
    assert any(item.kind == "gunfire" and item.position == axis_ship.position for item in opponent_view.markers)
    assert any(item.kind == "gunfire" and item.position == axis_ship.position for item in judge_view.markers)
    assert all(
        item.position != firing_position
        for view in (player_view, opponent_view, judge_view)
        for item in view.markers
        if item.kind == "gunfire"
    )

    axis_ship.position = HexCoord(q=35, r=30)
    distant_player_view = service.observation(match.match_id, allies_seat.seat_id, opponent)
    assert not any(item.kind == "gunfire" for item in distant_player_view.markers)


def test_hidden_projected_enemy_appears_as_anonymous_player_contact(tmp_path) -> None:
    service, engine, _, _, _, match, axis_seat, _, _, axis_ship, _ = make_service(tmp_path)
    state = engine.get(match.game_id)
    state.options.optional_rules.hidden_contacts = True
    state.phase = Phase.REINFORCEMENT
    match.phase = Phase.REINFORCEMENT
    axis_ship.position = HexCoord(q=10, r=10)
    enemy = next(ship for ship in state.ships.values() if ship.side == Side.ALLIES and ship.position)
    enemy_position = HexCoord(q=11, r=10)
    enemy.position = None
    enemy.fired = True
    enemy.fire_markers = 1
    for ship in state.ships.values():
        if ship.id not in {axis_ship.id, enemy.id}:
            ship.position = HexCoord(q=35, r=35)
    marker = MarkerState(
        id="CONTACT-hidden-enemy",
        kind="contact",
        secret_side=Side.ALLIES,
        contact_truth="real",
        position=enemy_position,
        heading=2,
        movement_rate=3,
    )
    state.markers.append(marker)
    state.contact_formations[marker.id] = [enemy.id]
    state.contact_offsets[marker.id] = {enemy.id: (0, 0)}
    service.repository.save_match(match)

    token, _ = service.issue_token("axis-player", "axis-password")
    user = service.authenticate(token)
    assert user
    view = service.observation(match.match_id, axis_seat.seat_id, user)
    contact = next(ship for ship in view.ships if ship.position == enemy_position and ship.id != axis_ship.id)
    assert contact.name.startswith(("驱逐", "巡洋", "战列"))
    assert contact.id == contact.name
    assert contact.side == Side.AXIS
    assert contact.asset in {"DD标记.png", "CA-CL标记.png", "BB标记.png"}
    assert contact.fire_markers == 1 and contact.fired
    assert view.referee_ship_states == []
    assert enemy.id not in view.model_dump_json()
    assert enemy.name not in view.model_dump_json()

    judge_user = service.authenticate(service.issue_token("judge", "judge-password")[0])
    assert judge_user
    judge_view = service.judge_observation(match.match_id, judge_user)
    assert any(ship["id"] == enemy.id for ship in judge_view.referee_ship_states)
    assert any(ship.id == enemy.id and ship.position == enemy_position for ship in judge_view.ships)
    assert not any(signal.kind == "contact" for signal in judge_view.markers)


def test_judge_can_patch_visibility_and_enable_storm(tmp_path) -> None:
    service, engine, judge, _, _, match, axis_seat, allies_seat, _, _, _ = make_service(tmp_path)
    judge_user = service.authenticate(service.issue_token("judge", "judge-password")[0])
    axis_user = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    allies_user = service.authenticate(service.issue_token("allies-player", "allies-password")[0])
    assert judge_user and axis_user and allies_user

    judge_view = service.judge_observation(match.match_id, judge_user)
    assert judge_view.visibility_axis == 14
    assert judge_view.visibility_allies == 12
    assert judge_view.storm is False
    result = service.judge_patch_visibility(
        match.match_id,
        judge_user,
        JudgeVisibilityPatchRequest(
            axis=16,
            allies=10,
            storm=True,
            hidden_contacts=True,
            reason="测试风暴降临",
            expected_version=judge_view.version,
        ),
    )
    assert result["visibility_axis"] == 16
    assert result["visibility_allies"] == 10
    state = engine.get(match.game_id)
    assert state.options.optional_rules.storm is True
    assert state.options.optional_rules.hidden_contacts is True
    assert result["hidden_contacts"] is True
    assert engine.visibility_radius(state, Side.AXIS) == 8
    assert engine.visibility_radius(state, Side.ALLIES) == 5
    axis_view = service.observation(match.match_id, axis_seat.seat_id, axis_user)
    allies_view = service.observation(match.match_id, allies_seat.seat_id, allies_user)
    assert axis_view.visibility == 8
    assert allies_view.visibility == 5
    assert axis_view.visibility_axis is None
    assert axis_view.visibility_allies is None
    assert any(record.kind == "visibility" for record in service.judge_overrides(match.match_id, judge_user))
    assert service.judge_overrides(match.match_id, judge_user)[-1].after["hidden_contacts"] is True


def test_orders_wait_for_all_seats_and_referee_approval(tmp_path) -> None:
    service, engine, judge, axis, allies, match, axis_seat, allies_seat, proxy_seats, _, _ = make_service(tmp_path)
    axis_user = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    allies_user = service.authenticate(service.issue_token("allies-player", "allies-password")[0])
    judge_user = service.authenticate(service.issue_token("judge", "judge-password")[0])
    assert axis_user and allies_user and judge_user

    axis_batch = OrderBatch(side=Side.AXIS, phase=match.phase)
    allies_batch = OrderBatch(side=Side.ALLIES, phase=match.phase)
    service.submit_order(
        match.match_id,
        axis_seat.seat_id,
        axis_user,
        SeatOrderSubmission(phase=match.phase, batch=axis_batch, request_id="axis-1"),
    )
    assert service.summary(match.match_id).match.stage == MatchStage.COLLECTING
    ready = service.submit_order(
        match.match_id,
        allies_seat.seat_id,
        allies_user,
        SeatOrderSubmission(phase=match.phase, batch=allies_batch, request_id="allies-1"),
    )
    for proxy_seat in proxy_seats:
        service.submit_order(
            match.match_id,
            proxy_seat.seat_id,
            judge_user,
            SeatOrderSubmission(
                phase=match.phase,
                batch=OrderBatch(side=proxy_seat.side, phase=match.phase),
                request_id=f"proxy-{proxy_seat.seat_id}",
            ),
        )
    ready = service.summary(match.match_id).match
    assert ready.stage == MatchStage.READY_FOR_REVIEW
    result = service.approve(match.match_id, judge_user, ready.version)
    assert result.events
    assert result.match.stage == MatchStage.AWAITING_PUBLICATION
    assert engine.get(match.game_id).phase == result.match.phase
    published = service.publish_adjudication(match.match_id, judge_user, result.match.version)
    assert published.stage in {MatchStage.COLLECTING, MatchStage.COMPLETE}


def test_seat_fragments_can_cover_movement_and_resolution_phases(tmp_path) -> None:
    service, engine, judge, axis, allies, match, axis_seat, allies_seat, proxy_seats, _, _ = make_service(tmp_path)
    axis_user = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    allies_user = service.authenticate(service.issue_token("allies-player", "allies-password")[0])
    judge_user = service.authenticate(service.issue_token("judge", "judge-password")[0])
    assert axis_user and allies_user and judge_user

    def submit(seat, user, batch, request_id):
        current_phase = service.summary(match.match_id).match.phase
        batch.phase = current_phase
        service.submit_order(
            match.match_id,
            seat.seat_id,
            user,
            SeatOrderSubmission(phase=current_phase, batch=batch, request_id=request_id),
        )

    # First approve the opening reinforcement phase.
    submit(axis_seat, axis_user, OrderBatch(side=Side.AXIS, phase=match.phase), "axis-reinforcement")
    submit(allies_seat, allies_user, OrderBatch(side=Side.ALLIES, phase=match.phase), "allies-reinforcement")
    for proxy in proxy_seats:
        submit(proxy, judge_user, OrderBatch(side=proxy.side, phase=match.phase), f"proxy-{proxy.seat_id}-reinforcement")
    review = service.summary(match.match_id).match
    first = service.approve(match.match_id, judge_user, review.version)
    assert engine.get(match.game_id).phase.value == "movement_planning"
    service.publish_adjudication(match.match_id, judge_user, first.match.version)

    # Each seat contributes only its own movement plan; the platform validates
    # the complete side after the last seat arrives.
    for seat in [axis_seat, allies_seat, *proxy_seats]:
        user = axis_user if seat is axis_seat else allies_user if seat is allies_seat else judge_user
        ship_id = seat.controlled_ship_ids[0]
        submit(
            seat,
            user,
            OrderBatch(
                side=seat.side,
                phase=match.phase,
                movement=[MovementOrder(ship_id=ship_id, plan="0")],
            ),
            f"movement-{seat.seat_id}",
        )
    review = service.summary(match.match_id).match
    movement_result = service.approve(match.match_id, judge_user, review.version)
    assert movement_result.match.phase.value == "torpedo_planning"
    service.publish_adjudication(match.match_id, judge_user, movement_result.match.version)

    # Approval consumes movement_resolution in the same referee action; no
    # order collection is opened for that deterministic phase.
    for seat in [axis_seat, allies_seat, *proxy_seats]:
        user = axis_user if seat is axis_seat else allies_user if seat is allies_seat else judge_user
        submit(seat, user, OrderBatch(side=seat.side, phase=match.phase), f"torpedo-{seat.seat_id}")
    review = service.summary(match.match_id).match
    torpedo_result = service.approve(match.match_id, judge_user, review.version)
    assert torpedo_result.match.phase.value == "gunnery"
    service.publish_adjudication(match.match_id, judge_user, torpedo_result.match.version)


def test_player_turn_submission_validates_future_phase_orders(tmp_path) -> None:
    service, engine, _, _, _, match, _, allies_seat, _, _, ship = make_service(tmp_path)
    user = service.authenticate(service.issue_token("allies-player", "allies-password")[0])
    assert user

    state = engine.get(match.game_id)
    ship.name = "华盛顿"
    ship.ship_type = "BB"
    ship.speed_track = (5, 4, 4)
    ship.speed_damage_track = ((5, 4, 3, 2, 1), (4, 3, 2, 1), (4, 3, 2, 1))
    ship.speed_damage_crossed = (0, 0, 0)
    ship.current_speed = 5
    ship.previous_speed = 5
    service.game_repository.save(state)

    with pytest.raises(ValueError, match="movement cost 6 outside legal range") as error:
        service.submit_order(
            match.match_id,
            allies_seat.seat_id,
            user,
            SeatOrderSubmission(
                phase=Phase.REINFORCEMENT,
                batch=OrderBatch(
                    side=Side.ALLIES,
                    phase=Phase.REINFORCEMENT,
                    movement=[MovementOrder(ship_id=ship.id, plan="6", speed=5)],
                    torpedoes=[TorpedoOrder(ship_id=ship.id, launcher_id="INVALID")],
                    gunnery=[GunneryOrder(
                        ship_id=ship.id,
                        primary_target="NO-SUCH-SHIP",
                        mounts=[GunMountOrder(mount_id="INVALID", target_id="NO-SUCH-SHIP")],
                    )],
                    illumination=[IlluminationOrder(
                        ship_id=ship.id,
                        mount_id="INVALID",
                        target_hex=HexCoord(q=1, r=1),
                    )],
                ),
                scope="turn",
                request_id="washington-six-mf",
            ),
        )
    message = str(error.value)
    assert "unavailable torpedo launcher INVALID" in message
    assert "Illegal target NO-SUCH-SHIP" in message
    assert "Star-shell orders require optional rule 9.3" in message


def test_full_turn_submission_accepts_future_phase_orders_and_advances_once(tmp_path) -> None:
    service, engine, _, _, _, match, axis_seat, allies_seat, proxy_seats, axis_ship, _ = make_service(tmp_path)
    axis_user = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    allies_user = service.authenticate(service.issue_token("allies-player", "allies-password")[0])
    judge_user = service.authenticate(service.issue_token("judge", "judge-password")[0])
    assert axis_user and allies_user and judge_user
    state = engine.get(match.game_id)
    state.options.optional_rules.star_shells = True
    state.options.optional_rules.searchlights = True
    axis_mount = state.ships[axis_ship.id].gun_mounts[0]

    for seat in [axis_seat, allies_seat, *proxy_seats]:
        user = axis_user if seat is axis_seat else allies_user if seat is allies_seat else judge_user
        ship_id = seat.controlled_ship_ids[0]
        ship = state.ships[ship_id]
        orders = OrderBatch(
            side=seat.side,
            phase=Phase.REINFORCEMENT,
            movement=[MovementOrder(ship_id=ship_id, plan="0")],
        )
        if seat is axis_seat:
            orders.illumination = [
                IlluminationOrder(ship_id=ship_id, mount_id=axis_mount.id, target_hex=ship.position)
            ]
            orders.searchlights = [SearchlightOrder(ship_id=ship_id, target_id=ship_id)]
        service.submit_order(
            match.match_id,
            seat.seat_id,
            user,
            SeatOrderSubmission(phase=Phase.REINFORCEMENT, scope="turn", batch=orders, request_id=f"turn-{seat.seat_id}"),
        )

    ready = service.summary(match.match_id).match
    assert ready.stage == MatchStage.READY_FOR_REVIEW
    assert service.repository.order_scope(match.match_id, Phase.REINFORCEMENT) == "turn"
    result = service.approve(match.match_id, judge_user, ready.version)
    assert result.match.phase == Phase.REINFORCEMENT
    assert state.turn == 2
    assert result.match.stage == MatchStage.AWAITING_PUBLICATION
    player = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    assert player
    published_view = service.observation(match.match_id, axis_seat.seat_id, player)
    assert published_view.turn == 1
    assert published_view.stage == MatchStage.AWAITING_PUBLICATION
    published = service.publish_adjudication(match.match_id, judge_user, result.match.version)
    assert published.stage in {MatchStage.COLLECTING, MatchStage.COMPLETE}
    assert service.observation(match.match_id, axis_seat.seat_id, player).turn == 2


def test_full_turn_submission_can_ignore_missing_movement_and_keeps_review_warning(tmp_path) -> None:
    service, engine, _, axis_user_record, allies_user_record, match, axis_seat, allies_seat, proxy_seats, _, _ = make_service(tmp_path)
    axis_user = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    allies_user = service.authenticate(service.issue_token("allies-player", "allies-password")[0])
    judge_user = service.authenticate(service.issue_token("judge", "judge-password")[0])
    assert axis_user and allies_user and judge_user
    seats = [axis_seat, allies_seat, *proxy_seats]
    missing_seat = seats[-1]
    for seat in seats[:-1]:
        user = axis_user if seat.side == Side.AXIS and seat.user_id == axis_user_record.user_id else (
            allies_user if seat.side == Side.ALLIES and seat.user_id == allies_user_record.user_id else judge_user
        )
        ship_id = seat.controlled_ship_ids[0]
        service.submit_order(
            match.match_id,
            seat.seat_id,
            user,
            SeatOrderSubmission(
                phase=Phase.REINFORCEMENT,
                scope="turn",
                batch=OrderBatch(side=seat.side, phase=Phase.REINFORCEMENT, movement=[MovementOrder(ship_id=ship_id, plan="0")]),
                request_id=f"turn-covered-{seat.seat_id}",
            ),
        )

    ship_id = missing_seat.controlled_ship_ids[0]
    user = axis_user if missing_seat.side == Side.AXIS and missing_seat.user_id == axis_user_record.user_id else (
        allies_user if missing_seat.side == Side.ALLIES and missing_seat.user_id == allies_user_record.user_id else judge_user
    )
    submission = SeatOrderSubmission(
        phase=Phase.REINFORCEMENT,
        scope="turn",
        batch=OrderBatch(side=missing_seat.side, phase=Phase.REINFORCEMENT),
        request_id="turn-missing-movement",
    )
    assert submission.ignore_validation_errors is False
    with pytest.raises(ValueError, match="缺少移动计划"):
        service.submit_order(
            match.match_id,
            missing_seat.seat_id,
            user,
            submission,
        )

    assert service.summary(match.match_id).match.stage == MatchStage.COLLECTING
    assert len(service.repository.orders(match.match_id, Phase.REINFORCEMENT)) == len(seats) - 1
    submission.ignore_validation_errors = True
    ready = service.submit_order(match.match_id, missing_seat.seat_id, user, submission)
    assert ready.stage == MatchStage.READY_FOR_REVIEW
    warnings = service.repository.order_validation_errors(match.match_id, Phase.REINFORCEMENT)[missing_seat.seat_id]
    assert any("缺少移动计划" in warning for warning in warnings)
    with pytest.raises(ValueError, match="Movement plans must cover every active ship"):
        service.approve(match.match_id, service.authenticate(service.issue_token("judge", "judge-password")[0]), ready.version)


def test_non_owner_cannot_submit_for_a_seat(tmp_path) -> None:
    service, _, _, _, allies, match, axis_seat, _, _, _, _ = make_service(tmp_path)
    allies_user = service.authenticate(service.issue_token("allies-player", "allies-password")[0])
    assert allies_user
    batch = OrderBatch(side=Side.AXIS, phase=match.phase)
    try:
        service.submit_order(
            match.match_id,
            axis_seat.seat_id,
            allies_user,
            SeatOrderSubmission(phase=match.phase, batch=batch, request_id="forged"),
        )
    except PermissionError:
        return
    raise AssertionError("foreign seat submission was accepted")


def test_same_side_contact_marker_is_private_to_the_submitting_seat(tmp_path) -> None:
    service, engine, _, axis, _, match, axis_seat, _, _, _, _ = make_service(tmp_path)
    state = engine.get(match.game_id)
    state.markers.append(
        MarkerState(id="contact-private", kind="contact", secret_side=Side.AXIS)
    )
    token, _ = service.issue_token("axis-player", "axis-password")
    user = service.authenticate(token)
    assert user
    assert not any(marker.id == "contact-private" for marker in service.observation(match.match_id, axis_seat.seat_id, user).markers)
    service.repository.remember_markers(match.match_id, axis_seat.seat_id, ["contact-private"])
    assert any(marker.id == "contact-private" for marker in service.observation(match.match_id, axis_seat.seat_id, user).markers)


def test_referee_can_replace_any_pending_seat_order(tmp_path) -> None:
    service, _, judge, _, _, match, axis_seat, _, _, _, _ = make_service(tmp_path)
    judge_user = service.authenticate(service.issue_token("judge", "judge-password")[0])
    player = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    assert judge_user and player
    service.submit_order(
        match.match_id,
        axis_seat.seat_id,
        player,
        SeatOrderSubmission(
            phase=match.phase,
            batch=OrderBatch(side=Side.AXIS, phase=match.phase),
            request_id="player-order",
        ),
    )
    expected = service.summary(match.match_id).match.version
    updated = service.judge_replace_order(
        match.match_id,
        axis_seat.seat_id,
        judge_user,
        JudgeOrderOverrideRequest(
            phase=match.phase,
            batch=OrderBatch(side=Side.AXIS, phase=match.phase),
            reason="裁判修订行动",
            expected_version=expected,
        ),
    )
    assert updated.version == expected + 1
    request_id, _ = service.repository.orders(match.match_id, match.phase)[axis_seat.seat_id]
    assert request_id.startswith("judge-")
    assert service.repository.overrides(match.match_id)[-1].kind == "order"


def test_referee_can_reject_before_all_seats_submit(tmp_path) -> None:
    service, _, judge, _, _, match, axis_seat, _, _, _, _ = make_service(tmp_path)
    judge_user = service.authenticate(service.issue_token("judge", "judge-password")[0])
    axis_user = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    assert judge_user and axis_user
    service.submit_order(
        match.match_id,
        axis_seat.seat_id,
        axis_user,
        SeatOrderSubmission(
            phase=match.phase,
            batch=OrderBatch(side=Side.AXIS, phase=match.phase),
            request_id="partial-order",
        ),
    )
    current = service.summary(match.match_id).match
    assert current.stage == MatchStage.COLLECTING
    rejected = service.reject(match.match_id, judge_user, current.version, "裁判要求全部重填")
    assert rejected.stage == MatchStage.COLLECTING
    assert service.repository.orders(match.match_id, match.phase) == {}


def test_referee_reroll_replays_star_shell_and_publishes_only_final_result(tmp_path, monkeypatch) -> None:
    service, engine, _, _, _, match, axis_seat, allies_seat, proxy_seats, axis_ship, _ = make_service(tmp_path)
    judge_user = service.authenticate(service.issue_token("judge", "judge-password")[0])
    axis_user = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    allies_user = service.authenticate(service.issue_token("allies-player", "allies-password")[0])
    assert judge_user and axis_user and allies_user
    state = engine.get(match.game_id)
    state.options.optional_rules.star_shells = True
    axis_mount = state.ships[axis_ship.id].gun_mounts[0]
    original_hull = state.ships[axis_ship.id].hull
    seats = [axis_seat, allies_seat, *proxy_seats]
    for seat in seats:
        user = axis_user if seat is axis_seat else allies_user if seat is allies_seat else judge_user
        ship_id = seat.controlled_ship_ids[0]
        batch = OrderBatch(
            side=seat.side,
            phase=Phase.REINFORCEMENT,
            movement=[MovementOrder(ship_id=ship_id, plan="0")],
        )
        if seat is axis_seat:
            batch.illumination = [IlluminationOrder(ship_id=ship_id, mount_id=axis_mount.id, target_hex=state.ships[ship_id].position)]
        service.submit_order(
            match.match_id,
            seat.seat_id,
            user,
            SeatOrderSubmission(phase=Phase.REINFORCEMENT, scope="turn", batch=batch, request_id=f"reroll-{seat.seat_id}"),
        )
    approved = service.approve(match.match_id, judge_user, service.summary(match.match_id).match.version)
    assert approved.match.stage == MatchStage.AWAITING_PUBLICATION
    star_shell = next(event for event in engine.get(match.game_id).events if event.type == "star_shell_fired")
    old_success = star_shell.payload["success"]
    player_view = service.observation(match.match_id, axis_seat.seat_id, axis_user)
    assert player_view.turn == 1
    assert not any(marker.kind == "star_shell" for marker in player_view.markers)

    patched = service.judge_patch_ship(
        match.match_id,
        axis_ship.id,
        judge_user,
        JudgeShipPatchRequest(
            patch={"hull": max(0, original_hull - 1), "smoke": True},
            reason="补录伤害",
            expected_version=approved.match.version,
        ),
    )
    replacement = 1 if old_success else 6
    monkeypatch.setattr(
        service,
        "_reroll",
        lambda _state, dice: DiceRoll(dice=[replacement], notation=dice.notation, raw=replacement, rng_index=dice.rng_index),
    )
    rerolled = service.judge_reroll(
        match.match_id,
        judge_user,
        JudgeRerollRequest(
            event_sequence=star_shell.sequence,
            reason="重投",
            expected_version=patched["match"]["version"],
        ),
    )
    assert rerolled["event"]["dice"]["raw"] == replacement
    assert rerolled["reroll_event"]["type"] == "judge_reroll"
    base_sequence = service.repository.pending_adjudication(match.match_id)["base_sequence"]
    non_dice = next(
        event for event in engine.get(match.game_id).events
        if event.sequence > base_sequence and event.dice is None
    )
    with pytest.raises(ValueError, match="Only events with dice can be rerolled"):
        service.judge_reroll(
            match.match_id,
            judge_user,
            JudgeRerollRequest(
                event_sequence=non_dice.sequence,
                reason="不能重骰非骰点事件",
                expected_version=rerolled["match"]["version"],
            ),
        )
    rerolled = service.judge_reroll(
        match.match_id,
        judge_user,
        JudgeRerollRequest(
            event_sequence=rerolled["event"]["sequence"],
            reason="再次确认重骰替换而非叠加",
            expected_version=rerolled["match"]["version"],
        ),
    )
    current = engine.get(match.game_id)
    updated_star_shell = next(event for event in current.events if event.dice and event.dice.rng_index == star_shell.dice.rng_index)
    assert updated_star_shell.payload["success"] is (replacement >= 4)
    assert any(marker.kind == "star_shell" for marker in current.markers) is (replacement >= 4)
    assert current.ships[axis_ship.id].hull == max(0, original_hull - 1)
    assert current.ships[axis_ship.id].smoke is True
    assert len([marker for marker in current.markers if marker.kind == "star_shell"]) == int(replacement >= 4)
    assert any(item.kind == "ship_state" for item in service.repository.overrides(match.match_id))
    assert any(event.type == "judge_ship_override" for event in current.events)
    engine._event(
        current,
        "enemy_secret_action",
        "隐藏敌舰炮击计划",
        payload={"secret_side": Side.ALLIES.value, "ship_id": "hidden-enemy"},
    )
    view = service.observation(match.match_id, axis_seat.seat_id, axis_user)
    assert view.recent_events == []
    assert "隐藏敌舰炮击计划" not in view.model_dump_json()
    assert view.turn == 1
    assert view.stage == MatchStage.AWAITING_PUBLICATION
    published = service.publish_adjudication(match.match_id, judge_user, rerolled["match"]["version"])
    assert published.stage == MatchStage.COLLECTING
    published_view = service.observation(match.match_id, axis_seat.seat_id, axis_user)
    assert published_view.turn == 2
    assert any(marker.kind == "star_shell" for marker in published_view.markers) is (replacement >= 4)


def test_referee_captain_patch_blocks_next_turn_orders_and_exposes_judge_candidates(tmp_path) -> None:
    service, engine, _, _, _, match, axis_seat, _, _, axis_ship, _ = make_service(tmp_path)
    judge_user = service.authenticate(service.issue_token("judge", "judge-password")[0])
    axis_user = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    assert judge_user and axis_user
    state = engine.get(match.game_id)
    patched = service.judge_patch_ship(
        match.match_id,
        axis_ship.id,
        judge_user,
        JudgeShipPatchRequest(
            patch={"captain_status": "wounded"},
            reason="测试舰长失能",
            expected_version=service.summary(match.match_id).match.version,
        ),
    )
    assert patched["ship"]["captain_command_loss_turn"] == state.turn + 1

    judge_view = service.judge_observation(match.match_id, judge_user)
    candidate_ids = {candidate["ship_id"] for candidate in judge_view.gunnery_candidates}
    assert candidate_ids == set(state.ships)

    state.turn += 1
    view = service.observation(match.match_id, axis_seat.seat_id, axis_user)
    own_ship = next(ship for ship in view.ships if ship.id == axis_ship.id)
    assert own_ship.captain_command_lost is True
    with pytest.raises(ValueError, match="舰长失能"):
        service.submit_order(
            match.match_id,
            axis_seat.seat_id,
            axis_user,
            SeatOrderSubmission(
                phase=Phase.REINFORCEMENT,
                scope="turn",
                batch=OrderBatch(
                    side=Side.AXIS,
                    phase=Phase.REINFORCEMENT,
                    movement=[MovementOrder(ship_id=axis_ship.id, plan="0")],
                ),
                request_id="captain-incapacity-order",
            ),
        )


@pytest.mark.parametrize("storm,die_sides", [(False, 100), (True, 50)])
def test_telegram_receive_roll_is_reduced_by_each_hit_in_sent_turn(tmp_path, monkeypatch, storm, die_sides) -> None:
    service, engine, _, _, _, match, axis_seat, _, proxy_seats, _, _ = make_service(tmp_path)
    axis_user = service.authenticate(service.issue_token("axis-player", "axis-password")[0])
    judge_user = service.authenticate(service.issue_token("judge", "judge-password")[0])
    assert axis_user and judge_user
    recipient = next(seat for seat in proxy_seats if seat.side == Side.AXIS)
    target_id = recipient.controlled_ship_ids[0]
    state = engine.get(match.game_id)
    state.options.optional_rules.storm = storm
    for event_type, payload in (
        ("gunnery_result", {"target": target_id}),
        ("gunnery_result", {"target": target_id}),
        ("torpedo_result", {"target": target_id}),
        ("collision_result", {"ship_id": target_id}),
    ):
        engine._event(state, event_type, "test hit", payload=payload)
    service.submit_telegram(
        match.match_id,
        axis_seat.seat_id,
        axis_user,
        TelegramSubmission(message="x" * 27),
    )
    monkeypatch.setattr("iron_bottom_sound.platform.service.secrets.randbelow", lambda _sides: 49)
    state.turn += 1
    records = service.judge_telegrams(match.match_id, judge_user)
    delivery = next(
        item for item in records[0]["deliveries"]
        if item["recipient_seat_id"] == recipient.seat_id
    )

    assert delivery["die_sides"] == die_sides
    assert delivery["roll"] == 50
    assert delivery["hit_count"] == 4
    assert delivery["adjusted_roll"] == 18
    assert delivery["delivered"] is False

    monkeypatch.setattr(
        service.repository,
        "telegram_inbox",
        lambda _match_id, _seat_id: [{
            "rule_id": "IBS-R-TELEGRAM-01",
            "telegram_id": "telegram-private-result",
            "sent_turn": 1,
            "received_turn": 2,
            "message": None,
            "delivered": False,
            "char_count": 27,
            "die_sides": die_sides,
            "roll": 50,
            "hit_count": 4,
            "adjusted_roll": 18,
        }],
    )
    player_inbox = service.telegram_inbox(match.match_id, axis_seat.seat_id, axis_user)
    player_delivery = player_inbox["messages"][0]
    assert player_delivery == {
        "telegram_id": "telegram-private-result",
        "sent_turn": 1,
        "received_turn": 2,
        "sender": "友军电报",
        "message": None,
        "delivered": False,
    }
    assert not {"char_count", "die_sides", "roll", "hit_count", "adjusted_roll"} & player_delivery.keys()


def test_telegram_delivery_schema_migrates_existing_records(tmp_path) -> None:
    database = tmp_path / "legacy-platform.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE platform_telegram_deliveries (
             telegram_id TEXT NOT NULL,
             recipient_seat_id TEXT NOT NULL,
             received_turn INTEGER NOT NULL,
             char_count INTEGER NOT NULL,
             die_sides INTEGER NOT NULL,
             roll INTEGER NOT NULL,
             delivered INTEGER NOT NULL
           )"""
    )
    connection.close()

    repository = PlatformRepository(database)
    columns = {row["name"] for row in repository.connection.execute("PRAGMA table_info(platform_telegram_deliveries)")}
    repository.connection.close()
    assert {"rule_id", "hit_count", "adjusted_roll"} <= columns


def test_non_referee_cannot_use_judge_override(tmp_path) -> None:
    service, _, _, _, allies, match, axis_seat, _, _, _, _ = make_service(tmp_path)
    allies_user = service.authenticate(service.issue_token("allies-player", "allies-password")[0])
    assert allies_user
    try:
        service.judge_replace_order(
            match.match_id,
            axis_seat.seat_id,
            allies_user,
            JudgeOrderOverrideRequest(
                phase=match.phase,
                batch=OrderBatch(side=Side.AXIS, phase=match.phase),
                reason="forged",
                expected_version=service.summary(match.match_id).match.version,
            ),
        )
    except PermissionError:
        return
    raise AssertionError("non-referee override was accepted")


def test_seat_validation_errors_do_not_name_foreign_ships(tmp_path) -> None:
    """席位报错不得点出非本席位舰船的真实身份（2026-10-12 信息泄露修复）。

    席位视图对所有非本席位舰船做匿名化——**包括能观察到的敌舰**，玩家只该看到匿名接触名。
    而引擎/平台报错用真实 id 与舰名："cannot bear on <id>"、"舰长失能…"、
    "Seat cannot command ships: [...]"，都会把身份直接摊给玩家。故脱敏按**归属**判定。
    """
    service, engine, _, axis, _, match, axis_seat, _, _, axis_ship, allies_ship = make_service(tmp_path)
    state = engine.get(match.game_id)
    assert axis_ship.position
    # 关键回归场景：敌舰就在旁边（本席位能看见、能瞄到），报错仍不得点名它。
    allies_ship.position = HexCoord(q=axis_ship.position.q + 1, r=axis_ship.position.r)

    # ① "炮没射界"这类报错不得带敌方 id（能瞄到 ≠ 知道身份）
    bearing = service._redact_foreign_ship_identities(
        match, axis_seat, [f"{axis_ship.id}:TT-M1 cannot bear on {allies_ship.id}"]
    )[0]
    assert allies_ship.id not in bearing
    assert axis_ship.id in bearing  # 本方舰只保留身份
    assert "未识别舰船" in bearing

    # ② 带舰名的报错（舰长失能）同样不得点名敌舰
    named = service._redact_foreign_ship_identities(
        match,
        axis_seat,
        [f"{allies_ship.name} 舰长失能：本回合仅由引擎自动按原速直航，不接受其他行动订单"],
    )[0]
    assert allies_ship.name not in named
    assert "未识别舰船" in named

    # ③ 归属报错同样经过脱敏
    batch = OrderBatch(side=axis_seat.side, phase=state.phase, gunnery=[GunneryOrder(ship_id=allies_ship.id)])
    submission = SeatOrderSubmission(phase=state.phase, batch=batch, scope="phase", request_id="leak-ownership")
    with pytest.raises(PermissionError) as excinfo:
        service.submit_order(match.match_id, axis_seat.seat_id, axis, submission)
    message = str(excinfo.value)
    assert allies_ship.id not in message
    assert "未识别舰船" in message

    # ④ 本方舰名必须保留：脱敏只针对非本席位舰船，不做无差别遮蔽
    kept = service._redact_foreign_ship_identities(match, axis_seat, [f"{axis_ship.name} 舰长失能：示例"])
    assert axis_ship.name in kept[0]


def test_observer_is_match_scoped_read_only_and_gets_full_view(tmp_path) -> None:
    service, engine, _judge, *_rest = make_service(tmp_path)
    match = service.repository.connection.execute("SELECT match_id FROM platform_matches").fetchone()["match_id"]
    other_match = service.create_match("IBS-S-03", 9, service.repository.user_by_username("judge").user_id)
    app = FastAPI()
    app.include_router(build_router(service))
    client = TestClient(app)
    judge_token, _ = service.issue_token("judge", "judge-password") or (None, None)
    assert judge_token

    created = client.post(
        f"/platform/matches/{match}/judge/observers",
        headers={"Authorization": f"Bearer {judge_token}"},
    )
    assert created.status_code == 201
    credentials = created.json()
    assert credentials["username"].startswith("observer-")
    assert "password" in credentials

    login = client.post("/platform/auth/login", json={"username": credentials["username"], "password": credentials["password"]})
    assert login.status_code == 200
    observer_token = login.json()["token"]
    assert login.json()["is_observer"] is True
    assert login.json()["is_referee"] is False

    summary = client.get(f"/platform/matches/{match}", headers={"Authorization": f"Bearer {observer_token}"})
    assert summary.status_code == 200
    assert summary.json()["seats"] == []

    observation = client.get(f"/platform/matches/{match}/spectator/view", headers={"Authorization": f"Bearer {observer_token}"})
    assert observation.status_code == 200
    assert len(observation.json()["referee_ship_states"]) == len(engine.get(service.repository.match(match).game_id).ships)
    assert observation.json()["recent_events"] == []
    assert observation.json()["rerollable_events"] == []

    assert client.get(f"/platform/matches/{match}/judge/view", headers={"Authorization": f"Bearer {observer_token}"}).status_code == 403
    assert client.get(f"/platform/matches/{match}/review", headers={"Authorization": f"Bearer {observer_token}"}).status_code == 403
    assert client.get(f"/platform/matches/{match}/judge/observers", headers={"Authorization": f"Bearer {observer_token}"}).status_code == 403
    assert client.get(f"/platform/matches/{other_match.match_id}/spectator/view", headers={"Authorization": f"Bearer {observer_token}"}).status_code == 403
    seat_id = service.repository.seats(match)[0].seat_id
    assert client.get(f"/platform/matches/{match}/seats/{seat_id}/view", headers={"Authorization": f"Bearer {observer_token}"}).status_code == 403

