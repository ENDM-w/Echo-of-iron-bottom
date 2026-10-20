from copy import deepcopy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iron_bottom_sound.custom_scenarios import with_engine_default_formations
from iron_bottom_sound.data import load_scenario, register_custom_scenario, unregister_custom_scenario
from iron_bottom_sound.engine import IronBottomEngine
from iron_bottom_sound.models import FormationState, GunneryOrder, HexCoord, IlluminationOrder, OrderBatch, Phase, SearchlightOrder, Side
from iron_bottom_sound.platform.router import build_router
from iron_bottom_sound.platform.service import PlatformService
from iron_bottom_sound.storage import GameRepository


@pytest.fixture
def online_formation(tmp_path):
    service = PlatformService(IronBottomEngine(), GameRepository(tmp_path / "online.sqlite3"))
    service.create_user("referee", "referee-password", is_referee=True)
    definition = deepcopy(load_scenario("IBS-S-03"))
    definition["id"] = "IBS-CUSTOM-ONLINE-FORMATIONS"
    definition["optional_rules"] = []
    definition["ships"] = []
    source = load_scenario("IBS-S-03")
    definition["formations"] = {}
    definition["seat_assignments"] = []
    for side in Side:
        members = deepcopy([ship for ship in source["ships"] if ship["side"] == side.value][:2])
        for index, ship in enumerate(members):
            ship.update(position=("H10" if side == Side.AXIS else "X20"), heading=1, speed=3, flagship=index == 0)
            definition["seat_assignments"].append({
                "ship_id": ship["id"], "username": f"{side.value}-{index}", "password": "formation-password",
            })
        definition["ships"].extend(members)
        ids = [ship["id"] for ship in members]
        definition["formations"][side.value] = [{
            "formation_id": f"{side.value}-column", "name": f"{side.value} column", "ship_ids": ids,
            "leader_id": ids[0], "flagship_id": ids[0], "reserve_flagship_id": ids[1], "spacing": 1, "heading": 1,
        }]
    definition = json.loads(json.dumps(with_engine_default_formations(definition), default=str))
    register_custom_scenario(definition)
    service.game_repository.save_custom_scenario(definition["id"], definition)
    app = FastAPI()
    app.include_router(build_router(service))
    try:
        with TestClient(app) as client:
            def login(username, password):
                response = client.post("/platform/auth/login", json={"username": username, "password": password})
                assert response.status_code == 200, response.text
                return response.json()
            judge = login("referee", "referee-password")
            judge_headers = {"Authorization": f"Bearer {judge['token']}"}
            created = client.post("/platform/matches", headers=judge_headers, json={
                "scenario_id": definition["id"], "seed": 17, "judge_user_id": judge["user_id"], "realistic_command": True,
            })
            assert created.status_code == 200, created.text
            match_id = created.json()["match_id"]
            root = f"/platform/matches/{match_id}"
            seats = client.get(root, headers=judge_headers).json()["seats"]
            players = []
            for credentials in definition["seat_assignments"]:
                user = login(credentials["username"], credentials["password"])
                seat = next(item for item in seats if item["user_id"] == user["user_id"])
                players.append((seat, {"Authorization": f"Bearer {user['token']}"}))
            yield client, service, root, judge_headers, players
    finally:
        unregister_custom_scenario(definition["id"])


