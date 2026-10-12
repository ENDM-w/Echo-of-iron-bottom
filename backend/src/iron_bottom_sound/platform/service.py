from __future__ import annotations

import re
import secrets
import sqlite3
import uuid
from copy import copy
from ..engine import ORDER_PHASES, IronBottomEngine
from ..models import DiceRoll, GameEvent, GameOptions, GameState, HexCoord, MarkerState, MovementOrder, OrderBatch, Phase, PublicShip, ShipState, Side
from ..storage import GameRepository
from ..ship_imports import ShipImportEnvelope
from ..ship_records import clear_imported_ship_asset_cache, load_ship_catalog, load_ship_records
from .auth import AuthenticatedUser, PasswordHasher, SessionStore
from .models import (
    ApprovalResult,
    ControllerType,
    JudgeOrderOverrideRequest,
    JudgeRerollRequest,
    JudgeShipPatchRequest,
    JudgeVisibilityPatchRequest,
    TelegramSubmission,
    TELEGRAM_RULE_ID,
    MatchRecord,
    MatchStage,
    MatchSummary,
    SeatObservation,
    SeatOrderSubmission,
    SeatRecord,
    UserRecord,
)


class OrderValidationIssue(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("；".join(errors))
from .repository import PlatformRepository


ANONYMOUS_COUNTERS = {
    "destroyer": "DD标记.png",
    "cruiser": "CA-CL标记.png",
    "battleship": "BB标记.png",
}
ANONYMOUS_PREFIXES = {
    "destroyer": "驱逐",
    "cruiser": "巡洋",
    "battleship": "战列",
}
ANONYMOUS_SUFFIXES = tuple("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥天地玄黄")
DEFAULT_PLATFORM_VISIBILITY = {
    Side.AXIS.value: 14,
    Side.ALLIES.value: 12,
}


def _anonymous_scale(ship_type: str) -> str:
    if ship_type in {"BB", "BC"}:
        return "battleship"
    if ship_type in {"CA", "CL", "CB", "AV"}:
        return "cruiser"
    return "destroyer"


def _anonymous_suffix(index: int) -> str:
    if index < len(ANONYMOUS_SUFFIXES):
        return ANONYMOUS_SUFFIXES[index]
    return f"{index + 1}"


class PlatformService:
    """Seat-aware orchestration around :class:`IronBottomEngine`.

    The engine remains authoritative.  This service never resolves a partial
    order set and never exposes the engine's full GameState to a player.
    """

    def __init__(self, engine: IronBottomEngine, game_repository: GameRepository) -> None:
        self.engine = engine
        self.game_repository = game_repository
        self.repository = PlatformRepository(game_repository.path)
        self.sessions = SessionStore()

    def create_user(self, username: str, password: str, *, is_referee: bool = False) -> UserRecord:
        user = UserRecord(
            user_id=str(uuid.uuid4()),
            username=username,
            password_hash=PasswordHasher.hash(password),
            is_referee=is_referee,
        )
        return self.repository.create_user(user)

    def issue_token(self, username: str, password: str) -> tuple[str, UserRecord] | None:
        user = self.repository.user_by_username(username)
        if user is None or user.disabled or not PasswordHasher.verify(password, user.password_hash):
            return None
        return self.sessions.issue(user), user

    def authenticate(self, token: str | None) -> AuthenticatedUser | None:
        return self.sessions.get(token)

    def import_ship(self, envelope: ShipImportEnvelope) -> dict:
        request = envelope.definition
        ship_id = request.record.id
        if ship_id in load_ship_records() or ship_id in load_ship_catalog():
            raise ValueError(f"舰船 ID 已存在，不能覆盖：{ship_id}")
        asset_mime, asset_data = envelope.image_payload()
        try:
            self.game_repository.save_imported_ship(
                ship_id, request.catalog_payload(), request.record_payload(), asset_mime, asset_data
            )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"舰船 ID 已存在，不能覆盖：{ship_id}") from error
        clear_imported_ship_asset_cache()
        return {
            "id": ship_id,
            "name": request.record.name,
            "ship_type": request.record.ship_type,
            "asset": f"db:{ship_id}",
            "complete": True,
        }

    def create_match(self, scenario_id: str, seed: int, judge_user_id: str) -> MatchRecord:
        judge = self.repository.user_by_id(judge_user_id)
        if judge is None or not judge.is_referee:
            raise PermissionError("judge_user_id must reference a referee account")
        state = self.engine.reset(scenario_id, seed, options=GameOptions(mode="hotseat"))
        # Platform matches use the public multiplayer defaults; standalone
        # engine scenarios retain their scenario-authored visibility values.
        state.visibility = dict(DEFAULT_PLATFORM_VISIBILITY)
        self.game_repository.save(state)
        match = MatchRecord(
            match_id=str(uuid.uuid4()),
            game_id=state.game_id,
            scenario_id=scenario_id,
            judge_user_id=judge_user_id,
            phase=state.phase,
        )
        self.repository.save_match(match)
        assignments = self._scenario_seat_assignments(scenario_id)
        # Legacy/built-in scenarios remain loadable. New scenarios authored in
        # the workshop carry assignments and are materialized automatically.
        if not assignments:
            return match
        usernames: set[str] = set()
        for ship in state.ships.values():
            assignment = assignments.get(ship.id)
            if assignment is None:
                raise ValueError(f"Scenario is missing credentials for ship {ship.id}")
            username = str(assignment.get("username") or "").strip()
            password = str(assignment.get("password") or "")
            if not username or len(password) < 8:
                raise ValueError(f"Invalid credentials for ship {ship.id}")
            if username in usernames:
                raise ValueError(f"Duplicate seat username: {username}")
            usernames.add(username)
            user = self._provision_seat_user(username, password)
            self.add_seat(
                match.match_id,
                user_id=user.user_id,
                side=ship.side,
                controlled_ship_ids=[ship.id],
                credential_password=password,
            )
        return self.repository.match(match.match_id)

    def _scenario_seat_assignments(self, scenario_id: str) -> dict[str, dict[str, str]]:
        try:
            definition = self.game_repository.custom_scenario(scenario_id)
        except KeyError:
            return {}
        raw = definition.get("seat_assignments") or []
        return {
            str(item.get("ship_id")): item
            for item in raw
            if isinstance(item, dict) and item.get("ship_id")
        }

    def _provision_seat_user(self, username: str, password: str) -> UserRecord:
        existing = self.repository.user_by_username(username)
        if existing is not None:
            if existing.is_referee or not PasswordHasher.verify(password, existing.password_hash):
                raise ValueError(f"Seat username already belongs to another account: {username}")
            return existing
        return self.create_user(username, password)

    def add_seat(
        self,
        match_id: str,
        *,
        user_id: str | None,
        side: Side,
        controlled_ship_ids: list[str],
        controller_type: ControllerType = ControllerType.HUMAN,
        credential_password: str | None = None,
    ) -> SeatRecord:
        match = self.repository.match(match_id)
        self._ensure_loaded(match.game_id)
        state = self.engine.get(match.game_id)
        if not controlled_ship_ids:
            raise ValueError("A seat must control at least one ship")
        ships = [state.ships.get(ship_id) for ship_id in controlled_ship_ids]
        if any(ship is None for ship in ships):
            raise ValueError("Seat references an unknown ship")
        if any(ship.side != side for ship in ships if ship is not None):
            raise ValueError("All ships in a seat must belong to the selected side")
        existing = self.repository.seats(match_id)
        occupied = {ship_id for seat in existing for ship_id in seat.controlled_ship_ids}
        duplicates = occupied.intersection(controlled_ship_ids)
        if duplicates:
            raise ValueError(f"Ships already assigned to another seat: {sorted(duplicates)}")
        if controller_type == ControllerType.HUMAN:
            if user_id is None or self.repository.user_by_id(user_id) is None:
                raise ValueError("A human seat requires an existing user")
        if user_id is not None and any(seat.user_id == user_id for seat in existing):
            raise ValueError("A user may occupy only one seat in a match")
        seat = SeatRecord(
            seat_id=str(uuid.uuid4()),
            match_id=match_id,
            user_id=user_id,
            credential_password=credential_password,
            side=side,
            controlled_ship_ids=controlled_ship_ids,
            controller_type=controller_type,
        )
        self.repository.create_seat(seat)
        match.required_seat_ids.append(seat.seat_id)
        match.version += 1
        self.repository.save_match(match)
        return seat

    def summary(self, match_id: str) -> MatchSummary:
        match = self.repository.match(match_id)
        self._ensure_loaded(match.game_id)
        return MatchSummary(match=self.repository.match(match_id), seats=self.repository.seats(match_id))

    def player_match_record(self, match: MatchRecord) -> MatchRecord:
        pending = self.repository.pending_adjudication(match.match_id)
        if pending:
            return match.model_copy(update={
                "phase": Phase(pending["before"]["phase"]),
                "version": pending["visible_version"],
            })
        return match

    def _player_state(self, match: MatchRecord) -> GameState:
        live = self._ensure_loaded(match.game_id)
        pending = self.repository.pending_adjudication(match.match_id)
        return GameState.model_validate(pending["before"]) if pending else live

    def submit_telegram(
        self,
        match_id: str,
        seat_id: str,
        user: AuthenticatedUser,
        request: TelegramSubmission,
    ) -> dict:
        match = self.repository.match(match_id)
        seat = self.repository.seat(seat_id)
        self._authorize_seat(seat, user)
        if seat.match_id != match_id:
            raise PermissionError("Seat does not belong to this match")
        if match.stage not in {MatchStage.COLLECTING, MatchStage.READY_FOR_REVIEW}:
            raise ValueError("裁判正在审理回合结果，发布后才能提交新电报")
        state = self._ensure_loaded(match.game_id)
        if state.phase == Phase.COMPLETE:
            raise ValueError("A completed match cannot send telegrams")
        message = request.message.strip()
        if not message:
            raise ValueError("Telegram message cannot be blank")
        return self.repository.save_telegram(match_id, seat, state.turn, message)

    @staticmethod
    def _telegram_char_count(message: str) -> int:
        return sum(not character.isspace() for character in message)

    def _resolve_due_telegrams(self, match_id: str, state) -> None:
        if state.phase == Phase.COMPLETE:
            return
        seats = self.repository.seats(match_id)
        storm = bool(state.options.optional_rules.storm)
        die_sides = 50 if storm else 100
        for telegram in self.repository.pending_telegrams(match_id, state.turn):
            sender_side = Side(telegram["side"])
            char_count = self._telegram_char_count(telegram["message"])
            deliveries = []
            for recipient in seats:
                if recipient.side != sender_side or recipient.seat_id == telegram["sender_seat_id"]:
                    continue
                roll = secrets.randbelow(die_sides) + 1
                hit_count = self.engine.telegram_receiver_hit_count(
                    state, recipient.controlled_ship_ids, int(telegram["sent_turn"])
                )
                adjusted_roll = self.engine.adjust_telegram_roll(roll, hit_count)
                deliveries.append({
                    "recipient_seat_id": recipient.seat_id,
                    "rule_id": TELEGRAM_RULE_ID,
                    "char_count": char_count,
                    "die_sides": die_sides,
                    "roll": roll,
                    "hit_count": hit_count,
                    "adjusted_roll": adjusted_roll,
                    "delivered": char_count < adjusted_roll,
                })
            self.repository.resolve_telegram(telegram["telegram_id"], state.turn, deliveries)

    def telegram_inbox(self, match_id: str, seat_id: str, user: AuthenticatedUser) -> dict:
        match = self.repository.match(match_id)
        seat = self.repository.seat(seat_id)
        self._authorize_seat(seat, user)
        if seat.match_id != match_id:
            raise PermissionError("Seat does not belong to this match")
        state = self._player_state(match)
        self._resolve_due_telegrams(match_id, state)
        messages = []
        for item in self.repository.telegram_inbox(match_id, seat_id):
            if item["received_turn"] > state.turn:
                continue
            messages.append({
                "telegram_id": item["telegram_id"],
                "sent_turn": item["sent_turn"],
                "received_turn": item["received_turn"],
                "sender": "友军电报",
                "message": item["message"] if item["delivered"] else None,
                "delivered": bool(item["delivered"]),
            })
        return {
            "turn": state.turn,
            "sent_turn": state.turn,
            "sent_message": self.repository.telegram_sent_this_turn(match_id, seat_id, state.turn),
            "messages": messages,
        }

    def judge_telegrams(self, match_id: str, user: AuthenticatedUser) -> list[dict]:
        match = self.repository.match(match_id)
        self._authorize_judge(match, user)
        state = self._ensure_loaded(match.game_id)
        self._resolve_due_telegrams(match_id, state)
        result = []
        for telegram in self.repository.telegram_records(match_id):
            deliveries = []
            for item in self.repository.telegram_deliveries(telegram["telegram_id"]):
                deliveries.append({
                    "rule_id": item["rule_id"],
                    "telegram_id": telegram["telegram_id"],
                    "sent_turn": telegram["sent_turn"],
                    "received_turn": item["received_turn"],
                    "sender": telegram["sender_username"] or "裁判代理",
                    "message": telegram["message"] if item["delivered"] else None,
                    "delivered": bool(item["delivered"]),
                    "char_count": item["char_count"],
                    "die_sides": item["die_sides"],
                    "roll": item["roll"],
                    "hit_count": item["hit_count"],
                    "adjusted_roll": item["adjusted_roll"],
                    "sender_seat_id": telegram["sender_seat_id"],
                    "sender_username": telegram["sender_username"],
                    "recipient_seat_id": item["recipient_seat_id"],
                    "recipient_username": item["recipient_username"],
                    "side": item["side"],
                })
            result.append({
                "rule_id": TELEGRAM_RULE_ID,
                "telegram_id": telegram["telegram_id"],
                "sent_turn": telegram["sent_turn"],
                "resolved_turn": telegram["resolved_turn"],
                "sender_seat_id": telegram["sender_seat_id"],
                "sender_username": telegram["sender_username"],
                "side": telegram["side"],
                "message": telegram["message"],
                "char_count": self._telegram_char_count(telegram["message"]),
                "deliveries": deliveries,
            })
        return result

    def _seat_trajectories(
        self,
        state,
        *,
        side: Side,
        own_ids: set[str],
        visible_other_ids: set[str],
        own_positions: list[HexCoord | None],
        aliases: dict[str, str],
    ) -> dict[str, list[str]]:
        """Return only the previous completed movement route visible to a seat."""
        if state.last_trajectory_turn != state.turn - 1:
            return {}
        result: dict[str, list[str]] = {}
        for ship_id in own_ids | visible_other_ids:
            raw_path = state.last_turn_trajectories.get(ship_id, [])
            if not raw_path:
                continue
            ship = state.ships.get(ship_id)
            if ship is None:
                continue
            if ship_id in own_ids:
                visible_path = raw_path
            else:
                visible_path = []
                for position in raw_path:
                    probe = ship.model_copy(update={"position": position, "sunk": False})
                    if self.engine._visible_to(
                        state,
                        probe,
                        side,
                        own_positions,
                        observer_ship_ids=own_ids,
                    ):
                        visible_path.append(position)
            # Collapse consecutive duplicates from a stopped ship and keep
            # coordinates as labels only; no hidden identifiers cross the API.
            labels: list[str] = []
            for position in visible_path:
                label = position.label
                if not labels or labels[-1] != label:
                    labels.append(label)
            if labels:
                result[ship_id if ship_id in own_ids else aliases[ship_id]] = labels
        return result

    @staticmethod
    def _full_trajectories(state) -> dict[str, list[str]]:
        if state.last_trajectory_turn != state.turn - 1:
            return {}
        return {
            ship_id: [position.label for position in path]
            for ship_id, path in state.last_turn_trajectories.items()
            if path
        }

    def observation(self, match_id: str, seat_id: str, user: AuthenticatedUser) -> SeatObservation:
        match = self.repository.match(match_id)
        seat = self.repository.seat(seat_id)
        self._authorize_seat(seat, user)
        if seat.match_id != match_id:
            raise PermissionError("Seat does not belong to this match")
        pending = self.repository.pending_adjudication(match_id)
        state = self._player_state(match)
        side_view = self.engine.observe(match.game_id, seat.side, state_override=state)
        own_ids = set(seat.controlled_ship_ids)
        all_hidden_contact_positions = self._hidden_contact_positions(state)
        hidden_contact_positions = {
            ship_id: projection
            for ship_id, projection in all_hidden_contact_positions.items()
            if ship_id in own_ids
        }
        sight_ships = {
            ship.id: ship.model_copy(
                update={
                    "position": all_hidden_contact_positions[ship.id][0],
                    "heading": all_hidden_contact_positions[ship.id][1],
                    "current_speed": all_hidden_contact_positions[ship.id][2],
                }
            )
            if ship.id in all_hidden_contact_positions and ship.position is None
            else ship
            for ship in state.ships.values()
        }
        own_positions = [
            state.ships[ship_id].position or hidden_contact_positions.get(ship_id, (None, None, None))[0]
            for ship_id in own_ids
            if ship_id in state.ships
        ]
        hidden_contact_mode = bool(state.options.optional_rules.hidden_contacts)
        visibility_radius = self.engine.visibility_radius(state, seat.side)
        visible_enemy_ids = {
            ship_id
            for ship_id, ship in sight_ships.items()
            if ship.side != seat.side
            and ship.position
            and self._seat_can_observe_ship(
                state, ship, seat.side, own_positions, own_ids,
                hidden_contact_mode=hidden_contact_mode,
                visibility_radius=visibility_radius,
            )
        }
        # A seat sees its own hulls plus any friendly or enemy contact that
        # those controlled hulls can observe.  Same-side ships do not share a
        # global tactical picture merely because they belong to the same side.
        visible_friendly_ids = {
            ship_id
            for ship_id, ship in sight_ships.items()
            if ship.side == seat.side
            and ship_id not in own_ids
            and ship.position
            and self._seat_can_observe_ship(
                state, ship, seat.side, own_positions, own_ids,
                hidden_contact_mode=hidden_contact_mode,
                visibility_radius=visibility_radius,
            )
        }
        visible_other_ids = visible_friendly_ids | visible_enemy_ids
        visible_hull_ids = {
            ship_id
            for ship_id in visible_other_ids
            if sight_ships[ship_id].position
            and self._position_within_sight(
                state, sight_ships[ship_id].position, own_positions, visibility_radius
            )
        }
        if not hidden_contact_mode:
            # Ordinary observation can identify contacts through radar or
            # silhouettes outside visual range; retain its hull projection.
            visible_hull_ids = visible_other_ids
        signal_only_ids = visible_other_ids - visible_hull_ids
        illuminated_ids = {
            ship_id
            for ship_id in signal_only_ids
            if self._contact_illuminates_ship(state, sight_ships[ship_id])
        }
        signal_target_ids: set[str] = set()
        if hidden_contact_mode and visibility_radius > 0:
            signal_ship_ids = {
                ship_id
                for marker in state.markers
                if marker.kind in {"fire", "gunfire"}
                for ship_id in (marker.ship_id, marker.target_ship_id)
                if ship_id
            }
            signal_target_ids = {
                ship_id
                for ship_id, ship in sight_ships.items()
                if ship_id not in own_ids | visible_other_ids
                and ship.position is not None
                and not ship.sunk
                and any(origin is not None for origin in own_positions)
                and (ship.fire_markers > 0 or ship.fired or ship_id in signal_ship_ids)
                and visibility_radius < min(
                    origin.distance(ship.position)
                    for origin in own_positions if origin is not None
                ) <= visibility_radius * 2
                and self._position_within_sight(
                    state, ship.position, own_positions, visibility_radius * 2
                )
            }
        allowed_ids = own_ids | visible_other_ids
        side_ships = {ship.id: ship for ship in side_view.ships}
        ships = []
        for ship_id in allowed_ids:
            source = side_ships.get(ship_id)
            truth = sight_ships[ship_id]
            if source is None:
                # Hidden formations have no authoritative position until their
                # contact marker is projected, so the engine's ordinary side
                # observation omits them. Build only a minimal contact record.
                source = PublicShip(
                    id=truth.id,
                    name=truth.name,
                    side=truth.side,
                    ship_type=truth.ship_type,
                    position=truth.position,
                    heading=truth.heading,
                    current_speed=truth.current_speed,
                    hull=None,
                    max_hull=None,
                    fire_markers=truth.fire_markers,
                    fired=truth.fired,
                    sunk=truth.sunk,
                    asset=truth.asset,
                )
            elif ship_id in all_hidden_contact_positions:
                source = source.model_copy(
                    update={
                        "position": truth.position,
                        "heading": truth.heading,
                        "current_speed": truth.current_speed,
                    }
                )
            ships.append(source)
        if not self.repository.contact_alias_migration_done(match_id, seat_id):
            self._migrate_contact_aliases(match_id, seat_id, state)
            self.repository.mark_contact_alias_migration_done(match_id, seat_id)
        contact_aliases = self.repository.contact_aliases(match_id, seat_id)
        scale_counts = {
            scale: sum(1 for known_scale, _ in contact_aliases.values() if known_scale == scale)
            for scale in ANONYMOUS_COUNTERS
        }
        # Allocate aliases from a perspective-neutral spatial order.  Do not
        # sort by the internal ship id: the catalog ids contain faction
        # prefixes, which would make 甲/乙/丙 order a side-channel.
        visible_contacts = sorted(
            (sight_ships[ship_id] for ship_id in visible_other_ids),
            key=lambda contact: (
                _anonymous_scale(contact.ship_type),
                contact.position.q if contact.position else 10**9,
                contact.position.r if contact.position else 10**9,
                contact.ship_type,
            ),
        )
        for contact in visible_contacts:
            if contact.id not in visible_other_ids:
                continue
            if contact.id in contact_aliases:
                continue
            scale = _anonymous_scale(contact.ship_type)
            alias = f"{ANONYMOUS_PREFIXES[scale]}{_anonymous_suffix(scale_counts[scale])}"
            self.repository.remember_contact_alias(match_id, seat_id, contact.id, scale, alias)
            contact_aliases[contact.id] = (scale, alias)
            scale_counts[scale] += 1
        other_aliases = {ship_id: alias for ship_id, (_, alias) in contact_aliases.items()}
        other_name_aliases = {
            state.ships[ship_id].name: alias
            for ship_id, alias in other_aliases.items()
            if ship_id in state.ships
        }
        for other in state.ships.values():
            if other.id in visible_other_ids:
                other_name_aliases.setdefault(
                other.name, f"{ANONYMOUS_PREFIXES[_anonymous_scale(other.ship_type)]}接触"
            )
        ships = [
            ship
            if ship.id in own_ids
            else ship.model_copy(
                update={
                    # Contact side is deliberately normalized.  The actual
                    # side never crosses the player boundary.
                    "id": other_aliases[ship.id],
                    "side": seat.side,
                    "name": other_aliases[ship.id],
                    "ship_type": scale,
                    "asset": (
                        ANONYMOUS_COUNTERS[scale]
                        if ship.id not in signal_only_ids or ship.id in illuminated_ids
                        else None
                    ),
                    "signal_only": ship.id in signal_only_ids,
                    "illuminated": ship.id in illuminated_ids,
                    **(
                        {
                            "heading": state.ships[ship.id].heading if ship.id in illuminated_ids else 1,
                            "current_speed": 0,
                            "hull": None,
                            "max_hull": None,
                            "fire_markers": 0,
                            "fired": False,
                            "sunk": False,
                            "vp": 0,
                            "max_speed": None,
                            "speed_damage_crossed": None,
                            "speed_damage_track": None,
                            "min_legal_speed": None,
                            "max_legal_speed": None,
                            "torpedo_type": None,
                            "gun_mounts": [],
                            "torpedo_launchers": [],
                        }
                        if ship.id in signal_only_ids
                        else {}
                    ),
                    "combat_history": [] if ship.id in signal_only_ids else [
                        entry.model_copy(
                            update={
                                "message": self._redact_enemy_names(entry.message, other_name_aliases),
                                "related_ship_id": self._redact_id(entry.related_ship_id, other_aliases),
                                "related_ship_name": self._redact_enemy_names(entry.related_ship_name, other_name_aliases)
                                if entry.related_ship_name else None,
                                "payload": self._redact_payload(entry.payload, other_aliases),
                            }
                        )
                        for entry in ship.combat_history
                    ],
                }
            )
            for ship in ships
            for scale in [_anonymous_scale(state.ships[ship.id].ship_type)]
        ]
        signal_aliases = self.repository.signal_targets(match_id, seat_id)
        for ship_id in sorted(
            signal_target_ids,
            key=lambda key: (sight_ships[key].position.q, sight_ships[key].position.r),
        ):
            truth = sight_ships[ship_id]
            if ship_id not in signal_aliases:
                alias = f"signal-{secrets.token_hex(8)}"
                self.repository.remember_signal_target(match_id, seat_id, ship_id, alias)
                signal_aliases[ship_id] = alias
            ships.append(PublicShip(
                id=signal_aliases[ship_id],
                name="某标记",
                side=seat.side,
                ship_type="unknown",
                position=truth.position,
                heading=1,
                current_speed=0,
                hull=None,
                max_hull=None,
                fire_markers=0,
                fired=False,
                sunk=False,
                asset=None,
                signal_only=True,
                illuminated=False,
            ))
        tracks = [
            track
            for track in side_view.torpedo_tracks
            if track.launcher_ship_id in own_ids
            or bool(set(track.contact_ship_ids).intersection(own_ids))
        ]
        if hidden_contact_mode:
            tracks = []
        tracks = [
            track.model_copy(
                update={
                    # A non-owned track is a neutral contact.  Never expose
                    # its launcher id, national artwork, or side.
                    "id": (
                        self._redact_id(track.id, other_aliases)
                        if not hidden_contact_mode or track.launcher_ship_id in own_ids
                        else f"torpedo-contact-{track.position.label}-{index}"
                    ),
                    "side": seat.side if track.launcher_ship_id not in own_ids else track.side,
                    "launcher_ship_id": track.launcher_ship_id
                    if track.launcher_ship_id in own_ids
                    else "contact",
                    "contact_ship_ids": [
                        ship_id for ship_id in track.contact_ship_ids if ship_id in own_ids
                    ],
                    **(
                        {
                            "launch_position": (
                                track.launch_position
                                if track.launch_position is not None
                                and self._position_within_sight(
                                    state,
                                    track.launch_position,
                                    own_positions,
                                    visibility_radius * 2,
                                )
                                else None
                            ),
                            "traversed_hexes": [
                                position
                                for position in track.traversed_hexes
                                if self._position_within_sight(
                                    state, position, own_positions, visibility_radius * 2
                                )
                            ],
                        }
                        if hidden_contact_mode
                        else {}
                    ),
                }
            )
            for index, track in enumerate(tracks, start=1)
        ]
        signal_aliases = self.repository.signal_targets(match_id, seat_id)
        public_target_ids = {**other_aliases, **signal_aliases}
        gunnery_candidates = []
        for candidate in self.engine._gunnery_candidates(state, seat.side):
            if candidate["ship_id"] not in own_ids:
                continue
            targets = [
                {
                    **target,
                    "target_id": public_target_ids.get(target["target_id"], target["target_id"]),
                }
                for target in candidate["targets"]
                if target["target_id"] in visible_other_ids
            ]
            gunnery_candidates.append({**candidate, "targets": targets})
        known_marker_ids = self.repository.known_markers(match_id, seat_id)
        if hidden_contact_mode:
            markers = self._hidden_contact_signal_markers(
                state,
                state.markers,
                own_ids=own_ids,
                visible_other_ids=visible_hull_ids,
                own_positions=own_positions,
                visibility_radius=visibility_radius,
                ships=sight_ships,
            )
        else:
            markers = [
                marker
                for marker in side_view.markers
                if self._marker_visible(marker, own_ids, visible_other_ids, known_marker_ids)
            ]
        orders = self.repository.orders(match_id, match.phase)
        redacted_markers = [
            marker.model_copy(
                update={
                    "id": self._redact_id(marker.id, other_aliases),
                    "ship_id": self._redact_id(marker.ship_id, other_aliases),
                    "target_ship_id": self._redact_id(marker.target_ship_id, other_aliases),
                }
            )
            for marker in markers
        ]
        wrecks = side_view.wrecks
        if hidden_contact_mode:
            wrecks = [
                wreck for wreck in wrecks
                if self._position_within_sight(
                    state, wreck.position, own_positions, visibility_radius * 2
                )
            ]
        redacted_wrecks = [
            wreck.model_copy(
                update={
                    "id": (
                        f"wreck-contact-{wreck.position.label}-{index}"
                        if hidden_contact_mode
                        else self._redact_id(wreck.id, other_aliases)
                    ),
                    "source_ship_id": (
                        wreck.source_ship_id
                        if wreck.source_ship_id in own_ids
                        else other_aliases.get(wreck.source_ship_id, "contact")
                    ),
                }
            )
            for index, wreck in enumerate(wrecks, start=1)
        ]
        ship_trajectories = self._seat_trajectories(
            state,
            side=seat.side,
            own_ids=own_ids,
            visible_other_ids=(
                visible_hull_ids if hidden_contact_mode else visible_other_ids
            ),
            own_positions=own_positions,
            aliases=other_aliases,
        )
        return SeatObservation(
            match_id=match_id,
            seat_id=seat_id,
            side=seat.side,
            turn=state.turn,
            max_turns=state.max_turns,
            phase=state.phase,
            stage=match.stage,
            version=pending["visible_version"] if pending else match.version,
            scenario_id=state.scenario_id,
            scenario_title=state.scenario_title,
            map_columns=state.map_columns,
            map_rows=state.map_rows,
            printed_columns=state.printed_columns,
            printed_rows=state.printed_rows,
            map_infinite=state.map_infinite,
            visibility=self.engine.visibility_radius(state, seat.side),
            visibility_axis=None,
            visibility_allies=None,
            storm=bool(state.options.optional_rules.storm),
            optional_rules=state.options.optional_rules.model_dump(),
            ships=ships,
            torpedo_tracks=tracks,
            markers=redacted_markers,
            wrecks=redacted_wrecks,
            score=side_view.score,
            # Player event feeds can reveal hidden actions even after message
            # redaction. Keep them referee-only; player updates use the phase
            # and their filtered observation fields above.
            recent_events=[],
            ship_trajectories=ship_trajectories,
            own_ship_ids=sorted(own_ids),
            # Kept for API compatibility; the legacy field must not disclose
            # which visible contacts are enemies.
            visible_enemy_ship_ids=[],
            submitted_seat_count=len(pending["submitted_seat_ids"]) if pending else len(orders),
            required_seat_count=len(match.required_seat_ids),
            seat_has_submitted=seat_id in pending["submitted_seat_ids"] if pending else seat_id in orders,
            winner=state.winner,
            victory_reason=state.victory_reason,
            torpedo_candidates=self.engine._torpedo_candidates(
                state, seat.side, ship_ids=own_ids
            ),
            gunnery_candidates=gunnery_candidates,
        )

    def torpedo_candidates_for_movement_draft(
        self,
        match_id: str,
        seat_id: str,
        user: AuthenticatedUser,
        movement_orders: list[MovementOrder],
    ) -> list[dict]:
        """Preview this seat's torpedo launch points against an uncommitted movement draft."""
        match = self.repository.match(match_id)
        seat = self.repository.seat(seat_id)
        self._authorize_seat(seat, user)
        if seat.match_id != match_id:
            raise PermissionError("Seat does not belong to this match")
        if any(order.ship_id not in seat.controlled_ship_ids for order in movement_orders):
            raise PermissionError("Movement preview may include only ships controlled by this seat")

        state = self._player_state(match).model_copy(deep=True)
        movement = OrderBatch(side=seat.side, phase=Phase.MOVEMENT_PLANNING, movement=movement_orders)
        projected_orders = []
        for order in movement_orders:
            ship = state.ships.get(order.ship_id)
            if not ship or ship.side != seat.side or ship.sunk or not ship.position:
                continue
            try:
                commands = self.engine.movement_commands(order)
                self.engine.movement_trajectory(
                    ship, order.plan, commands,
                    columns=state.map_columns, rows=state.map_rows,
                )
                ship.current_speed = self.engine.movement_cost(order.plan, commands)
            except ValueError:
                continue
            projected_orders.append(order)
        movement.movement = projected_orders
        state.sealed_orders.setdefault(
            f"{state.turn}:{Phase.MOVEMENT_PLANNING.value}", {}
        )[seat.side.value] = movement
        state.phase = Phase.TORPEDO_PLANNING
        return self.engine._torpedo_candidates(state, seat.side, ship_ids=seat.controlled_ship_ids)

    def judge_observation(self, match_id: str, user: AuthenticatedUser) -> SeatObservation:
        """Return the referee's full tactical map, including hidden positions."""
        match = self.repository.match(match_id)
        self._authorize_judge(match, user)
        state = self._ensure_loaded(match.game_id)
        pending = self.repository.pending_adjudication(match_id)
        view = self.engine.observe(match.game_id, Side.AXIS, debug=True)
        all_ship_ids = sorted(state.ships)
        ships = view.ships
        if state.phase == Phase.CONTACT_SETUP:
            ships = [
                ship.model_copy(update={"position": state.contact_reserve_positions[ship.id]})
                if ship.id in state.contact_reserve_positions
                else ship
                for ship in view.ships
            ]
        else:
            hidden_contact_positions = self._hidden_contact_positions(state)
            ships = [
                ship.model_copy(
                    update={
                        "position": hidden_contact_positions[ship.id][0],
                        "heading": hidden_contact_positions[ship.id][1],
                        "current_speed": hidden_contact_positions[ship.id][2],
                    }
                )
                if ship.position is None and ship.id in hidden_contact_positions
                else ship
                for ship in view.ships
            ]
        return SeatObservation(
            match_id=match_id,
            seat_id="referee",
            side=Side.AXIS,
            turn=state.turn,
            max_turns=state.max_turns,
            phase=state.phase,
            stage=match.stage,
            version=match.version,
            scenario_id=state.scenario_id,
            scenario_title=state.scenario_title,
            map_columns=state.map_columns,
            map_rows=state.map_rows,
            printed_columns=state.printed_columns,
            printed_rows=state.printed_rows,
            map_infinite=state.map_infinite,
            visibility=max(
                self.engine.visibility_radius(state, Side.AXIS),
                self.engine.visibility_radius(state, Side.ALLIES),
            ),
            visibility_axis=int(state.visibility[Side.AXIS.value]),
            visibility_allies=int(state.visibility[Side.ALLIES.value]),
            storm=bool(state.options.optional_rules.storm),
            optional_rules=state.options.optional_rules.model_dump(),
            ships=ships,
            referee_ship_states=[
                state.ships[ship_id].model_dump(mode="json")
                for ship_id in all_ship_ids
            ],
            previous_gunnery_targets=self._previous_gunnery_targets(state),
            torpedo_tracks=view.torpedo_tracks,
            markers=[marker for marker in view.markers if marker.kind != "contact"],
            wrecks=view.wrecks,
            score=view.score,
            recent_events=[event for event in view.recent_events if not event.type.startswith("judge_")],
            rerollable_events=[
                event for event in state.events
                if pending and event.sequence > pending["base_sequence"]
                and event.dice is not None and event.dice.rng_index is not None
                and not event.type.startswith("judge_")
            ],
            adjudication_turn=pending["before"]["turn"] if pending else None,
            ship_trajectories=self._full_trajectories(state),
            own_ship_ids=all_ship_ids,
            visible_enemy_ship_ids=all_ship_ids,
            submitted_seat_count=(
                len(pending["submitted_seat_ids"])
                if pending else len(self.repository.orders(match_id, match.phase))
            ),
            required_seat_count=len(match.required_seat_ids),
            winner=state.winner,
            victory_reason=state.victory_reason,
            torpedo_candidates=(
                self.engine._torpedo_candidates(state, Side.AXIS)
                + self.engine._torpedo_candidates(state, Side.ALLIES)
            ),
            gunnery_candidates=(
                self.engine._gunnery_candidates(state, Side.AXIS)
                + self.engine._gunnery_candidates(state, Side.ALLIES)
            ),
        )

    @staticmethod
    def _previous_gunnery_targets(state) -> dict[str, str]:
        latest: dict[str, tuple[int, str]] = {}
        for key, side_batches in state.sealed_orders.items():
            try:
                turn_text, phase = key.split(":", 1)
                turn = int(turn_text)
            except ValueError:
                continue
            if phase != Phase.GUNNERY.value or turn > state.turn:
                continue
            if turn == state.turn and state.phase == Phase.GUNNERY:
                continue
            for batch in side_batches.values():
                for order in batch.gunnery:
                    target_id = order.primary_target or order.secondary_target
                    if not target_id and order.mounts:
                        target_id = order.mounts[0].target_id
                    if target_id and turn >= latest.get(order.ship_id, (-1, ""))[0]:
                        latest[order.ship_id] = (turn, target_id)
        return {ship_id: target for ship_id, (_turn, target) in latest.items()}

    @staticmethod
    def _hidden_contact_positions(state) -> dict[str, tuple[HexCoord, int, int]]:
        """Build internal candidate positions for sealed, still-hidden formations."""
        if state.phase == Phase.CONTACT_SETUP:
            return {}
        projected: dict[str, tuple[HexCoord, int, int]] = {}
        for marker in state.markers:
            if marker.kind != "contact" or not marker.position or not marker.heading:
                continue
            offsets = state.contact_offsets.get(marker.id, {})
            for ship_id in state.contact_formations.get(marker.id, []):
                offset = offsets.get(ship_id)
                if offset is None:
                    continue
                ship = state.ships.get(ship_id)
                if ship is None or ship.sunk:
                    continue
                projected[ship_id] = (
                    HexCoord(q=marker.position.q + offset[0], r=marker.position.r + offset[1]),
                    marker.heading,
                    marker.movement_rate or ship.current_speed,
                )
        return projected

    def _seat_can_observe_ship(
        self,
        state,
        ship: ShipState,
        side: Side,
        own_positions: list[HexCoord | None],
        own_ids: set[str],
        *,
        hidden_contact_mode: bool,
        visibility_radius: int,
    ) -> bool:
        if not hidden_contact_mode:
            return self.engine._visible_to(
                state, ship, side, own_positions, observer_ship_ids=own_ids
            )
        if not ship.position or not self._position_within_sight(
            state, ship.position, own_positions, visibility_radius * 2
        ):
            return False
        distance = min(
            position.distance(ship.position)
            for position in own_positions
            if position is not None
        ) if any(position is not None for position in own_positions) else None
        if distance is None:
            return False
        if distance <= visibility_radius:
            return self.engine._visible_to(
                state, ship, side, own_positions, observer_ship_ids=own_ids
            )
        if state.options.optional_rules.squalls and self.engine._in_squall(state, ship.position):
            return False
        return self._contact_illuminates_ship(state, ship)

    def _contact_illuminates_ship(self, state, ship: ShipState) -> bool:
        if not ship.position:
            return False
        return any(
            marker.kind == "searchlight"
            and marker.target_ship_id == ship.id
            and marker.expires_turn == state.turn
            or marker.kind == "star_shell"
            and marker.expires_turn == state.turn
            and marker.position is not None
            and marker.position.distance(ship.position) <= 2
            for marker in state.markers
        )

    def _position_within_sight(
        self,
        state,
        position: HexCoord,
        own_positions: list[HexCoord | None],
        maximum_distance: int,
    ) -> bool:
        return any(
            origin is not None
            and origin.distance(position) <= maximum_distance
            and not self.engine._terrain_blocks_sight(state, origin, position)
            for origin in own_positions
        )

    def _hidden_contact_signal_markers(
        self,
        state,
        source_markers: list[MarkerState],
        *,
        own_ids: set[str],
        visible_other_ids: set[str],
        own_positions: list[HexCoord | None],
        visibility_radius: int,
        ships: dict[str, ShipState],
    ) -> list[MarkerState]:
        visible_ids = own_ids | visible_other_ids
        output: list[MarkerState] = []
        signal_counts: dict[tuple[str, str], int] = {}
        marked_ship_signals = {
            (marker.kind, marker.ship_id)
            for marker in source_markers
            if marker.ship_id is not None
        }

        def append_neutral(kind: str, position: HexCoord) -> None:
            key = (kind, position.label)
            index = signal_counts.get(key, 0) + 1
            signal_counts[key] = index
            output.append(MarkerState(
                id=f"signal-{kind}-{position.label}-{index}",
                kind=kind,
                position=position,
            ))

        for marker in source_markers:
            if marker.kind == "contact":
                continue
            position = marker.position
            if position is None and marker.target_ship_id:
                target = ships.get(marker.target_ship_id)
                position = target.position if target else None
            if position is None and marker.ship_id:
                source = ships.get(marker.ship_id)
                position = source.position if source else None
            if not position or not self._position_within_sight(
                state, position, own_positions, visibility_radius * 2
            ):
                continue
            if not position:
                continue
            append_neutral(marker.kind, position)

        if visibility_radius > 0:
            for ship in ships.values():
                if ship.id in visible_ids or not ship.position:
                    continue
                distance = min(
                    origin.distance(ship.position)
                    for origin in own_positions
                    if origin is not None
                ) if any(origin is not None for origin in own_positions) else None
                if distance is None or not visibility_radius < distance <= visibility_radius * 2:
                    continue
                if not self._position_within_sight(
                    state, ship.position, own_positions, visibility_radius * 2
                ):
                    continue
                if ship.fire_markers and ("fire", ship.id) not in marked_ship_signals:
                    append_neutral("fire", ship.position)
                if ship.fired and ("gunfire", ship.id) not in marked_ship_signals:
                    append_neutral("gunfire", ship.position)
        return output

    def turn_archives(self, match_id: str, user: AuthenticatedUser) -> list[dict[str, int | str]]:
        """List the newest persisted game snapshot for each turn, referee only."""
        match = self.repository.match(match_id)
        self._authorize_judge(match, user)
        latest_by_turn: dict[int, tuple[int, GameState]] = {}
        for sequence, snapshot in self.game_repository.snapshots(match.game_id):
            current = latest_by_turn.get(snapshot.turn)
            if current is None or sequence > current[0]:
                latest_by_turn[snapshot.turn] = (sequence, snapshot)
        return [
            {"turn": turn, "sequence": sequence, "phase": snapshot.phase.value}
            for turn, (sequence, snapshot) in sorted(latest_by_turn.items())
        ]

    def turn_archive_state(
        self, match_id: str, turn: int, user: AuthenticatedUser
    ) -> GameState:
        """Return a detached latest-in-turn snapshot after referee authorization."""
        match = self.repository.match(match_id)
        self._authorize_judge(match, user)
        candidates = [
            (sequence, snapshot)
            for sequence, snapshot in self.game_repository.snapshots(match.game_id)
            if snapshot.turn == turn
        ]
        if not candidates:
            raise KeyError(f"No saved archive for turn {turn}")
        return max(candidates, key=lambda item: item[0])[1]

    def submit_order(self, match_id: str, seat_id: str, user: AuthenticatedUser, submission: SeatOrderSubmission) -> MatchRecord:
        match = self.repository.match(match_id)
        seat = self.repository.seat(seat_id)
        self._authorize_seat(seat, user)
        if seat.match_id != match_id:
            raise PermissionError("Seat does not belong to this match")
        if match.stage not in {MatchStage.COLLECTING, MatchStage.READY_FOR_REVIEW}:
            raise ValueError("Orders are not being collected")
        if submission.phase != match.phase:
            raise ValueError(f"Orders belong to {submission.phase.value}, current phase is {match.phase.value}")
        if submission.scope == "turn" and match.phase != Phase.REINFORCEMENT:
            raise ValueError("整回合订单只能在回合开始时提交")
        existing_scope = self.repository.order_scope(match_id, match.phase)
        if existing_scope and existing_scope != submission.scope:
            raise ValueError("本阶段已有不同提交范围的订单，请先由裁判驳回后统一提交")
        batch = submission.batch.model_copy(deep=True)
        batch.phase = match.phase
        if batch.side != seat.side:
            raise PermissionError("Order side does not match the seat")
        if match.phase == Phase.CONTACT_SETUP and batch.contacts:
            raise PermissionError("Hidden contact deployment is managed by the referee")
        batch = self._resolve_contact_aliases(match_id, seat_id, batch)
        try:
            self._assert_order_ownership(batch, seat)
        except PermissionError as error:
            # 该文案会列出本席位无权指挥的舰船 id；对玩家而言这就是"点名"，
            # 因此与校验报错走同一道可见性脱敏（2026-10-12 信息泄露修复）。
            redacted = self._redact_unseen_ship_mentions(match, seat, [str(error)])
            raise PermissionError(redacted[0]) from error
        previous = self.repository.orders(match_id, match.phase).get(seat_id)
        validation_errors = self._validate_pending_batch(match, seat_id, seat.side, batch, submission.scope)
        pending_orders = self.repository.orders(match_id, match.phase)
        pending_orders[seat_id] = (submission.request_id, batch)
        if submission.scope == "turn":
            state = self._ensure_loaded(match.game_id)
            expected_ships = {
                ship.id for ship in state.ships.values()
                if ship.side == seat.side and ship.id in seat.controlled_ship_ids
                and not ship.sunk and ship.position is not None
            }
            expected_ships.update(order.ship_id for order in batch.reinforcements)
            submitted_ships = {order.ship_id for order in batch.movement}
            if expected_ships - submitted_ships:
                validation_errors.append("本席位有舰船缺少移动计划，请补齐后再提交。")
        submitted_side_seats = {
            required_id
            for required_id in match.required_seat_ids
            if self.repository.seat(required_id).side == seat.side
        }
        if submission.scope == "turn":
            validation_errors.extend(
                self._validate_turn_future_orders(match_id, pending_orders, only_side=seat.side)
            )
            if submitted_side_seats <= set(pending_orders):
                validation_errors.extend(
                    self._validate_turn_movement_coverage(match_id, pending_orders, only_side=seat.side)
                )
        validation_errors = list(dict.fromkeys(validation_errors))
        if validation_errors and not submission.ignore_validation_errors:
            # 席位侧必须脱敏：报错不得点名本席位看不见的舰船（裁判侧不经过这里，仍是完整文案）。
            raise OrderValidationIssue(
                self._redact_unseen_ship_mentions(match, seat, validation_errors)
            )
        if previous:
            self.repository.forget_markers(match_id, seat_id, self._marker_ids(previous[1]))
        self.repository.save_order(
            match_id, match.phase, seat, submission.request_id, batch, submission.scope, validation_errors
        )
        self.repository.remember_markers(match_id, seat_id, self._marker_ids(batch))
        orders = self.repository.orders(match_id, match.phase)
        match.stage = (
            MatchStage.READY_FOR_REVIEW
            if set(orders) >= set(match.required_seat_ids)
            else MatchStage.COLLECTING
        )
        match.version += 1
        self.repository.save_match(match)
        return match

    def _validate_turn_movement_coverage(
        self,
        match_id: str,
        orders: dict[str, tuple[str, OrderBatch]],
        only_side: Side | None = None,
    ) -> list[str]:
        state = self._ensure_loaded(self.repository.match(match_id).game_id)
        batches_by_side: dict[Side, list[OrderBatch]] = {side: [] for side in Side}
        for seat_id, (_, batch) in orders.items():
            batches_by_side[self.repository.seat(seat_id).side].append(batch)
        errors: list[str] = []
        for side, batches in batches_by_side.items():
            if only_side is not None and side != only_side:
                continue
            expected = {
                ship.id
                for ship in state.ships.values()
                if ship.side == side and not ship.sunk and ship.position is not None
                and not self.engine.captain_command_lost(state, ship)
            }
            expected.update(
                order.ship_id
                for batch in batches
                for order in batch.reinforcements
            )
            submitted = {order.ship_id for batch in batches for order in batch.movement}
            if submitted != expected:
                errors.append("本阵营有舰船缺少移动计划；详细舰只列表仅裁判可见。")
        return errors

    def _validate_turn_future_orders(
        self,
        match_id: str,
        orders: dict[str, tuple[str, OrderBatch]],
        only_side: Side | None = None,
    ) -> list[str]:
        """Validate all preplanned actions against isolated future-phase states.

        Turn-scope orders are submitted during reinforcement, so validating the
        current phase alone silently skips future actions. Run the engine's
        normal validator for movement, torpedo, and gunnery phases against an
        isolated copy; never mutate or advance the live game while drafting.
        """
        match = self.repository.match(match_id)
        state = self._ensure_loaded(match.game_id).model_copy(deep=True)
        state.submitted_orders.clear()
        validator = copy(self.engine)
        validator.games = {**self.engine.games, state.game_id: state}
        batches_by_side: dict[Side, list[OrderBatch]] = {side: [] for side in Side}
        for seat_id, (_, batch) in orders.items():
            side = self.repository.seat(seat_id).side
            if only_side is None or side == only_side:
                batches_by_side[side].append(batch)
                for reinforcement in batch.reinforcements:
                    ship = state.ships.get(reinforcement.ship_id)
                    if ship and ship.position is None:
                        ship.position = reinforcement.entry_hex
                        ship.heading = reinforcement.heading
                        ship.current_speed = reinforcement.speed
                        ship.previous_speed = reinforcement.speed
        errors: list[str] = []
        phase_order = (Phase.MOVEMENT_PLANNING, Phase.TORPEDO_PLANNING, Phase.GUNNERY)
        movement_batches: dict[Side, OrderBatch] = {}
        movement_end_states: dict[str, tuple[HexCoord, int, int]] = {}
        for phase in phase_order:
            state.phase = phase
            if phase == Phase.GUNNERY:
                for ship_id, (position, heading, speed) in movement_end_states.items():
                    ship = state.ships.get(ship_id)
                    if ship:
                        ship.position = position
                        ship.heading = heading
                        ship.current_speed = speed
            for side, batches in batches_by_side.items():
                if only_side is not None and side != only_side:
                    continue
                if not batches:
                    continue
                projected = [self._project_turn_batch(batch, phase) for batch in batches]
                merged = self._merge_side_orders(phase, side, projected)
                if phase == Phase.MOVEMENT_PLANNING:
                    movement_for_simulation = merged
                    if state.options.realistic_command:
                        from ..realistic_command import expand_movement_orders
                        movement_for_simulation, expansion_errors, _detach = expand_movement_orders(
                            validator, state, merged
                        )
                        errors.extend(
                            error for error in expansion_errors
                            if not self._is_coverage_error(error)
                        )
                    movement_batches[side] = movement_for_simulation
                    for order in movement_for_simulation.movement:
                        ship = state.ships.get(order.ship_id)
                        if not ship or not ship.position:
                            continue
                        try:
                            commands = validator.movement_commands(order)
                            trajectory, final_heading = validator.movement_trajectory(
                                ship, order.plan, commands,
                                columns=state.map_columns, rows=state.map_rows,
                            )
                            speed = validator.movement_cost(order.plan, commands)
                        except ValueError:
                            continue
                        final_position = trajectory[-1][0] if trajectory else ship.position
                        movement_end_states[ship.id] = (final_position, final_heading, speed)
                        ship.current_speed = speed
                elif phase == Phase.TORPEDO_PLANNING:
                    movement = movement_batches.get(side)
                    if movement:
                        sealed = state.sealed_orders.setdefault(
                            f"{state.turn}:{Phase.MOVEMENT_PLANNING.value}", {}
                        )
                        sealed[side.value] = movement
                validation = validator.validate_orders(state.game_id, merged)
                if not validation.valid:
                    errors.extend(
                        error for error in validation.errors
                        if not self._is_coverage_error(error)
                    )
        return list(dict.fromkeys(errors))

    def _resolve_contact_aliases(self, match_id: str, seat_id: str, batch: OrderBatch) -> OrderBatch:
        """Translate neutral contact labels back to authoritative ship ids.

        Contact ids in a player view are aliases such as ``驱逐甲``.  The
        engine still adjudicates against the real ids, but those ids never
        need to cross the player boundary.
        """
        aliases = self.repository.contact_aliases(match_id, seat_id)
        reverse = {alias: ship_id for ship_id, (_, alias) in aliases.items()}
        reverse.update({alias: ship_id for ship_id, alias in self.repository.signal_targets(match_id, seat_id).items()})
        if not reverse:
            return batch

        def resolve(value: str | None) -> str | None:
            return reverse.get(value, value) if value is not None else None

        for order in batch.gunnery:
            order.primary_target = resolve(order.primary_target)
            order.secondary_target = resolve(order.secondary_target)
            order.searchlight_target = resolve(order.searchlight_target)
            for mount in order.mounts:
                mount.target_id = resolve(mount.target_id) or mount.target_id
        for order in batch.searchlights:
            order.target_id = resolve(order.target_id) or order.target_id
        return batch

    def judge_replace_order(
        self,
        match_id: str,
        seat_id: str,
        user: AuthenticatedUser,
        request: JudgeOrderOverrideRequest,
    ) -> MatchRecord:
        """Replace any seat's pending order while the phase is still under review."""
        match = self.repository.match(match_id)
        self._authorize_judge(match, user)
        self._check_version(match, request.expected_version)
        seat = self.repository.seat(seat_id)
        if seat.match_id != match_id:
            raise PermissionError("Seat does not belong to this match")
        if match.stage not in {MatchStage.COLLECTING, MatchStage.READY_FOR_REVIEW}:
            raise ValueError("Orders can only be overridden before adjudication")
        if request.phase != match.phase:
            raise ValueError(f"Orders belong to {request.phase.value}, current phase is {match.phase.value}")
        scope = self.repository.order_scope(match_id, match.phase) or request.scope
        if scope == "turn" and match.phase != Phase.REINFORCEMENT:
            raise ValueError("整回合订单只能在回合开始时提交")
        existing_scope = self.repository.order_scope(match_id, match.phase)
        if existing_scope and existing_scope != request.scope:
            raise ValueError("本阶段已有不同提交范围的订单，请先驳回后统一提交")
        batch = request.batch.model_copy(deep=True)
        batch.phase = match.phase
        if batch.side != seat.side:
            raise PermissionError("Order side does not match the seat")
        self._assert_order_ownership(batch, seat)
        previous = self.repository.orders(match_id, match.phase).get(seat_id)
        validation_errors = self._validate_pending_batch(match, seat_id, seat.side, batch, scope)
        if validation_errors:
            raise ValueError(validation_errors)
        before = previous[1].model_dump(mode="json") if previous else {}
        request_id = f"judge-{uuid.uuid4()}"
        self.repository.save_order(match_id, match.phase, seat, request_id, batch, scope)
        self.repository.audit(match_id, match.phase, seat_id, request_id, batch, "judge_override", request.reason)
        if previous:
            self.repository.forget_markers(match_id, seat_id, self._marker_ids(previous[1]))
        self.repository.remember_markers(match_id, seat_id, self._marker_ids(batch))
        self.repository.save_override(
            match_id,
            kind="order",
            target=seat_id,
            reason=request.reason,
            before=before,
            after=batch.model_dump(mode="json"),
        )
        orders = self.repository.orders(match_id, match.phase)
        match.stage = MatchStage.READY_FOR_REVIEW if set(orders) >= set(match.required_seat_ids) else MatchStage.COLLECTING
        match.version += 1
        self.repository.save_match(match)
        return match

    def judge_patch_ship(
        self,
        match_id: str,
        ship_id: str,
        user: AuthenticatedUser,
        request: JudgeShipPatchRequest,
    ) -> dict:
        """Apply a validated referee patch to any mutable ship state field."""
        match = self.repository.match(match_id)
        self._authorize_judge(match, user)
        self._check_version(match, request.expected_version)
        state = self._ensure_loaded(match.game_id)
        ship = state.ships.get(ship_id)
        if ship is None:
            raise KeyError(f"Unknown ship {ship_id}")
        immutable = {"id", "name", "side", "ship_type"}
        forbidden = immutable.intersection(request.patch)
        if forbidden:
            raise ValueError(f"Ship identity fields cannot be changed: {sorted(forbidden)}")
        unknown = set(request.patch) - set(ShipState.model_fields)
        if unknown:
            raise ValueError(f"Unknown ship state fields: {sorted(unknown)}")
        before = ship.model_dump(mode="json")
        merged = dict(before)
        merged.update(request.patch)
        try:
            updated = ShipState.model_validate(merged)
        except Exception as error:
            raise ValueError(f"Invalid ship state patch: {error}") from error
        if "captain_status" in request.patch:
            updated.captain_command_loss_turn = (
                state.turn + 1 if updated.captain_status in {"wounded", "killed"} else None
            )
        state.ships[ship_id] = updated
        event = self.engine._event(
            state,
            "judge_ship_override",
            f"裁判覆盖舰船状态：{ship_id}",
            payload={
                "secret_side": "referee",
                "ship_id": ship_id,
                "fields": sorted(request.patch),
                "reason": request.reason,
            },
        )
        self.game_repository.save(state)
        after = updated.model_dump(mode="json")
        pending = self.repository.pending_adjudication(match_id)
        if pending:
            fields = set(request.patch)
            if "captain_status" in fields:
                fields.add("captain_command_loss_turn")
            pending["corrections"].append({"kind": "ship", "ship_id": ship_id, "patch": {key: after[key] for key in fields}})
            self.repository.save_adjudication(match_id, pending)
        override = self.repository.save_override(
            match_id,
            kind="ship_state",
            target=ship_id,
            reason=request.reason,
            before=before,
            after=after,
        )
        match.version += 1
        self.repository.save_match(match)
        return {"match": match.model_dump(mode="json"), "ship": after, "event": event.model_dump(mode="json"), "override": override.model_dump(mode="json")}

    def judge_reroll(
        self,
        match_id: str,
        user: AuthenticatedUser,
        request: JudgeRerollRequest,
    ) -> dict:
        """Replay the unpublished adjudication with a replacement engine roll."""
        match = self.repository.match(match_id)
        self._authorize_judge(match, user)
        self._check_version(match, request.expected_version)
        pending = self.repository.pending_adjudication(match_id)
        if match.stage != MatchStage.AWAITING_PUBLICATION or pending is None:
            raise ValueError("只能重骰尚未发布的回合裁决；请先批准订单进行裁决")
        state = self._ensure_loaded(match.game_id)
        target = next((event for event in state.events if event.sequence == request.event_sequence), None)
        if target is None:
            raise KeyError(f"Unknown event sequence {request.event_sequence}")
        if target.dice is None:
            raise ValueError("Only events with dice can be rerolled")
        if target.sequence <= pending["base_sequence"] or target.type.startswith("judge_") or target.dice.rng_index is None:
            raise ValueError("该事件不属于当前待发布裁决中的可重骰事件")
        before = target.dice.model_dump(mode="json")
        draw_state = state.model_copy(deep=True)
        draw_state.rng_counter = max(draw_state.rng_counter, pending.get("reroll_rng_counter", 0))
        new_dice = self._reroll(draw_state, target.dice)
        index = str(target.dice.rng_index)
        pending["overrides"][index] = {
            "dice": new_dice.model_dump(mode="json"),
            "identity": self._dice_event_identity(target),
        }
        invalidated = 0
        while True:
            rebuilt = self._replay_adjudication(pending)
            events_by_roll = {
                event.dice.rng_index: event for event in rebuilt.events
                if event.sequence > pending["base_sequence"] and event.dice and event.dice.rng_index is not None
            }
            stale = [
                key for key, replacement in pending["overrides"].items()
                if int(key) not in events_by_roll
                or self._dice_event_identity(events_by_roll[int(key)]) != replacement["identity"]
                or events_by_roll[int(key)].dice.dice != replacement["dice"]["dice"]
            ]
            if index in stale:
                raise ValueError("重裁无法定位原骰点事件，未修改战果")
            if not stale:
                break
            for key in stale:
                del pending["overrides"][key]
            invalidated += len(stale)
        revised_target = events_by_roll[int(index)]
        pending["reroll_rng_counter"] = draw_state.rng_counter
        rebuilt.rng_counter = max(rebuilt.rng_counter, draw_state.rng_counter)
        for correction in pending["corrections"]:
            if correction["kind"] == "ship":
                ship_id = correction["ship_id"]
                merged = rebuilt.ships[ship_id].model_dump(mode="json")
                merged.update(correction["patch"])
                rebuilt.ships[ship_id] = ShipState.model_validate(merged)
            elif correction["kind"] == "visibility":
                values = correction["values"]
                rebuilt.visibility = {Side.AXIS.value: values["axis"], Side.ALLIES.value: values["allies"]}
                rebuilt.options.optional_rules.storm = values["storm"]
                rebuilt.options.optional_rules.hidden_contacts = values["hidden_contacts"]
        for event in state.events:
            if event.sequence > pending["base_sequence"] and event.type.startswith("judge_"):
                rebuilt.events.append(event.model_copy(update={"sequence": len(rebuilt.events) + 1}))
        reroll_event = self.engine._event(
            rebuilt,
            "judge_reroll",
            f"裁判重投事件 #{target.sequence} 并重新裁决全部后果",
            payload={
                "secret_side": "referee",
                "target_sequence": target.sequence,
                "reason": request.reason,
                "original": before,
                "replacement": revised_target.dice.model_dump(mode="json"),
                "revised_sequence": revised_target.sequence,
                "invalidated_rerolls": invalidated,
            },
            dice=revised_target.dice,
        )
        self.game_repository.replace_after(rebuilt, pending["base_sequence"])
        self.engine.games[match.game_id] = rebuilt
        self.repository.save_adjudication(match_id, pending)
        self._refresh_adjudicated_telegrams(match_id, rebuilt, pending["before"]["turn"])
        override = self.repository.save_override(
            match_id,
            kind="reroll",
            target=str(target.sequence),
            reason=request.reason,
            before=before,
            after=revised_target.dice.model_dump(mode="json"),
        )
        match.phase = rebuilt.phase
        match.version += 1
        self.repository.save_match(match)
        return {
            "match": match.model_dump(mode="json"),
            "event": revised_target.model_dump(mode="json"),
            "reroll_event": reroll_event.model_dump(mode="json"),
            "override": override.model_dump(mode="json"),
            "invalidated_rerolls": invalidated,
        }

    @staticmethod
    def _dice_event_identity(event: GameEvent) -> dict:
        keys = {"attacker", "target", "ship_id", "track_id", "marker_id", "wreck_id", "obstacle_id", "ships"}
        if event.type == "gun_mount_attack":
            keys.add("mount_ids")
        if event.type == "star_shell_fired":
            keys.update({"mount_id", "target_hex"})
        return {"type": event.type, "phase": event.phase.value, "source": {key: event.payload[key] for key in sorted(keys) if key in event.payload}}

    def _replay_adjudication(self, pending: dict) -> GameState:
        replay_engine = IronBottomEngine()
        replay_engine.rules = self.engine.rules
        baseline = GameState.model_validate(pending["before"])
        replay_engine.games[baseline.game_id] = baseline
        replay_engine._forced_rolls = {
            int(key): DiceRoll.model_validate(value["dice"]) for key, value in pending["overrides"].items()
        }
        orders = {Side(side): OrderBatch.model_validate(batch) for side, batch in pending["orders"].items()}
        if pending["scope"] == "turn":
            replay_engine.step(baseline.game_id, orders)
        else:
            for side in Side:
                validation = replay_engine.submit_orders(baseline.game_id, orders[side])
                if not validation.valid:
                    raise ValueError(validation.errors)
            while True:
                replay_engine.advance(baseline.game_id)
                if baseline.phase in ORDER_PHASES | {Phase.COMPLETE}:
                    break
        return replay_engine.get(baseline.game_id)

    def publish_adjudication(self, match_id: str, user: AuthenticatedUser, expected_version: int) -> MatchRecord:
        match = self.repository.match(match_id)
        self._authorize_judge(match, user)
        self._check_version(match, expected_version)
        if match.stage != MatchStage.AWAITING_PUBLICATION or self.repository.pending_adjudication(match_id) is None:
            raise ValueError("当前没有待发布的裁决")
        state = self._ensure_loaded(match.game_id)
        self.repository.clear_adjudication(match_id)
        match.stage = MatchStage.COMPLETE if state.phase == Phase.COMPLETE else MatchStage.COLLECTING
        match.version += 1
        self.repository.save_match(match)
        self._resolve_due_telegrams(match_id, state)
        return match

    def _refresh_adjudicated_telegrams(self, match_id: str, state: GameState, sent_turn: int) -> None:
        for telegram in self.repository.telegram_records(match_id):
            if telegram["sent_turn"] != sent_turn:
                continue
            for delivery in self.repository.telegram_deliveries(telegram["telegram_id"]):
                seat = self.repository.seat(delivery["recipient_seat_id"])
                hits = self.engine.telegram_receiver_hit_count(state, seat.controlled_ship_ids, sent_turn)
                adjusted = self.engine.adjust_telegram_roll(delivery["roll"], hits)
                self.repository.update_telegram_result(
                    telegram["telegram_id"], seat.seat_id, hits, adjusted, delivery["char_count"] < adjusted,
                )

    def judge_patch_visibility(
        self,
        match_id: str,
        user: AuthenticatedUser,
        request: JudgeVisibilityPatchRequest,
    ) -> dict:
        """Override player visibility settings without restarting the match."""
        match = self.repository.match(match_id)
        self._authorize_judge(match, user)
        self._check_version(match, request.expected_version)
        state = self._ensure_loaded(match.game_id)
        if request.storm and (request.axis % 2 or request.allies % 2):
            raise ValueError("风暴降临需要将两边基础视野半径设置为偶数")
        before = {
            "axis": int(state.visibility[Side.AXIS.value]),
            "allies": int(state.visibility[Side.ALLIES.value]),
            "storm": bool(state.options.optional_rules.storm),
        }
        before["hidden_contacts"] = bool(state.options.optional_rules.hidden_contacts)
        state.visibility = {
            Side.AXIS.value: int(request.axis),
            Side.ALLIES.value: int(request.allies),
        }
        state.options.optional_rules.storm = bool(request.storm)
        if request.hidden_contacts is not None:
            state.options.optional_rules.hidden_contacts = request.hidden_contacts
        event = self.engine._event(
            state,
            "judge_visibility_override",
            "裁判覆盖玩家视野设置",
            payload={
                "secret_side": "referee",
                "axis": int(request.axis),
                "allies": int(request.allies),
                "storm": bool(request.storm),
                "hidden_contacts": bool(state.options.optional_rules.hidden_contacts),
                "reason": request.reason,
            },
        )
        self.game_repository.save(state)
        after = {
            "axis": int(request.axis),
            "allies": int(request.allies),
            "storm": bool(request.storm),
            "hidden_contacts": bool(state.options.optional_rules.hidden_contacts),
        }
        pending = self.repository.pending_adjudication(match_id)
        if pending:
            pending["corrections"].append({"kind": "visibility", "values": after})
            self.repository.save_adjudication(match_id, pending)
        override = self.repository.save_override(
            match_id,
            kind="visibility",
            target="all_players",
            reason=request.reason,
            before=before,
            after=after,
        )
        match.version += 1
        self.repository.save_match(match)
        return {
            "match": match.model_dump(mode="json"),
            "visibility_axis": after["axis"],
            "visibility_allies": after["allies"],
            "storm": after["storm"],
            "hidden_contacts": after["hidden_contacts"],
            "event": event.model_dump(mode="json"),
            "override": override.model_dump(mode="json"),
        }

    def judge_overrides(self, match_id: str, user: AuthenticatedUser):
        match = self.repository.match(match_id)
        self._authorize_judge(match, user)
        return self.repository.overrides(match_id)

    def _reroll(self, state, dice: DiceRoll) -> DiceRoll:
        notation = dice.notation.upper().replace(" ", "")
        if notation in {"D6", "1D6"}:
            raw, value = self.engine._roll_d6(state)
            values = [value]
        elif notation in {"2D6", "2D6SUM"}:
            raw, values = self.engine._roll_2d6(state)
        elif notation == "D66":
            raw, values = self.engine._roll_d66(state)
        else:
            match = re.fullmatch(r"(\d+)D(\d+)", notation)
            if not match or match.group(2) != "6":
                raise ValueError(f"Unsupported dice notation for reroll: {dice.notation}")
            count = int(match.group(1))
            if count == 1:
                raw, value = self.engine._roll_d6(state)
                values = [value]
            elif count == 2:
                raw, values = self.engine._roll_2d6(state)
            else:
                raise ValueError(f"Unsupported dice notation for reroll: {dice.notation}")
        return DiceRoll(dice=values, notation=dice.notation, raw=raw, rng_index=dice.rng_index)

    def reject(self, match_id: str, user: AuthenticatedUser, expected_version: int, reason: str) -> MatchRecord:
        match = self.repository.match(match_id)
        self._authorize_judge(match, user)
        self._check_version(match, expected_version)
        if match.stage not in {MatchStage.COLLECTING, MatchStage.READY_FOR_REVIEW}:
            raise ValueError("Orders can only be rejected while they are being collected or reviewed")
        current_orders = self.repository.orders(match_id, match.phase)
        for seat_id, (request_id, batch) in current_orders.items():
            self.repository.audit(match_id, match.phase, seat_id, request_id, batch, "rejected", reason)
            self.repository.forget_markers(
                match_id,
                seat_id,
                [order.marker_id for order in batch.contacts] + [order.marker_id for order in batch.contact_movement],
            )
        self.repository.clear_orders(match_id, match.phase)
        match.stage = MatchStage.COLLECTING
        match.version += 1
        self.repository.save_match(match)
        return match

    def approve(self, match_id: str, user: AuthenticatedUser, expected_version: int, note: str | None = None) -> ApprovalResult:
        match = self.repository.match(match_id)
        self._authorize_judge(match, user)
        self._check_version(match, expected_version)
        if match.stage != MatchStage.READY_FOR_REVIEW:
            raise ValueError("The phase is not ready for referee approval")
        current_orders = self.repository.orders(match_id, match.phase)
        if set(current_orders) < set(match.required_seat_ids):
            raise ValueError("All required seats must submit before approval")
        state = self._ensure_loaded(match.game_id)
        assigned_ship_ids = {
            ship_id
            for seat in self.repository.seats(match_id)
            for ship_id in seat.controlled_ship_ids
        }
        missing_ship_ids = set(state.ships) - assigned_ship_ids
        if missing_ship_ids:
            raise ValueError(f"Every ship needs a seat before approval: {sorted(missing_ship_ids)}")
        assigned_sides = {self.repository.seat(seat_id).side for seat_id in match.required_seat_ids}
        if assigned_sides != set(Side):
            raise ValueError("Both sides need at least one seat before approval")
        scope = self.repository.order_scope(match_id, match.phase) or "phase"
        state_before = self.engine.get(match.game_id).model_copy(deep=True)
        if scope == "turn":
            match.stage = MatchStage.RESOLVING
            self.repository.save_match(match)
            try:
                full_turn_orders = {
                    side: self._merge_side_orders(
                        match.phase,
                        side,
                        [batch for seat_id, (_, batch) in current_orders.items() if self.repository.seat(seat_id).side == side],
                    )
                    for side in Side
                }
                _, events = self.engine.step(match.game_id, full_turn_orders)
            except Exception:
                self.engine.games[match.game_id] = state_before
                match.stage = MatchStage.READY_FOR_REVIEW
                self.repository.save_match(match)
                raise
            state = self.engine.get(match.game_id)
            self._hold_adjudication(match, state_before, full_turn_orders, scope, current_orders)
            self.game_repository.save(state)
            for seat_id, (request_id, batch) in current_orders.items():
                self.repository.audit(match_id, match.phase, seat_id, request_id, batch, "approved", note)
            self.repository.clear_orders(match_id, match.phase)
            match.phase = state.phase
            match.stage = MatchStage.AWAITING_PUBLICATION
            match.version += 1
            self.repository.save_match(match)
            self._resolve_due_telegrams(match_id, state)
            return ApprovalResult(match=match, events=events)
        merged = {
            side: self._merge_side_orders(
                match.phase,
                side,
                [batch for seat_id, (_, batch) in current_orders.items() if self.repository.seat(seat_id).side == side],
            )
            for side in Side
        }
        for batch in merged.values():
            validation = self.engine.validate_orders(match.game_id, batch)
            if not validation.valid:
                raise ValueError(validation.errors)
        match.stage = MatchStage.RESOLVING
        self.repository.save_match(match)
        try:
            for side in Side:
                result = self.engine.submit_orders(match.game_id, merged[side])
                if not result.valid:
                    raise ValueError(result.errors)
            events = []
            # Resolution-only phases are deterministic consequences of the
            # approved orders. Process them inside this referee approval so a
            # new seat collection never opens for a phase with no orders.
            while True:
                events.extend(self.engine.advance(match.game_id))
                if self.engine.get(match.game_id).phase in ORDER_PHASES | {Phase.COMPLETE}:
                    break
        except Exception:
            self.engine.games[match.game_id] = state_before
            match.stage = MatchStage.READY_FOR_REVIEW
            self.repository.save_match(match)
            raise
        state = self.engine.get(match.game_id)
        self._hold_adjudication(match, state_before, merged, scope, current_orders)
        self.game_repository.save(state)
        for seat_id, (request_id, batch) in current_orders.items():
            self.repository.audit(match_id, match.phase, seat_id, request_id, batch, "approved", note)
        self.repository.clear_orders(match_id, match.phase)
        match.phase = state.phase
        match.stage = MatchStage.AWAITING_PUBLICATION
        match.version += 1
        self.repository.save_match(match)
        self._resolve_due_telegrams(match_id, state)
        return ApprovalResult(match=match, events=events)

    def _hold_adjudication(self, match: MatchRecord, before: GameState, orders: dict[Side, OrderBatch], scope: str, submitted: dict) -> None:
        self.repository.save_adjudication(match.match_id, {
            "before": before.model_dump(mode="json"),
            "orders": {side.value: batch.model_dump(mode="json") for side, batch in orders.items()},
            "scope": scope,
            "base_sequence": before.events[-1].sequence if before.events else 0,
            "visible_version": match.version,
            "submitted_seat_ids": list(submitted),
            "overrides": {},
            "corrections": [],
            "reroll_rng_counter": self.engine.get(match.game_id).rng_counter,
        })

    def _ensure_loaded(self, game_id: str):
        try:
            state = self.engine.get(game_id)
        except KeyError:
            state = self.game_repository.load(game_id)
            self.engine.games[game_id] = state
        if state.phase == Phase.CONTACT_SETUP:
            for ship_id, position in state.contact_reserve_positions.items():
                ship = state.ships.get(ship_id)
                if ship and ship.position is None:
                    ship.position = position
            state.markers = [marker for marker in state.markers if marker.kind != "contact"]
            state.contact_reserve_positions.clear()
            state.contact_formations.clear()
            state.contact_offsets.clear()
            state.submitted_orders.clear()
            state.sealed_orders.pop(f"{state.turn}:{Phase.CONTACT_SETUP.value}", None)
            state.phase = state.resume_phase or Phase.REINFORCEMENT
            state.resume_phase = None
            match = self.repository.match_for_game(game_id)
            if match:
                self.repository.clear_orders(match.match_id, Phase.CONTACT_SETUP)
                match.phase = state.phase
                match.stage = MatchStage.COLLECTING
                match.version += 1
                self.repository.save_match(match)
            self.game_repository.save(state)
        return state

    @staticmethod
    def _authorize_seat(seat: SeatRecord, user: AuthenticatedUser) -> None:
        if user.is_referee or seat.user_id == user.user_id:
            return
        raise PermissionError("This account is not bound to the requested seat")

    @staticmethod
    def _authorize_judge(match: MatchRecord, user: AuthenticatedUser) -> None:
        if not user.is_referee or user.user_id != match.judge_user_id:
            raise PermissionError("Only the assigned referee may adjudicate this match")

    @staticmethod
    def _check_version(match: MatchRecord, expected_version: int) -> None:
        if match.version != expected_version:
            raise ValueError(f"Stale match version: expected {expected_version}, current {match.version}")

    @staticmethod
    def _marker_ids(batch: OrderBatch) -> list[str]:
        return [order.marker_id for order in batch.contacts] + [order.marker_id for order in batch.contact_movement]

    def _validate_pending_batch(
        self,
        match: MatchRecord,
        seat_id: str,
        side: Side,
        batch: OrderBatch,
        scope: str = "phase",
    ) -> list[str]:
        """Validate a seat order against the currently assembled side order.

        The rules engine validates a complete side in one call.  A platform
        seat is intentionally only a fragment of that side, so coverage errors
        are deferred until all seats are present and the referee approves.
        Any error concerning the submitted fragment itself remains immediate.
        """
        current = self.repository.orders(match.match_id, match.phase)
        state = self._ensure_loaded(match.game_id)
        captain_errors = self._captain_order_errors(state, batch)
        if captain_errors:
            return captain_errors
        batches = [
            stored_batch
            for stored_seat_id, (_, stored_batch) in current.items()
            if stored_seat_id != seat_id and self.repository.seat(stored_seat_id).side == side
        ]
        batches.append(batch)
        if scope == "turn":
            batches = [self._project_turn_batch(item, match.phase) for item in batches]
        merged = self._merge_side_orders(match.phase, side, batches)
        validation = self.engine.validate_orders(match.game_id, merged)
        if validation.valid:
            return captain_errors
        deferred = [error for error in validation.errors if self._is_coverage_error(error)]
        immediate = [error for error in validation.errors if error not in deferred]
        return [*immediate, *captain_errors]

    def _captain_order_errors(self, state, batch: OrderBatch) -> list[str]:
        fields = (
            "reinforcements", "movement", "torpedoes", "gunnery", "illumination",
            "searchlights", "smoke", "formation_setup", "formation_movement",
            "formation_speed_decisions",
        )
        errors: list[str] = []
        for field_name in fields:
            for order in getattr(batch, field_name):
                ship_ids = [getattr(order, "ship_id")] if getattr(order, "ship_id", None) else []
                ship_ids.extend(getattr(order, "ship_ids", []))
                formation_id = getattr(order, "formation_id", None)
                if formation_id and formation_id in state.formations:
                    ship_ids.extend(state.formations[formation_id].ship_ids)
                for ship_id in set(ship_ids):
                    ship = state.ships.get(ship_id)
                    if ship and self.engine.captain_command_lost(state, ship):
                        errors.append(f"{ship.name} 舰长失能：本回合仅由引擎自动按原速直航，不接受其他行动订单")
        for ship_id in batch.smoke_ships:
            ship = state.ships.get(ship_id)
            if ship and self.engine.captain_command_lost(state, ship):
                errors.append(f"{ship.name} 舰长失能：本回合仅由引擎自动按原速直航，不接受其他行动订单")
        return list(dict.fromkeys(errors))

    def _redact_unseen_ship_mentions(
        self, match: MatchRecord, seat: SeatRecord, errors: list[str]
    ) -> list[str]:
        """席位报错不得点名本席位看不见的舰船（舰名或舰船 id）。

        引擎与平台的部分报错会带上舰名或 id（"舰长失能…"、"cannot see <id>"、
        "Seat cannot command ships: [...]"）。若该舰对本席位不可见，玩家就能从报错里
        读出敌方编成乃至隐蔽损伤——2026-10-12 报告"校验炮击不通过时会报出对方船名"即此。

        可见性沿用席位视图的判定（`_seat_can_observe_ship` + 本席位舰船位置），不复制
        任何规则常量；方向 fail-closed：拿不准可见的一律按不可见处理，只保留本席位
        自己指挥的舰船与确实能观察到的船。
        """
        if not errors:
            return errors
        state = self._ensure_loaded(match.game_id)
        own_ids = {ship_id for ship_id in seat.controlled_ship_ids if ship_id in state.ships}
        own_positions = [state.ships[ship_id].position for ship_id in own_ids]
        hidden_contact_mode = bool(state.options.optional_rules.hidden_contacts)
        visibility_radius = self.engine.visibility_radius(state, seat.side)
        seen = set(own_ids)
        for ship_id, ship in state.ships.items():
            if ship_id in own_ids or not ship.position:
                continue
            if self._seat_can_observe_ship(
                state,
                ship,
                seat.side,
                own_positions,
                own_ids,
                hidden_contact_mode=hidden_contact_mode,
                visibility_radius=visibility_radius,
            ):
                seen.add(ship_id)
        unseen = [
            (ship_id, ship.name or "")
            for ship_id, ship in state.ships.items()
            if ship_id not in seen
        ]
        if not unseen:
            return errors
        unseen.sort(key=lambda item: len(item[1]), reverse=True)  # 长名先替换，避免短名切碎长名
        redacted: list[str] = []
        for error in errors:
            text = error
            for ship_id, name in unseen:
                if ship_id:
                    text = text.replace(ship_id, "未识别舰船")
                if name:
                    text = text.replace(name, "未识别舰船")
            redacted.append(text)
        return redacted

    @staticmethod
    def _is_coverage_error(error: str) -> bool:
        return error.startswith(
            (
                "Movement plans must cover every active ship;",
                "Reinforcement orders must cover the available group;",
                "Hidden contact setup must cover all four owned markers",
                "The two real hidden contacts must partition every owned starting ship exactly once",
                "Contact movement must cover all owned contacts;",
                "Formation movement must cover active formations;",
            )
        )

    @staticmethod
    def _assert_order_ownership(batch: OrderBatch, seat: SeatRecord) -> None:
        owned = set(seat.controlled_ship_ids)
        referenced: set[str] = set()
        for field_name in (
            "reinforcements", "movement", "gunnery", "torpedoes", "illumination", "searchlights", "smoke",
        ):
            for order in getattr(batch, field_name, []):
                ship_id = getattr(order, "ship_id", None)
                if ship_id:
                    referenced.add(ship_id)
        for formation in batch.formation_setup:
            referenced.update(formation.ship_ids)
            referenced.add(formation.leader_id)
            referenced.add(formation.flagship_id)
            referenced.add(formation.reserve_flagship_id)
        for formation in batch.formation_movement:
            # Formation IDs are not ship IDs; the engine validates membership.
            if not formation.formation_id:
                raise ValueError("Formation ID is required")
        foreign = referenced - owned
        if foreign:
            raise PermissionError(f"Seat cannot command ships: {sorted(foreign)}")

    @staticmethod
    def _merge_side_orders(phase: Phase, side: Side, batches: list[OrderBatch]) -> OrderBatch:
        merged = OrderBatch(side=side, phase=phase)
        list_fields = (
            "reinforcements", "contacts", "contact_movement", "movement", "gunnery", "torpedoes",
            "smoke_ships", "smoke", "illumination", "searchlights", "formation_setup",
            "formation_movement", "formation_speed_decisions",
        )
        for field_name in list_fields:
            values = []
            for batch in batches:
                values.extend(getattr(batch, field_name))
            setattr(merged, field_name, values)
        return merged

    @staticmethod
    def _project_turn_batch(batch: OrderBatch, phase: Phase) -> OrderBatch:
        projected = OrderBatch(side=batch.side, phase=phase)
        fields_by_phase = {
            Phase.REINFORCEMENT: ("reinforcements",),
            Phase.MOVEMENT_PLANNING: ("movement", "contact_movement", "formation_movement", "formation_speed_decisions"),
            Phase.TORPEDO_PLANNING: ("torpedoes",),
            Phase.GUNNERY: ("gunnery", "smoke_ships", "smoke", "illumination", "searchlights"),
            Phase.CONTACT_SETUP: ("contacts",),
            Phase.FORMATION_SETUP: ("formation_setup",),
        }
        for field_name in fields_by_phase.get(phase, ()):
            setattr(projected, field_name, getattr(batch, field_name))
        return projected

    @staticmethod
    def _marker_visible(marker, own_ids: set[str], visible_enemy_ids: set[str], known_marker_ids: set[str]) -> bool:
        if marker.secret_side is None:
            return (
                marker.ship_id in own_ids
                or marker.target_ship_id in own_ids
                or marker.kind in {"fire", "smoke", "squall"}
                or marker.kind == "gunfire"
                and marker.ship_id in visible_enemy_ids
            )
        return (
            marker.id in known_marker_ids
            or marker.ship_id in own_ids
            or marker.target_ship_id in own_ids
            or marker.target_ship_id in visible_enemy_ids
        )

    @staticmethod
    def _redact_enemy_names(text: str, aliases: dict[str, str]) -> str:
        for name, alias in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
            text = text.replace(name, alias)
        return text

    def _migrate_contact_aliases(self, match_id: str, seat_id: str, state) -> None:
        """Repair aliases created by the old internal-id ordering scheme."""
        existing = self.repository.contact_aliases(match_id, seat_id)
        by_scale: dict[str, list[tuple[int, int, str, str]]] = {}
        for ship_id, (scale, _) in existing.items():
            ship = state.ships.get(ship_id)
            position = ship.position if ship else None
            by_scale.setdefault(scale, []).append(
                (
                    position.q if position else 10**9,
                    position.r if position else 10**9,
                    ship.ship_type if ship else "",
                    ship_id,
                )
            )
        updates: dict[str, str] = {}
        for scale, entries in by_scale.items():
            prefix = ANONYMOUS_PREFIXES.get(scale, "接触")
            for index, (_, _, _, ship_id) in enumerate(sorted(entries)):
                suffix = _anonymous_suffix(index)
                updates[ship_id] = f"{prefix}{suffix}"
        if updates:
            self.repository.rename_contact_aliases(match_id, seat_id, updates)

    @staticmethod
    def _redact_id(value: str | None, aliases: dict[str, str]) -> str | None:
        if value is None:
            return None
        for internal_id, alias in aliases.items():
            if value == internal_id:
                return alias
            value = value.replace(internal_id, alias)
        return value

    @classmethod
    def _redact_payload(cls, payload: dict, aliases: dict[str, str]) -> dict:
        def replace(key: str, value):
            if key.endswith("_side") or key == "secret_side":
                return "contact"
            if isinstance(value, str):
                return cls._redact_id(value, aliases)
            if isinstance(value, list):
                return [replace(key, item) for item in value]
            if isinstance(value, dict):
                return {nested_key: replace(nested_key, item) for nested_key, item in value.items()}
            return value

        return {key: replace(key, value) for key, value in payload.items()}

    @classmethod
    def _redact_event(
        cls,
        event: GameEvent,
        name_aliases: dict[str, str],
        id_aliases: dict[str, str],
    ) -> GameEvent:
        def replace(value):
            if isinstance(value, str):
                return cls._redact_id(cls._redact_enemy_names(value, name_aliases), id_aliases)
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            return value

        return event.model_copy(
            update={
                "message": cls._redact_enemy_names(event.message, name_aliases),
                "payload": cls._redact_payload(replace(event.payload), id_aliases),
            }
        )
