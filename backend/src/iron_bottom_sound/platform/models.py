from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from ..models import GameEvent, MovementOrder, OrderBatch, Phase, PublicShip, Side, TorpedoTrack, WreckState, MarkerState

TELEGRAM_RULE_ID = "IBS-R-COMMS-01"


class ControllerType(StrEnum):
    HUMAN = "human"
    AI = "ai"
    REFEREE_PROXY = "referee_proxy"


class MatchStage(StrEnum):
    COLLECTING = "collecting"
    READY_FOR_REVIEW = "ready_for_review"
    RESOLVING = "resolving"
    AWAITING_PUBLICATION = "awaiting_publication"
    COMPLETE = "complete"


class UserRecord(BaseModel):
    user_id: str
    username: str
    password_hash: str
    is_referee: bool = False
    is_observer: bool = False
    disabled: bool = False


class SeatRecord(BaseModel):
    seat_id: str
    match_id: str
    user_id: str | None = None
    username: str | None = None
    credential_password: str | None = None
    side: Side
    controlled_ship_ids: list[str] = Field(default_factory=list)
    controller_type: ControllerType = ControllerType.HUMAN


class MatchRecord(BaseModel):
    match_id: str
    game_id: str
    scenario_id: str
    judge_user_id: str
    phase: Phase
    stage: MatchStage = MatchStage.COLLECTING
    version: int = 0
    required_seat_ids: list[str] = Field(default_factory=list)


class SeatObservation(BaseModel):
    match_id: str
    seat_id: str
    side: Side
    turn: int
    max_turns: int
    phase: Phase
    stage: MatchStage
    version: int
    scenario_id: str
    scenario_title: str
    map_columns: int
    map_rows: int
    printed_columns: int | None = None
    printed_rows: int | None = None
    map_infinite: bool = False
    visibility: int
    # Judge-only base radii. Player observations leave these unset so the
    # opposing side's configured radius is never exposed.
    visibility_axis: int | None = None
    visibility_allies: int | None = None
    storm: bool = False
    optional_rules: dict[str, bool] = Field(default_factory=dict)
    realistic_command: bool = False
    # Seat entries contain command metadata only, never other hull identities,
    # positions, status or trails. Referee entries retain the complete roster.
    formations: list[dict[str, Any]] = Field(default_factory=list)
    # Fleet-mate reports come from the previous completed turn and are not
    # included in ships/map observation or any current visibility calculation.
    formation_reports: list[dict[str, Any]] = Field(default_factory=list)
    formation_roster: list[dict[str, Any]] = Field(default_factory=list)
    ships: list[PublicShip] = Field(default_factory=list)
    # Populated only by judge_observation; never include authoritative hidden
    # state in a player seat response.
    referee_ship_states: list[dict[str, Any]] = Field(default_factory=list)
    previous_gunnery_targets: dict[str, str] = Field(default_factory=dict)
    torpedo_tracks: list[TorpedoTrack] = Field(default_factory=list)
    markers: list[MarkerState] = Field(default_factory=list)
    wrecks: list[WreckState] = Field(default_factory=list)
    score: dict[str, int] = Field(default_factory=dict)
    recent_events: list[GameEvent] = Field(default_factory=list)
    rerollable_events: list[GameEvent] = Field(default_factory=list)
    adjudication_turn: int | None = None
    # Keys use the seat's public ship id (real id for owned ships, anonymous
    # alias for contacts); points are already clipped to this seat's view.
    ship_trajectories: dict[str, list[str]] = Field(default_factory=dict)
    own_ship_ids: list[str] = Field(default_factory=list)
    visible_enemy_ship_ids: list[str] = Field(default_factory=list)
    submitted_seat_count: int
    required_seat_count: int
    seat_has_submitted: bool = False
    winner: Side | None = None
    victory_reason: str | None = None
    # Derived only from the seat's own ships and sealed movement plans.  This
    # keeps torpedo controls aligned with the engine without exposing hidden
    # enemy state.
    torpedo_candidates: list[dict[str, Any]] = Field(default_factory=list)
    # Engine-derived gun/target legality, filtered to this seat's visible
    # contacts and rewritten to their anonymous public ids.
    gunnery_candidates: list[dict[str, Any]] = Field(default_factory=list)