def seat_view(client, root, player):
    seat, headers = player
    response = client.get(f"{root}/seats/{seat['seat_id']}/view", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def submit(client, root, player, *, plan=None, mode="column_follow", commands=None):
    seat, headers = player
    view = seat_view(client, root, player)
    if commands is None:
        commands = [{
            "formation_id": formation["formation_id"], "leader_plan": plan or "0", "spacing": 1,
            "heading": 1, "turn_mode": mode,
        } for formation in view["formations"] if formation["role"] == "flagship"]
    return client.post(f"{root}/seats/{seat['seat_id']}/orders", headers=headers, json={
        "phase": view["phase"], "batch": {"side": seat["side"], "phase": view["phase"]},
        "scope": "turn" if view["phase"] == "reinforcement" else "phase",
        "request_id": f"test-{seat['seat_id']}-{view['turn']}-{view['phase']}", "formation_commands": commands,
    })


def command_players(client, root, players):
    return [
        player for player in players
        if any(formation["role"] == "flagship" for formation in seat_view(client, root, player)["formations"])
    ]


def approve_and_publish(client, root, headers):
    current = client.get(root, headers=headers).json()["match"]
    result = client.post(f"{root}/approve", headers=headers, json={"expected_version": current["version"]})
    assert result.status_code == 200, result.text
    assert result.json()["match"]["stage"] == "awaiting_publication"
    published = client.post(f"{root}/judge/publish", headers=headers, json={"expected_version": result.json()["match"]["version"]})
    assert published.status_code == 200, published.text


@pytest.mark.parametrize("turn_mode", ["column_follow", "synchronized"])
def test_online_flagship_setup_and_whole_turn_moves_followers(online_formation, turn_mode):
    client, service, root, judge, players = online_formation
    commanders = command_players(client, root, players)
    for player in players:
        view = seat_view(client, root, player)
        assert view["realistic_command"] and view["phase"] == "formation_setup"
        if player in commanders:
            assert len(view["own_ship_ids"]) == 1
            assert view["formation_reports"] == []
        else:
            assert view["own_ship_ids"] == [] and view["ships"] == []
        assert len(view["formations"]) == 1
        metadata = view["formations"][0]
        assert "ship_ids" not in metadata and "guide_trail" not in metadata
        enemy_ids = {
            ship_id for ship_id, ship in service.engine.get(client.get(root, headers=judge).json()["match"]["game_id"]).ships.items()
            if ship.side.value != player[0]["side"]
        }
        assert not any(ship_id in str(metadata) for ship_id in enemy_ids)
    for player in commanders:
        response = submit(client, root, player, mode=turn_mode)
        assert response.status_code == 200, response.text
    approve_and_publish(client, root, judge)
    full = client.get(f"{root}/judge/view", headers=judge).json()
    assert full["phase"] == "reinforcement"
    assert len(full["formations"]) == 2
    assert all(formation["turn_mode"] == turn_mode for formation in full["formations"])
    before = {ship["id"]: ship["position"] for ship in full["ships"]}
    movement_mode = "synchronized" if turn_mode == "column_follow" else "column_follow"
    for player in commanders:
        response = submit(client, root, player, plan="3", mode=movement_mode)
        assert response.status_code == 200, response.text
    submitted = service.repository.orders(client.get(root, headers=judge).json()["match"]["match_id"], Phase.REINFORCEMENT)
    assert all(batch.formation_movement[0].turn_mode == movement_mode for _request_id, batch in submitted.values())
    approve_and_publish(client, root, judge)
    full = client.get(f"{root}/judge/view", headers=judge).json()
    assert full["turn"] == 2
    assert all(ship["position"] != before[ship["id"]] for ship in full["ships"])
    for player in players:
        view = seat_view(client, root, player)
        assert view["turn"] == 2
        if player in commanders:
            assert view["formation_reports"]
            assert all(report["turn"] == 1 for report in view["formation_reports"])
        else:
            assert view["formation_reports"] == []


def test_judge_can_reassign_flagship_and_recalculate_required_seats(online_formation):
    client, service, root, judge, players = online_formation
    state = client.get(f"{root}/judge/view", headers=judge).json()
    axis_ids = [ship["id"] for ship in state["ships"] if ship["side"] == "axis"]
    assert len(axis_ids) == 2
    new_flagship_id, reserve_id = axis_ids[1], axis_ids[0]
    response = client.put(f"{root}/judge/formations", headers=judge, json={
        "side": "axis", "reason": "test reassignment", "expected_version": state["version"],
        "formations": [{
            "formation_id": "axis-reorganized", "name": "Axis group", "ship_ids": axis_ids,
            "flagship_id": new_flagship_id, "reserve_flagship_id": reserve_id,
            "spacing": 1, "turn_mode": "synchronized",
        }],
    })
    assert response.status_code == 200, response.text
    active = service.repository.match(root.rsplit("/", 1)[1]).required_seat_ids
    expected = {
        player[0]["seat_id"] for player in players
        if player[0]["controlled_ship_ids"][0] in {new_flagship_id}
        or (player[0]["side"] == "allies" and player[0]["controlled_ship_ids"][0] == next(
            formation["flagship_id"] for formation in state["formations"] if formation["side"] == "allies"
        ))
    }
    assert set(active) == expected
    assert response.json()["override"]["kind"] == "formation_composition"


def test_follower_cannot_send_flagship_commands_even_with_validation_override(online_formation):
    client, _, root, _, players = online_formation
    flagship = next(player for player in players if seat_view(client, root, player)["formations"][0]["role"] == "flagship")
    follower = next(player for player in players if player[0]["side"] == flagship[0]["side"] and player != flagship)
    token = seat_view(client, root, flagship)["formations"][0]["formation_id"]
    response = submit(client, root, follower, commands=[{"formation_id": token}])
    assert response.status_code == 403
    seat, headers = follower
    raw = client.post(f"{root}/seats/{seat['seat_id']}/orders", headers=headers, json={
        "phase": "formation_setup", "batch": {"side": seat["side"], "phase": "formation_setup", "formation_movement": [{"formation_id": "axis-column"}]},
        "request_id": "bypass", "ignore_validation_errors": True,
    })
    assert raw.status_code == 403


def test_judge_can_override_flagship_setup_and_player_preview_uses_formation_route(online_formation):
    client, service, root, judge, players = online_formation
    commanders = command_players(client, root, players)
    for player in commanders:
        assert submit(client, root, player).status_code == 200
    state = client.get(f"{root}/judge/view", headers=judge).json()
    formation = state["formations"][0]
    flagship = next(player for player in players if formation["flagship_id"] in player[0]["controlled_ship_ids"])
    response = client.post(f"{root}/judge/seats/{flagship[0]['seat_id']}/orders", headers=judge, json={
        "phase": "formation_setup", "batch": {"side": flagship[0]["side"], "phase": "formation_setup"},
        "formation_commands": [{"formation_id": formation["formation_id"], "turn_mode": "synchronized"}],
        "reason": "test formation editor", "expected_version": state["version"],
    })
    assert response.status_code == 200, response.text
    approve_and_publish(client, root, judge)
    view = seat_view(client, root, flagship)
    command = {"formation_id": view["formations"][0]["formation_id"], "leader_plan": "3", "spacing": 1}
    preview = client.post(f"{root}/seats/{flagship[0]['seat_id']}/torpedo-candidates", headers=flagship[1], json={
        "movement": [], "formation_commands": [command],
    })
    assert preview.status_code == 200, preview.text
    assert preview.json()
    group_ship_ids = set(view["own_ship_ids"]) | {ship["id"] for group in view["formation_roster"] for ship in group["ships"]}
    assert {item["ship_id"] for item in preview.json()} <= group_ship_ids
    assert {point["mf"] for item in preview.json() for point in item["launch_positions"]} == {0, 1, 2, 3}
    state = service.engine.get(client.get(root, headers=judge).json()["match"]["game_id"])
    formation = next(item for item in state.formations.values() if item.flagship_id in flagship[0]["controlled_ship_ids"])
    follower_id = next(ship_id for ship_id in formation.ship_ids if ship_id != formation.flagship_id)
    leader_order = GunneryOrder(ship_id=formation.flagship_id, primary_target="primary-contact")
    inherited = service._inherit_formation_actions(state, OrderBatch(side=flagship[0]["side"], gunnery=[leader_order]))
    # 2026-10-20 裁判裁决：旗舰不再把炮击意图下传给队员。
    assert {item.ship_id for item in inherited.gunnery} == {formation.flagship_id}
    override_order = GunneryOrder(ship_id=follower_id, primary_target="independent-contact")
    overridden = service._inherit_formation_actions(
        state, OrderBatch(side=flagship[0]["side"], gunnery=[leader_order, override_order]),
    )
    assert {item.ship_id: item.primary_target for item in overridden.gunnery} == {
        formation.flagship_id: "primary-contact",
        follower_id: "independent-contact",
    }


def test_formation_light_orders_keep_the_selected_emitter_and_explicit_member_override(online_formation):
    _client, service, root, _judge, _players = online_formation
    state = service.engine.get(service.repository.match(root.rsplit("/", 1)[-1]).game_id)
    state.phase = Phase.GUNNERY
    leader_id, follower_id = [ship.id for ship in state.ships.values() if ship.side == Side.AXIS][:2]
    formation = FormationState(
        id="axis-test-formation", name="axis test", side=Side.AXIS,
        ship_ids=[leader_id, follower_id], leader_id=leader_id, flagship_id=leader_id,
        reserve_flagship_id=follower_id, heading=1,
    )
    state.formations[formation.id] = formation
    state.ships[leader_id].formation_id = formation.id
    state.ships[follower_id].formation_id = formation.id

    inherited = service._inherit_formation_actions(state, OrderBatch(
        side=Side.AXIS,
        illumination=[IlluminationOrder(ship_id=leader_id, target_hex=HexCoord(q=4, r=7))],
        searchlights=[SearchlightOrder(ship_id=leader_id, target_id="contact-a")],
    ))
    # 2026-10-20 裁判裁决：照明/探照灯不再下传给队员。
    assert {item.ship_id for item in inherited.illumination} == {leader_id}
    assert {item.ship_id for item in inherited.searchlights} == {leader_id}

    explicit = service._inherit_formation_actions(state, OrderBatch(
        side=Side.AXIS,
        illumination=[
            IlluminationOrder(ship_id=leader_id, target_hex=HexCoord(q=4, r=7)),
            IlluminationOrder(ship_id=follower_id, target_hex=HexCoord(q=8, r=2)),
        ],
        searchlights=[
            SearchlightOrder(ship_id=leader_id, target_id="contact-a"),
            SearchlightOrder(ship_id=follower_id, target_id="contact-b"),
        ],
    ))
    assert len(explicit.illumination) == 2
    assert next(item for item in explicit.illumination if item.ship_id == follower_id).target_hex == HexCoord(q=8, r=2)
    assert len(explicit.searchlights) == 2
    assert next(item for item in explicit.searchlights if item.ship_id == follower_id).target_id == "contact-b"


def test_formation_does_not_share_vision_or_accept_individual_movement(online_formation):
    client, service, root, judge, players = online_formation
    commanders = command_players(client, root, players)
    for player in commanders:
        response = submit(client, root, player)
        assert response.status_code == 200, response.text
    approve_and_publish(client, root, judge)
    flagship = next(player for player in players if seat_view(client, root, player)["formations"][0]["role"] == "flagship")
    follower = next(player for player in players if player[0]["side"] == flagship[0]["side"] and player != flagship)
    enemy = next(player for player in players if player[0]["side"] != flagship[0]["side"])
    game_id = client.get(root, headers=judge).json()["match"]["game_id"]
    state = service.engine.get(game_id)
    state.visibility = {"axis": 2, "allies": 2}
    state.options.optional_rules.hidden_contacts = True
    state.ships[flagship[0]["controlled_ship_ids"][0]].position = HexCoord.from_label("H10")
    state.ships[follower[0]["controlled_ship_ids"][0]].position = HexCoord.from_label("H20")
    state.ships[enemy[0]["controlled_ship_ids"][0]].position = HexCoord.from_label("I20")
    view = seat_view(client, root, flagship)
    assert len(view["ships"]) == 1
    assert view["ships"][0]["id"] == view["own_ship_ids"][0]
    follower_view = seat_view(client, root, follower)
    assert follower_view["formations"][0]["heading"] is None
    assert follower_view["own_ship_ids"] == [] and follower_view["ships"] == []
    raw = client.post(f"{root}/seats/{follower[0]['seat_id']}/orders", headers=follower[1], json={
        "phase": "reinforcement", "batch": {"side": follower[0]["side"], "phase": "reinforcement",
            "movement": [{"ship_id": follower[0]["controlled_ship_ids"][0], "plan": "3"}]},
        "scope": "turn", "request_id": "individual-follower", "ignore_validation_errors": True,
    })
    assert raw.status_code == 403


def test_missing_flagship_route_is_reported_on_last_side_submission(online_formation):
    client, _, root, judge, players = online_formation
    commanders = command_players(client, root, players)
    for player in commanders:
        assert submit(client, root, player).status_code == 200
    approve_and_publish(client, root, judge)
    flagship = next(player for player in players if seat_view(client, root, player)["formations"][0]["role"] == "flagship")
    follower = next(player for player in players if player[0]["side"] == flagship[0]["side"] and player != flagship)
    assert submit(client, root, follower, commands=[]).status_code == 403
    response = submit(client, root, flagship, commands=[])
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "order_validation"
    assert submit(client, root, flagship, plan="3").status_code == 200
    follower = next(player for player in players if player[0]["side"] == flagship[0]["side"] and player != flagship)
    follower_view = seat_view(client, root, follower)
    assert follower_view["own_ship_ids"] == []
    assert follower_view["formation_reports"] == []
