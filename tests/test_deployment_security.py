from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from iron_bottom_sound import api
from iron_bottom_sound.engine import IronBottomEngine
from iron_bottom_sound.platform.bootstrap_user import create_initial_referee
from iron_bottom_sound.platform.repository import PlatformRepository
from iron_bottom_sound.platform.router import build_router
from iron_bottom_sound.platform.service import PlatformService
from iron_bottom_sound.storage import GameRepository


def test_initial_referee_is_bootstrapped_offline_and_only_once(tmp_path) -> None:
    database = tmp_path / "bootstrap.sqlite3"

    user = create_initial_referee(database, "referee", "strong-password")

    repository = PlatformRepository(database)
    assert repository.user_by_username("referee").password_hash != "strong-password"
    assert user.is_referee
    try:
        create_initial_referee(database, "second", "another-password")
    except ValueError as error:
        assert "already exists" in str(error)
    else:
        raise AssertionError("bootstrap must refuse to replace an existing account")


def test_public_user_creation_cannot_claim_first_referee(tmp_path) -> None:
    service = PlatformService(IronBottomEngine(), GameRepository(tmp_path / "api.sqlite3"))
    app = FastAPI()
    app.include_router(build_router(service))

    response = TestClient(app).post(
        "/platform/users",
        json={"username": "attacker", "password": "strong-password", "is_referee": True},
    )

    assert response.status_code == 401
    assert not service.repository.has_users()


def test_legacy_game_routes_cannot_read_platform_match(monkeypatch, tmp_path) -> None:
    database = tmp_path / "private-match.sqlite3"
    engine = IronBottomEngine()
    repository = GameRepository(database)
    service = PlatformService(engine, repository)
    judge = service.create_user("judge", "strong-password", is_referee=True)
    match = service.create_match("IBS-S-03", 17, judge.user_id)

    monkeypatch.setattr(api, "engine", engine)
    monkeypatch.setattr(api, "repository", repository)
    monkeypatch.setattr(api, "platform_service", service)
    client = TestClient(api.app)

    response = client.get(
        f"/games/{match.game_id}/view?debug=true",
        headers={"X-Player-Side": "axis"},
    )
    proxied_response = client.get(
        f"/api/games/{match.game_id}/view?debug=true",
        headers={"X-Player-Side": "axis"},
    )
    summaries = client.get("/games").json()
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(f"/games/{match.game_id}?side=axis"):
            pass
    assert error.value.code == 4404

    assert response.status_code == 404
    assert proxied_response.status_code == 404
    assert match.game_id not in {item["game_id"] for item in summaries}