class SeatFormationCommand(BaseModel):
    formation_id: str = Field(min_length=1, max_length=64)
    leader_plan: str = "0"
    spacing: int = Field(default=1, ge=1, le=2)
    heading: int | None = Field(default=None, ge=1, le=6)
    turn_mode: str = Field(default="column_follow", pattern="^(column_follow|synchronized)$")
    reduce_speed: int | None = Field(default=None, ge=0, le=8)


class SeatOrderSubmission(BaseModel):
    phase: Phase
    batch: OrderBatch
    scope: str = Field(default="phase", pattern="^(phase|turn)$")
    request_id: str = Field(min_length=1, max_length=120)
    ignore_validation_errors: bool = False
    formation_commands: list[SeatFormationCommand] = Field(default_factory=list)


class TorpedoCandidatePreviewRequest(BaseModel):
    movement: list[MovementOrder] = Field(default_factory=list)
    formation_commands: list[SeatFormationCommand] = Field(default_factory=list)


class TelegramSubmission(BaseModel):
    message: str = Field(min_length=1, max_length=10000)


class TelegramDelivery(BaseModel):
    rule_id: str = TELEGRAM_RULE_ID
    telegram_id: str
    sent_turn: int
    received_turn: int
    sender: str
    message: str | None = None
    delivered: bool
    char_count: int
    die_sides: int
    roll: int
    hit_count: int = 0
    adjusted_roll: int = 1


class PlayerTelegramDelivery(BaseModel):
    telegram_id: str
    sent_turn: int
    received_turn: int
    sender: str
    message: str | None = None
    delivered: bool


class TelegramInbox(BaseModel):
    turn: int
    sent_turn: int
    sent_message: str | None = None
    messages: list[PlayerTelegramDelivery] = Field(default_factory=list)


class JudgeTelegramDelivery(TelegramDelivery):
    sender_seat_id: str
    sender_username: str | None = None
    recipient_seat_id: str
    recipient_username: str | None = None
    side: Side


class JudgeOrderOverrideRequest(BaseModel):
    phase: Phase
    batch: OrderBatch
    scope: str = Field(default="phase", pattern="^(phase|turn)$")
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int = Field(ge=0)
    formation_commands: list[SeatFormationCommand] = Field(default_factory=list)


class JudgeShipPatchRequest(BaseModel):
    patch: dict[str, Any] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int = Field(ge=0)


class JudgeFormationGroup(BaseModel):
    formation_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=40)
    ship_ids: list[str] = Field(min_length=2)
    flagship_id: str
    reserve_flagship_id: str
    spacing: int = Field(default=1, ge=1, le=2)
    turn_mode: str = Field(default="column_follow", pattern="^(column_follow|synchronized)$")


class JudgeFormationCompositionRequest(BaseModel):
    side: Side
    formations: list[JudgeFormationGroup] = Field(min_length=1, max_length=8)
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int = Field(ge=0)


class JudgeVisibilityPatchRequest(BaseModel):
    axis: int = Field(ge=0, le=99)
    allies: int = Field(ge=0, le=99)
    storm: bool = False
    hidden_contacts: bool | None = None
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int = Field(ge=0)


class JudgeRerollRequest(BaseModel):
    event_sequence: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int = Field(ge=0)


class JudgeOverrideRecord(BaseModel):
    override_id: str
    match_id: str
    kind: str
    target: str
    reason: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ApprovalRequest(BaseModel):
    expected_version: int = Field(ge=0)
    note: str | None = Field(default=None, max_length=500)


class RejectionRequest(BaseModel):
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    token: str
    user_id: str
    username: str
    is_referee: bool
    is_observer: bool = False


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=256)
    is_referee: bool = False


class MatchCreateRequest(BaseModel):
    scenario_id: str = "IBS-S-03"
    seed: int = 1
    judge_user_id: str
    realistic_command: bool | None = None


class SeatCreateRequest(BaseModel):
    username: str | None = None
    side: Side
    controlled_ship_ids: list[str] = Field(min_length=1)
    controller_type: ControllerType = ControllerType.HUMAN


class MatchSummary(BaseModel):
    match: MatchRecord
    seats: list[SeatRecord]


class ApprovalResult(BaseModel):
    match: MatchRecord
    events: list[GameEvent] = Field(default_factory=list)


class OrderStatus(BaseModel):
    seat_id: str
    submitted: bool
    phase: Phase
    request_id: str | None = None
    batch: OrderBatch | None = None
    errors: list[str] = Field(default_factory=list)
