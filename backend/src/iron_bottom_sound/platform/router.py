from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import Response

from ..savegame import build_save_bundle
from ..ship_imports import ShipImportEnvelope, ShipImportRequest

from .models import (
    ApprovalRequest,
    ControllerType,
    JudgeOrderOverrideRequest,
    JudgeRerollRequest,
    JudgeShipPatchRequest,
    JudgeVisibilityPatchRequest,
    LoginRequest,
    LoginResponse,
    MatchCreateRequest,
    RejectionRequest,
    SeatCreateRequest,
    SeatOrderSubmission,
    TelegramSubmission,
    UserCreateRequest,
)
from .service import OrderValidationIssue, PlatformService


def build_router(service: PlatformService) -> APIRouter:
    router = APIRouter(prefix="/platform", tags=["platform"])

    def user_from_header(authorization: str | None):
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required")
        user = service.authenticate(authorization[7:].strip())
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")
        return user

    def require_referee(authorization: str | None):
        user = user_from_header(authorization)
        if not user.is_referee:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Referee role required")
        return user

    @router.post("/auth/login", response_model=LoginResponse)
    def login(request: LoginRequest):
        issued = service.issue_token(request.username, request.password)
        if issued is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
        token, user = issued
        return LoginResponse(token=token, user_id=user.user_id, username=user.username, is_referee=user.is_referee)

    @router.get("/ships/import-schema")
    def ship_import_schema(authorization: Annotated[str | None, Header()] = None):
        require_referee(authorization)
        return ShipImportRequest.model_json_schema()

    @router.post("/ships/import", status_code=201)
    def import_ship(
        request: ShipImportEnvelope,
        authorization: Annotated[str | None, Header()] = None,
    ):
        require_referee(authorization)
        try:
            return service.import_ship(request)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/users")
    def create_user(
        request: UserCreateRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        require_referee(authorization)
        try:
            user = service.create_user(request.username, request.password, is_referee=request.is_referee)
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"user_id": user.user_id, "username": user.username, "is_referee": user.is_referee}

    @router.post("/matches")
    def create_match(
        request: MatchCreateRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        if request.judge_user_id != user.user_id:
            raise HTTPException(status_code=403, detail="A referee may create only their own matches")
        try:
            return service.create_match(request.scenario_id, request.seed, request.judge_user_id).model_dump(mode="json")
        except (KeyError, ValueError, PermissionError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/matches/{match_id}")
    def match_summary(match_id: str, authorization: Annotated[str | None, Header()] = None):
        user = user_from_header(authorization)
        summary = service.summary(match_id)
        allowed = user.is_referee and user.user_id == summary.match.judge_user_id
        if not allowed and not any(seat.user_id == user.user_id for seat in summary.seats):
            raise HTTPException(status_code=403, detail="You are not assigned to this match")
        if not allowed:
            # A player learns only their own seat binding; other seat/user
            # assignments are referee information and must not become a map
            # for same-side discovery.
            summary = summary.model_copy(
                update={
                    "match": service.player_match_record(summary.match),
                    "seats": [
                        seat.model_copy(update={"credential_password": None})
                        for seat in summary.seats
                        if seat.user_id == user.user_id
                    ]
                }
            )
        return summary.model_dump(mode="json")

    @router.post("/matches/{match_id}/seats")
    def create_seat(
        match_id: str,
        request: SeatCreateRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        summary = service.summary(match_id)
        if summary.match.judge_user_id != user.user_id:
            raise HTTPException(status_code=403, detail="Only the assigned referee may add seats")
        user_id = None
        if request.username:
            account = service.repository.user_by_username(request.username)
            if account is None:
                raise HTTPException(status_code=404, detail="Unknown seat user")
            user_id = account.user_id
        try:
            seat = service.add_seat(
                match_id,
                user_id=user_id,
                side=request.side,
                controlled_ship_ids=request.controlled_ship_ids,
                controller_type=request.controller_type,
            )
        except (KeyError, ValueError, PermissionError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return seat.model_dump(mode="json")

    @router.get("/matches/{match_id}/seats/{seat_id}/view")
    def seat_view(
        match_id: str,
        seat_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = user_from_header(authorization)
        try:
            return service.observation(match_id, seat_id, user).model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @router.get("/matches/{match_id}/judge/view")
    def judge_view(
        match_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        try:
            return service.judge_observation(match_id, user).model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @router.get("/matches/{match_id}/turn-archives")
    def turn_archives(
        match_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        try:
            return service.turn_archives(match_id, user)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @router.get("/matches/{match_id}/turn-archives/{turn}")
    def download_turn_archive(
        match_id: str,
        turn: int,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        try:
            state = service.turn_archive_state(match_id, turn, user)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        custom_scenario = None
        if state.scenario_id.startswith("IBS-CUSTOM-"):
            try:
                custom_scenario = service.game_repository.custom_scenario(state.scenario_id)
            except KeyError as error:
                raise HTTPException(409, "该对局的自定义想定定义已丢失，无法生成可移植存档") from error
        bundle = build_save_bundle(service.game_repository, state, custom_scenario)
        filename = f"iron-bottom-sound-{match_id}-turn-{turn:02d}.ibs-save.json"
        return Response(
            content=json.dumps(bundle, ensure_ascii=False, separators=(",", ":")),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.post("/matches/{match_id}/seats/{seat_id}/orders")
    def submit_order(
        match_id: str,
        seat_id: str,
        request: SeatOrderSubmission,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = user_from_header(authorization)
        try:
            match = service.submit_order(match_id, seat_id, user, request)
        except OrderValidationIssue as error:
            raise HTTPException(
                status_code=422,
                detail={"code": "order_validation", "errors": error.errors},
            ) from error
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return match.model_dump(mode="json")

    @router.post("/matches/{match_id}/seats/{seat_id}/telegrams")
    def submit_telegram(
        match_id: str,
        seat_id: str,
        request: TelegramSubmission,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = user_from_header(authorization)
        try:
            return service.submit_telegram(match_id, seat_id, user, request)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.get("/matches/{match_id}/seats/{seat_id}/telegrams")
    def telegram_inbox(
        match_id: str,
        seat_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = user_from_header(authorization)
        try:
            return service.telegram_inbox(match_id, seat_id, user)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @router.get("/matches/{match_id}/review")
    def review(
        match_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        summary = service.summary(match_id)
        if summary.match.judge_user_id != user.user_id:
            raise HTTPException(status_code=403, detail="Only the assigned referee may review")
        orders = service.repository.orders(match_id, summary.match.phase)
        return {
            "match": summary.match.model_dump(mode="json"),
            "orders": [
                {
                    "seat_id": seat_id,
                    "request_id": request_id,
                    "scope": service.repository.order_scope(match_id, summary.match.phase) or "phase",
                    "batch": batch.model_dump(mode="json"),
                    "validation_errors": service.repository.order_validation_errors(
                        match_id, summary.match.phase
                    ).get(seat_id, []),
                }
                for seat_id, (request_id, batch) in orders.items()
            ],
        }

    @router.get("/matches/{match_id}/judge/telegrams")
    def judge_telegrams(
        match_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        try:
            return service.judge_telegrams(match_id, user)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @router.post("/matches/{match_id}/judge/seats/{seat_id}/orders")
    def judge_replace_order(
        match_id: str,
        seat_id: str,
        request: JudgeOrderOverrideRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        try:
            match = service.judge_replace_order(match_id, seat_id, user, request)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return match.model_dump(mode="json")

    @router.patch("/matches/{match_id}/judge/ships/{ship_id}")
    def judge_patch_ship(
        match_id: str,
        ship_id: str,
        request: JudgeShipPatchRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        try:
            return service.judge_patch_ship(match_id, ship_id, user, request)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.patch("/matches/{match_id}/judge/visibility")
    def judge_patch_visibility(
        match_id: str,
        request: JudgeVisibilityPatchRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        try:
            return service.judge_patch_visibility(match_id, user, request)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/matches/{match_id}/judge/reroll")
    def judge_reroll(
        match_id: str,
        request: JudgeRerollRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        try:
            return service.judge_reroll(match_id, user, request)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/matches/{match_id}/judge/publish")
    def publish_adjudication(
        match_id: str,
        request: ApprovalRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        try:
            return service.publish_adjudication(match_id, user, request.expected_version).model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.get("/matches/{match_id}/judge/overrides")
    def judge_overrides(
        match_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        try:
            return [record.model_dump(mode="json") for record in service.judge_overrides(match_id, user)]
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @router.post("/matches/{match_id}/reject")
    def reject(
        match_id: str,
        request: RejectionRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        try:
            match = service.reject(match_id, user, request.expected_version, request.reason)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return match.model_dump(mode="json")

    @router.post("/matches/{match_id}/approve")
    def approve(
        match_id: str,
        request: ApprovalRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        user = require_referee(authorization)
        try:
            result = service.approve(match_id, user, request.expected_version, request.note)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return result.model_dump(mode="json")

    return router
