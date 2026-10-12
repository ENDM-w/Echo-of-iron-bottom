import { useEffect, useMemo, useRef, useState } from "react";
import {
  platformApprove,
  platformPublish,
  platformCreateMatch,
  platformJudgeOrder,
  platformJudgePatchShip,
  platformJudgePatchVisibility,
  platformJudgeReroll,
  platformLogin,
  platformJudgeView,
  platformOverrides,
  platformReject,
  platformReview,
  platformSeatView,
  platformTorpedoCandidatePreview,
  platformSubmitOrder,
  platformSubmitTelegram,
  platformTelegramInbox,
  platformJudgeTelegrams,
  platformTurnArchives,
  platformDownloadTurnArchive,
  platformSummary,
  PlatformOrderValidationError,
  type PlatformLogin,
  type PlatformTurnArchive,
  type PlatformReview,
  type PlatformJudgeTelegramRecord,
  type PlatformSummary,
  type PlatformTelegramInbox,
  type PlatformView,
} from "./api";
import { HexMap } from "./HexMap";
import { hexFromLabel, hexLabel } from "./hexGeometry";
import { CustomScenarioEditor } from "./CustomScenarioEditor";
import type { Marker, Ship, Side as GameSide, TorpedoTrack, Wreck } from "./types";
import "./PlatformPortal.css";
import "./PlatformPortalDownloads.css";
import "./TelegramPanel.css";

type Props = { onClose: () => void };
type Side = "axis" | "allies";
type JsonRecord = Record<string, unknown>;
type PlatformTorpedoCandidate = {
  ship_id: string;
  blocked_reason?: string | null;
  launchers: Array<{ launcher_id: string; loaded: number; sides: string[]; angles: string[] }>;
  launch_positions: Array<{ mf: number; hex: { q: number; r: number }; heading: number }>;
  settings: Array<{ index: number; speed: number[]; range: number }>;
};
const pretty = (value: unknown) => JSON.stringify(value, null, 2);
const positionLabel = (value: unknown) => {
  if (!value || typeof value !== "object") return "未部署";
  const position = value as { label?: unknown; q?: unknown; r?: unknown };
  if (typeof position.label === "string") return position.label;
  if (typeof position.q === "number" && typeof position.r === "number")
    return hexLabel({ q: position.q, r: position.r });
  return "未部署";
};
const parseMapHexLabel = (label: string) => {
  try {
    return hexFromLabel(label.trim().toUpperCase());
  } catch {
    return null;
  }
};
const movementPlanCost = (raw: string): { cost: number; error?: string } => {
  const plan = raw.trim().toUpperCase().replaceAll("左", "P").replaceAll("右", "S");
  if (plan === "0") return { cost: 0 };
  const tokens = plan.match(/\d+|PP|SS|LL|RR|P|S|L|R/g) ?? [];
  if (tokens.join("") !== plan) return { cost: 0, error: "航路格式无法识别" };
  const cost = tokens.reduce((total, token) => {
    if (/^\d+$/.test(token)) return total + Number(token);
    return total + (["PP", "SS", "LL", "RR"].includes(token) ? 1 : 0);
  }, 0);
  return Number.isSafeInteger(cost) ? { cost } : { cost: 0, error: "航路 MF 数值超出范围" };
};
const targetLabel = (ship: Ship) =>
  ship.signal_only ? `某标记 · ${positionLabel(ship.position)}` : ship.name;
const saveDownload = (blob: Blob, filename: string) => {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
};
const gunKindLabel: Record<string, string> = {
  primary: "主炮",
  secondary: "副炮",
  tertiary: "高射炮",
};
const captainStatusLabel: Record<string, string> = {
  fit: "正常",
  wounded: "负伤",
  killed: "阵亡",
};
const emptyBatch = (side: Side, phase: string) =>
  pretty({
    side,
    phase,
    movement: [],
    gunnery: [],
    torpedoes: [],
    reinforcements: [],
    smoke_ships: [],
    smoke: [],
    illumination: [],
    searchlights: [],
    formation_setup: [],
    formation_movement: [],
    formation_speed_decisions: [],
    confirmation: { ready: true },
  });
const orderDraftFields = [
  "reinforcements", "movement", "gunnery", "torpedoes",
  "smoke_ships", "smoke", "illumination", "searchlights", "formation_setup",
  "formation_movement", "formation_speed_decisions",
] as const;
const submittedFieldsByPhase: Record<string, string[]> = {
  reinforcement: ["reinforcements"],
  movement_planning: ["movement", "formation_movement", "formation_speed_decisions"],
  torpedo_planning: ["torpedoes"],
  gunnery: ["gunnery", "smoke", "smoke_ships", "illumination", "searchlights"],
  formation_setup: ["formation_setup"],
};
const orderDraftKey = (matchId: string, seatId: string, turn: number) =>
  `ibs-order-draft:${matchId}:${seatId}:${turn}`;
const batchForPhase = (batch: JsonRecord, phase: string): JsonRecord => {
  const submitted: JsonRecord = {
    side: batch.side,
    phase,
    confirmation: batch.confirmation ?? { ready: true },
  };
  for (const field of submittedFieldsByPhase[phase] ?? []) {
    if (batch[field] !== undefined) submitted[field] = batch[field];
  }
  return submitted;
};
const restoreOrderDraft = (side: Side, phase: string, matchId: string, seatId: string, turn: number) => {
  const base = JSON.parse(emptyBatch(side, phase)) as JsonRecord;
  try {
    const saved = JSON.parse(localStorage.getItem(orderDraftKey(matchId, seatId, turn)) ?? "null") as JsonRecord | null;
    if (saved) for (const field of orderDraftFields) {
      if (Array.isArray(saved[field])) base[field] = saved[field];
    }
  } catch {
    // Ignore an unavailable or malformed local draft and keep a clean order.
  }
  return pretty(base);
};

const displayValue = (value: unknown, empty = "无") => value === null || value === undefined || value === "" ? empty : String(value);
const stateRecord = (value: unknown): JsonRecord => value && typeof value === "object" && !Array.isArray(value) ? value as JsonRecord : {};
const stateList = (value: unknown): JsonRecord[] => Array.isArray(value) ? value.map(stateRecord) : [];
const mountPositionLabel: Record<string, string> = { bow: "舰首", stern: "舰尾", midships: "舯部" };
const arcLabel: Record<string, string> = { bow: "舰首", stern: "舰尾", port: "左舷", starboard: "右舷" };
const commandStatusLabel: Record<string, string> = { attached: "保持编队", detaching: "脱离编队", retreating: "撤退中", withdrawn: "已撤离" };
const edgeLabel: Record<string, string> = { north: "北", east: "东", south: "南", west: "西" };
const weaponSummary = (value: unknown) => {
  if (!value || typeof value !== "object") return "无";
  const weapon = value as JsonRecord;
  const kind = weapon.kind === "torpedo" ? "鱼雷武器" : gunKindLabel[String(weapon.kind)] ?? displayValue(weapon.kind);
  return `${kind} · 火力 ${displayValue(weapon.firepower, "0")} · ${displayValue(weapon.caliber, "0")}\" · 弹药 ${displayValue(weapon.ammo, "不限")} · ${weapon.destroyed ? "损毁" : "可用"}`;
};

function RefereeShipCard({ ship }: { ship: JsonRecord }) {
  const guns = stateList(ship.gun_mounts);
  const launchers = stateList(ship.torpedo_launchers);
  const damageRows = Array.isArray(ship.speed_damage_track) ? ship.speed_damage_track as unknown[][] : [];
  const crossed = Array.isArray(ship.speed_damage_crossed) ? ship.speed_damage_crossed as unknown[] : [];
  const core = [
    ["当前位置", positionLabel(ship.position)], ["船体", `${displayValue(ship.hull, "—")}/${displayValue(ship.max_hull, "—")}`],
    ["当前 / 上回合航速", `${displayValue(ship.current_speed, "—")} / ${displayValue(ship.previous_speed, "—")} MF`], ["艏向", displayValue(ship.heading, "—")],
  ];
  const tags = [
    [ship.sunk ? "沉没" : "漂浮", ship.sunk ? "bad" : "good"],
    [Number(ship.fire_markers ?? 0) > 0 ? `起火 ${String(ship.fire_markers)}` : "无火灾", Number(ship.fire_markers ?? 0) > 0 ? "bad" : "good"],
    [ship.fired ? "已开火" : "未开火", ship.fired ? "used" : "good"],
    [ship.smoke ? "正在冒烟" : "无烟幕", ship.smoke ? "used" : "good"],
    [`指挥：${commandStatusLabel[String(ship.command_status)] ?? "保持编队"}`, ship.command_status === "withdrawn" ? "bad" : "good"],
  ];
  const modules = [["射击指挥仪", ship.mfc_destroyed], ["雷达", ship.radar_destroyed], ["舰桥", ship.bridge_destroyed], ["舵机", ship.rudder_destroyed]] as const;
  return (
    <details className="judge-fleet-ship">
      <summary>
        <b>{displayValue(ship.name, displayValue(ship.id))}</b>
        <span>{ship.side === "axis" ? "轴心" : "同盟"} · {displayValue(ship.ship_type, "舰种未知")} · {positionLabel(ship.position)}</span>
        <span>船体 {displayValue(ship.hull, "—")}/{displayValue(ship.max_hull, "—")} · 航速 {displayValue(ship.current_speed, "—")} MF · 艏向 {displayValue(ship.heading, "—")}</span>
      </summary>
      <article className="own-ship judge-state-card">
        <div className="ship-core-status">{core.map(([label, value]) => <span key={label}><b>{label}</b>{value}</span>)}</div>
        <div className="ship-state-tags">{tags.map(([label, kind]) => <span key={label} className={kind}>{label}</span>)}
          {ship.captain_status !== "fit" && <span className="bad">舰长{captainStatusLabel[String(ship.captain_status)] ?? displayValue(ship.captain_status)}</span>}
          {Number(ship.guns_disabled_turns ?? 0) > 0 && <span className="bad">火炮停用 {String(ship.guns_disabled_turns)} 回合</span>}
          {Number(ship.forced_straight_turns ?? 0) > 0 && <span className="used">强制直航 {String(ship.forced_straight_turns)} 回合</span>}
          {Number(ship.forced_circle_turns ?? 0) > 0 && <span className="bad">强制转圈 {String(ship.forced_circle_turns)} 回合</span>}
          {Number(ship.forced_speed_turns ?? 0) > 0 && <span className="used">限速 {displayValue(ship.forced_speed, "—")} · {String(ship.forced_speed_turns)} 回合</span>}
        </div>
        <div className="ship-module-block"><strong>炮位</strong><div className="ship-module-list">
          {guns.length ? guns.map((mount) => <span key={String(mount.id)} className={mount.destroyed ? "bad" : mount.fired_this_phase ? "used" : "good"}>
            {displayValue(mount.id)} · {gunKindLabel[String(mount.kind)] ?? displayValue(mount.kind)} · {displayValue(mount.caliber, "—")}&quot; · 火力 {displayValue(mount.firepower, "—")} · {mountPositionLabel[String(mount.position)] ?? displayValue(mount.position)} · 装甲 {displayValue(mount.armour, "无")} · {mount.destroyed ? "损毁" : mount.fired_this_phase ? "本阶段已射击" : "可用"} · 射界 {Array.isArray(mount.arcs) ? mount.arcs.map((arc) => arcLabel[String(arc)] ?? String(arc)).join("/") : "无"}
          </span>) : <span className="muted">暂无炮位数据</span>}
        </div></div>
        <div className="ship-module-block"><strong>鱼雷发射器</strong><div className="ship-module-list">
          {launchers.length ? launchers.map((launcher) => <span key={String(launcher.id)} className={launcher.destroyed ? "bad" : "good"}>
            {displayValue(launcher.id)} · {launcher.destroyed ? "损毁" : `装填 ${displayValue(launcher.loaded, "0")}/${displayValue(launcher.torpedoes, "0")} · 备雷 ${displayValue(launcher.reloads_remaining, "0")}/${displayValue(launcher.reloads, "0")} · 再装填 ${displayValue(launcher.reload_turns_remaining, "0")} 回合`} · 射界 {Array.isArray(launcher.arcs) ? launcher.arcs.map((arc) => arcLabel[String(arc)] ?? String(arc)).join("/") : "无"}
          </span>) : <span className="muted">无鱼雷发射器</span>}
        </div></div>
        <div className="ship-module-block"><strong>模块与损伤</strong><div className="ship-module-list">
          {modules.map(([label, destroyed]) => <span key={label} className={destroyed ? "bad" : "good"}>{label} · {destroyed ? "损毁" : "正常"}</span>)}
          <span className={ship.captain_status === "killed" ? "bad" : ship.captain_status === "wounded" ? "used" : "good"}>舰长 · {captainStatusLabel[String(ship.captain_status)] ?? "正常"}</span>
          <span className={ship.aircraft ? "good" : "muted"}>舰载机 · {ship.aircraft ? "有" : "无"}</span><span className={ship.radar ? "good" : "muted"}>雷达设备 · {ship.radar ? "有" : "无"}</span>
          {Number(ship.fire_markers ?? 0) > 0 && <span className="bad">火灾来源 · {displayValue(ship.fire_source_attacker)} · 第 {displayValue(ship.fire_source_turn, "—")} 回合</span>}
        </div></div>
        <div className="ship-module-block"><strong>速度损伤轨迹</strong><div className="judge-track-list">
          {damageRows.length ? damageRows.map((row, index) => <span key={index}><b>轨 {index + 1}</b> {Array.isArray(row) ? row.join(" → ") : "—"} · 已越过 {displayValue(crossed[index], "0")}</span>) : <span>无损伤轨迹数据</span>}
        </div></div>
        <div className="ship-module-block"><strong>武备、防护与其他状态</strong><div className="judge-state-facts">
          <span>主炮：{weaponSummary(ship.primary)}</span><span>副炮：{weaponSummary(ship.secondary)}</span><span>鱼雷：{weaponSummary(ship.torpedo)} · 类型 {displayValue(ship.torpedo_type)}</span>
          <span>装甲：舷侧 {displayValue(ship.belt_armor, "0")} · 主炮 {displayValue(ship.primary_armor, "0")} · 副炮 {displayValue(ship.secondary_armor, "0")} · 舰桥 {displayValue(ship.bridge_armor, "0")}</span>
          <span>吨位级 {displayValue(ship.displacement_band)} · 初始最高航速 {displayValue(ship.initial_max_speed)} · 航速周期 {Array.isArray(ship.speed_track) ? ship.speed_track.join(" / ") : "—"} · VP {displayValue(ship.vp)}</span>
          <span>转向限制 {displayValue(ship.turn_limit_degrees, "无")}° · 强制转向 {({ port: "左舷", starboard: "右舷" } as Record<string, string>)[String(ship.forced_turn_side)] ?? displayValue(ship.forced_turn_side)} · 沉没回合 {displayValue(ship.sinking_turn)} · 沉没漂移待结算 {ship.sinking_drift_pending ? "是" : "否"}</span>
          <span>强制直航 {displayValue(ship.forced_straight_turns, "0")} 回合 · 强制转圈 {displayValue(ship.forced_circle_turns, "0")} 回合 · 强制速度 {displayValue(ship.forced_speed)} · 限速剩余 {displayValue(ship.forced_speed_turns, "0")} 回合 · 火炮停用 {displayValue(ship.guns_disabled_turns, "0")} 回合</span>
          <span>增援回合 {displayValue(ship.reinforcement_turn)} · 编队 {displayValue(ship.formation_id)} · 指挥状态 {commandStatusLabel[String(ship.command_status)] ?? displayValue(ship.command_status)} · 撤离边缘 {edgeLabel[String(ship.withdrawal_edge)] ?? displayValue(ship.withdrawal_edge)} · 资产 {displayValue(ship.asset)} · ID {displayValue(ship.id)}</span>
        </div></div>
      </article>
    </details>
  );
}

function PlatformMap({
  view,
  fullMap,
  onSelect,
}: {
  view?: PlatformView;
  fullMap: boolean;
  onSelect: (ship: Ship) => void;
}) {
  if (!view)
    return (
      <section className="platform-panel platform-map-panel platform-map-empty">
        <span className="platform-kicker">TACTICAL MAP</span>
        <h2>{fullMap ? "裁判全图" : "我的战术海图"}</h2>
        <p>载入对局后显示舰船位置、六角坐标和可见航迹。</p>
      </section>
    );
  return (
    <section className="platform-panel platform-map-panel">
      <div className="panel-heading">
        <div>
          <span className="platform-kicker">TACTICAL MAP</span>
          <h2>{fullMap ? "裁判全图" : "我的战术海图"}</h2>
        </div>
        <span className="match-meta">
          第 {view.turn}/{view.max_turns} 回合 · {view.phase}
        </span>
      </div>
      <div className="platform-map-viewport">
        <HexMap
          ships={view.ships as unknown as Ship[]}
          torpedoTracks={view.torpedo_tracks as unknown as TorpedoTrack[]}
          markers={view.markers as unknown as Marker[]}
          wrecks={view.wrecks as unknown as Wreck[]}
          onSelect={onSelect}
          viewerSide={view.side as GameSide}
          visibility={view.visibility}
          observerShipIds={view.own_ship_ids}
          dims={{
            map_columns: view.map_columns,
            map_rows: view.map_rows,
            printed_columns: view.printed_columns,
            printed_rows: view.printed_rows,
            map_infinite: view.map_infinite,
          }}
          showVisibility={!fullMap}
        />
      </div>
    </section>
  );
}

export function PlatformPortal({ onClose }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [session, setSession] = useState<PlatformLogin>();
  const [matchId, setMatchId] = useState("");
  const [summary, setSummary] = useState<PlatformSummary>();
  const [view, setView] = useState<PlatformView>();
  const [judgeView, setJudgeView] = useState<PlatformView>();
  const [review, setReview] = useState<PlatformReview>();
  const [telegramInbox, setTelegramInbox] = useState<PlatformTelegramInbox>();
  const [telegramDraft, setTelegramDraft] = useState("");
  const [judgeTelegrams, setJudgeTelegrams] = useState<PlatformJudgeTelegramRecord[]>([]);
  const [turnArchives, setTurnArchives] = useState<PlatformTurnArchive[]>([]);
  const [selectedSeatId, setSelectedSeatId] = useState("");
  const [orderSeatId, setOrderSeatId] = useState("");
  const [orderText, setOrderText] = useState("");
  const [torpedoDraftCandidates, setTorpedoDraftCandidates] = useState<PlatformTorpedoCandidate[]>();
  const [orderValidationErrors, setOrderValidationErrors] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [scenarioId, setScenarioId] = useState("");
  const [seed, setSeed] = useState("1");
  const [patchShipId, setPatchShipId] = useState("");
  const [patchText, setPatchText] = useState('{"hull": 1}');
  const [rerollSequence, setRerollSequence] = useState("");
  const [visibilityAxis, setVisibilityAxis] = useState("14");
  const [visibilityAllies, setVisibilityAllies] = useState("12");
  const [stormEnabled, setStormEnabled] = useState(false);
  const [hiddenContactsEnabled, setHiddenContactsEnabled] = useState(false);
  const [scenarioWorkshopOpen, setScenarioWorkshopOpen] = useState(false);
  const [actionShipId, setActionShipId] = useState("");
  const [actionTargetId, setActionTargetId] = useState("");
  const [movementPlan, setMovementPlan] = useState("0");
  const [movementSpeed, setMovementSpeed] = useState("");
  const [torpedoLauncherId, setTorpedoLauncherId] = useState("");
  const [torpedoCount, setTorpedoCount] = useState("1");
  const [torpedoLaunchAtMf, setTorpedoLaunchAtMf] = useState("");
  const [torpedoLaunchSide, setTorpedoLaunchSide] = useState("");
  const [torpedoLaunchAngle, setTorpedoLaunchAngle] = useState("");
  const [smokeShipId, setSmokeShipId] = useState("");
  const [smokeDeploy, setSmokeDeploy] = useState(true);
  const [torpedoSettingIndex, setTorpedoSettingIndex] = useState("");
  const [illuminationHexLabel, setIlluminationHexLabel] = useState("");
  const [searchlightTargetId, setSearchlightTargetId] = useState("");
  const playerViewRef = useRef<PlatformView | undefined>(undefined);
  const pollInFlight = useRef(false);
  playerViewRef.current = view;
  const ownSeats = useMemo(
    () =>
      summary?.seats.filter((seat) => seat.user_id === session?.user_id) ?? [],
    [summary, session],
  );
  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await action();
    } catch (reason) {
      const detail = String(reason);
      if (detail.includes("Invalid or expired session") || detail.includes("Bearer token required")) {
        setUsername((current) => current || session?.username || "");
        setPassword("");
        setSession(undefined);
        setSummary(undefined);
        setView(undefined);
        setJudgeView(undefined);
        setReview(undefined);
        setError("登录会话已失效，请重新登录后继续；当前想定 ID 已保留。");
      } else {
        setError(detail);
      }
    } finally {
      setBusy(false);
    }
  };
  const syncJudgeVisibility = (next: PlatformView) => {
    if (next.visibility_axis != null) setVisibilityAxis(String(next.visibility_axis));
    if (next.visibility_allies != null) setVisibilityAllies(String(next.visibility_allies));
    setStormEnabled(Boolean(next.storm));
    setHiddenContactsEnabled(Boolean(next.optional_rules?.hidden_contacts));
  };
  const login = () =>
    void run(async () => {
      const next = await platformLogin(username, password);
      setSession(next);
      setMessage(`已登录：${next.username}`);
    });
  const loadSummary = () => {
    if (!session || !matchId.trim()) return;
    void run(async () => {
      const next = await platformSummary(session.token, matchId.trim());
      setSummary(next);
      setScenarioId(next.match.scenario_id);
      const seat =
        next.seats.find((item) => item.user_id === session.user_id) ??
        next.seats[0];
      if (seat) {
        setSelectedSeatId(seat.seat_id);
        setOrderSeatId(seat.seat_id);
        if (!session.is_referee) {
          const seatView = await platformSeatView(
              session.token,
              next.match.match_id,
              seat.seat_id,
            );
          setView(seatView);
          setOrderText(restoreOrderDraft(
            seat.side,
            seatView.phase,
            next.match.match_id,
            seat.seat_id,
            seatView.turn,
          ));
        } else {
          setOrderText(emptyBatch(seat.side, next.match.phase));
        }
        if (!session.is_referee) {
          const inbox = await platformTelegramInbox(session.token, next.match.match_id, seat.seat_id);
          setTelegramInbox(inbox);
          setTelegramDraft(inbox.sent_message ?? "");
        }
      }
      if (session.is_referee) {
        setReview(await platformReview(session.token, next.match.match_id));
        setJudgeTelegrams(await platformJudgeTelegrams(session.token, next.match.match_id));
        setTurnArchives(await platformTurnArchives(session.token, next.match.match_id));
        const nextJudge = await platformJudgeView(session.token, next.match.match_id);
        setJudgeView(nextJudge);
        syncJudgeVisibility(nextJudge);
      }
    });
  };
  const refreshView = (seatId = selectedSeatId) => {
    if (!session || !summary || !seatId) return;
    void run(async () => {
      const next = await platformSeatView(
        session.token,
        summary.match.match_id,
        seatId,
      );
      setView(next);
      const inbox = await platformTelegramInbox(session.token, summary.match.match_id, seatId);
      setTelegramInbox(inbox);
      setTelegramDraft(inbox.sent_message ?? "");
      const seat = summary.seats.find((item) => item.seat_id === seatId);
      if (seat) setOrderText(restoreOrderDraft(
        seat.side,
        next.phase,
        summary.match.match_id,
        seat.seat_id,
        next.turn,
      ));
    });
  };
  useEffect(() => {
    if (!session || session.is_referee || !summary || !selectedSeatId) return;
    let active = true;
    const refreshPublishedView = async () => {
      if (pollInFlight.current) return;
      pollInFlight.current = true;
      try {
        const match = summary.match;
        const next = await platformSeatView(session.token, match.match_id, selectedSeatId);
        if (!active) return;
        const previous = playerViewRef.current;
        if (!previous || next.version !== previous.version) {
          setView(next);
          setSummary((current) => current && current.match.match_id === match.match_id
            ? { ...current, match: { ...current.match, phase: next.phase, stage: next.stage, version: next.version } }
            : current);
          const released = previous?.stage === "awaiting_publication"
            && next.stage !== "awaiting_publication";
          const advanced = previous != null
            && (previous.turn !== next.turn || previous.phase !== next.phase);
          if (released || advanced) {
            const seat = summary.seats.find((item) => item.seat_id === selectedSeatId);
            if (seat) setOrderText(restoreOrderDraft(seat.side, next.phase, match.match_id, seat.seat_id, next.turn));
            const inbox = await platformTelegramInbox(session.token, match.match_id, selectedSeatId);
            if (!active) return;
            setTelegramInbox(inbox);
            setTelegramDraft(inbox.sent_message ?? "");
            if (released) setMessage(`裁判已发布第 ${next.turn} 回合裁决，地图与状态已更新`);
          }
        }
      } catch {
        // Keep the last published view during transient network errors.
      } finally {
        pollInFlight.current = false;
      }
    };
    // 轮询间隔直接决定服务端请求量：每个请求都会写 nginx 访问日志 + uvicorn 访问日志
    // + SQLite。2026-10-12 玩家活跃（约 250 请求/分钟）时，1.8s 轮询产生的 139 字节小随机写
    // 把低 IOPS 云盘的写入队列打满，导致整机卡死。放宽到 4s：回合制对局足够实时，
    // 同时把日志与写盘压力减半以上。
    const timer = window.setInterval(() => void refreshPublishedView(), 4000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [session, summary?.match.match_id, selectedSeatId]);
  const createMatch = () => {
    if (!session) return;
    if (!scenarioId.trim().startsWith("IBS-CUSTOM-")) {
      setError("请先打开剧本工坊，载入或制作想定并保存；只有带席位账密的自定义想定才能创建多人对局。");
      return;
    }
    void run(async () => {
      const next = await platformCreateMatch(session.token, {
        scenario_id: scenarioId,
        seed: Number(seed) || 1,
        judge_user_id: session.user_id,
      });
      const nextSummary = await platformSummary(session.token, next.match_id);
      setMatchId(next.match_id);
      setSummary(nextSummary);
      setReview(await platformReview(session.token, next.match_id));
      setJudgeTelegrams(await platformJudgeTelegrams(session.token, next.match_id));
      setTurnArchives(await platformTurnArchives(session.token, next.match_id));
      const nextJudge = await platformJudgeView(session.token, next.match_id);
      setJudgeView(nextJudge);
      syncJudgeVisibility(nextJudge);
      let archiveMessage = "";
      try {
        const archive = await platformDownloadTurnArchive(session.token, next.match_id, nextJudge.turn);
        saveDownload(archive, `iron-bottom-sound-${next.match_id}-turn-${String(nextJudge.turn).padStart(2, "0")}.ibs-save.json`);
        archiveMessage = `，第 ${nextJudge.turn} 回合存档已下载`;
      } catch {
        archiveMessage = "；本回合存档可在下方手动下载";
      }
      setMessage(`对局已创建：${next.match_id}，自动生成 ${nextSummary.seats.length} 个席位${archiveMessage}`);
    });
  };
  const downloadSeatCredentials = () => {
    if (!summary || !session?.is_referee) return;
    const lines = [
      "铁底湾多人网推 - 玩家席位账密",
      `對局 ID：${summary.match.match_id}`,
      `想定：${summary.match.scenario_id}`,
      "",
      ...summary.seats.flatMap((seat, index) => [
        `席位 ${index + 1}`,
        `陣營：${seat.side === "axis" ? "軸心" : "同盟"}`,
        `艦船 ID：${seat.controlled_ship_ids.join(", ") || "無"}`,
        `账号：${seat.username ?? "裁判代理（无需登录）"}`,
        `密码：${seat.credential_password ?? (seat.controller_type === "referee_proxy" ? "由裁判代理控制" : "历史席位未保存明文密码")}`,
        "",
      ]),
    ];
    const filename = `铁底湾席位账密-${summary.match.match_id}.txt`;
    saveDownload(new Blob(["\uFEFF", lines.join("\r\n")], { type: "text/plain;charset=utf-8" }), filename);
    setMessage(`已下载席位账密：${filename}`);
  };
  const refreshTurnArchives = () => {
    if (!session?.is_referee || !summary) return;
    void run(async () => {
      setTurnArchives(await platformTurnArchives(session.token, summary.match.match_id));
      setMessage("回合存档列表已刷新");
    });
  };
  const downloadTurnArchive = (turn: number) => {
    if (!session?.is_referee || !summary) return;
    void run(async () => {
      const blob = await platformDownloadTurnArchive(session.token, summary.match.match_id, turn);
      const filename = `iron-bottom-sound-${summary.match.match_id}-turn-${String(turn).padStart(2, "0")}.ibs-save.json`;
      saveDownload(blob, filename);
      setMessage(`已下载第 ${turn} 回合存档`);
    });
  };
  /**
   * 提交前用「当前移动草稿」重取一次鱼雷候选，把每条鱼雷行的 launch_hex/bearing 校正到其
   * launch_at_mf 对应的航路位置。避免"先用预览写单、之后又改移动草稿"留下的陈旧发射格被引擎拒绝
   * （engine.py:1801 要求 launch_hex 等于该 MF 处的航路位置）。预览不可用时保持原样，不阻塞提交。
   */
  const resyncTorpedoLaunchHex = async (batch: JsonRecord) => {
    if (!session || !summary || !selectedSeatId) return;
    const rows = Array.isArray(batch.torpedoes) ? batch.torpedoes as JsonRecord[] : [];
    const movement = Array.isArray(batch.movement) ? batch.movement : [];
    if (!rows.length || !movement.length) return;
    let candidates: PlatformTorpedoCandidate[];
    try {
      candidates = await platformTorpedoCandidatePreview(
        session.token, summary.match.match_id, selectedSeatId, movement,
      ) as unknown as PlatformTorpedoCandidate[];
    } catch {
      return;
    }
    const positionsByShip = new Map<string, Map<number, { hex: { q: number; r: number }; heading: number }>>();
    for (const candidate of candidates) {
      const positions = new Map<number, { hex: { q: number; r: number }; heading: number }>();
      for (const position of candidate.launch_positions) {
        positions.set(position.mf, { hex: position.hex, heading: position.heading });
      }
      positionsByShip.set(candidate.ship_id, positions);
    }
    for (const row of rows) {
      const position = positionsByShip.get(String(row.ship_id ?? ""))?.get(Number(row.launch_at_mf));
      if (position) {
        row.launch_hex = position.hex;
        row.bearing = position.heading;
      }
    }
  };

  // JSON.stringify 会把 NaN/Infinity 写成 null，而 null 会让后端的请求体校验直接返回 422；
  // 422 的 detail 是对象数组，join 之后玩家只看到 "[object Object]"（见 api.ts 的翻译修复）。
  // 这里在提交前先把这类"必然被拒"的行挡下来，并明确指出问题在哪。
  const collectNonFinitePaths = (value: unknown, path = "", found: string[] = []): string[] => {
    if (typeof value === "number") {
      if (!Number.isFinite(value)) found.push(path || "(根)");
    } else if (Array.isArray(value)) {
      value.forEach((item, index) => collectNonFinitePaths(item, `${path}[${index}]`, found));
    } else if (value && typeof value === "object") {
      for (const [key, item] of Object.entries(value as JsonRecord)) {
        collectNonFinitePaths(item, path ? `${path}.${key}` : key, found);
      }
    }
    return found;
  };
  const dropTorpedoRowsWithoutHex = (batch: JsonRecord): number => {
    const rows = Array.isArray(batch.torpedoes) ? (batch.torpedoes as JsonRecord[]) : [];
    const keep = rows.filter((row) => {
      const hex = row.launch_hex as JsonRecord | null | undefined;
      return Boolean(hex && Number.isInteger(Number(hex.q)) && Number.isInteger(Number(hex.r)));
    });
    if (keep.length !== rows.length) batch.torpedoes = keep;
    return rows.length - keep.length;
  };

  const submitOrder = (ignoreValidationErrors: boolean) => {
    if (!session || !summary || !view || !selectedSeatId) return;
    void run(async () => {
      let next;
      try {
        const submittedBatch = view.phase === "reinforcement"
          ? JSON.parse(orderText) as JsonRecord
          : batchForPhase(JSON.parse(orderText) as JsonRecord, view.phase);
        const incapacitatedShipIds = new Set(
          (view.ships as unknown as Ship[])
            .filter((ship) => view.own_ship_ids.includes(ship.id) && ship.captain_command_lost)
            .map((ship) => ship.id),
        );
        if (incapacitatedShipIds.size) {
          for (const key of ["reinforcements", "movement", "torpedoes", "gunnery", "illumination", "searchlights", "smoke"]) {
            if (Array.isArray(submittedBatch[key])) {
              submittedBatch[key] = (submittedBatch[key] as JsonRecord[])
                .filter((order) => !incapacitatedShipIds.has(String(order.ship_id ?? "")));
            }
          }
          if (Array.isArray(submittedBatch.smoke_ships)) {
            submittedBatch.smoke_ships = (submittedBatch.smoke_ships as string[])
              .filter((shipId) => !incapacitatedShipIds.has(shipId));
          }
        }
        await resyncTorpedoLaunchHex(submittedBatch);
        // 提交前自检：后端请求体校验失败只会回一个对象数组（此前前端显示成 "[object Object]"），
        // 所以先把"必然被拒"的载荷挡下来并说明原因，别让玩家面对一句无意义的报错。
        const nonFinitePaths = collectNonFinitePaths(submittedBatch);
        if (nonFinitePaths.length) {
          setError(`订单里有非法数值（NaN/无穷），提交必被后端拒绝，请修正后重试：${nonFinitePaths.slice(0, 4).join("、")}`);
          return;
        }
        const droppedTorpedoRows = dropTorpedoRowsWithoutHex(submittedBatch);
        if (droppedTorpedoRows) {
          setMessage(`已剔除 ${droppedTorpedoRows} 条缺少合法发射格的陈旧鱼雷行（请用「鱼雷发射草稿」重新写入），其余行动照常提交。`);
        }
        next = await platformSubmitOrder(
          session.token,
          summary.match.match_id,
          selectedSeatId,
          {
            phase: view.phase,
            batch: submittedBatch,
            scope: view.phase === "reinforcement" ? "turn" : "phase",
            request_id: `web-${Date.now()}`,
            ignore_validation_errors: ignoreValidationErrors,
          },
        );
      } catch (reason) {
        if (reason instanceof PlatformOrderValidationError) {
          setOrderValidationErrors(reason.errors);
          return;
        }
        throw reason;
      }
      setOrderValidationErrors([]);
      setView((current) => current ? {
        ...current,
        stage: next.stage,
        version: next.version,
        seat_has_submitted: true,
      } : current);
      const hasIncapacitatedShip = (view.ships as unknown as Ship[])
        .some((ship) => view.own_ship_ids.includes(ship.id) && ship.captain_command_lost);
      setMessage(hasIncapacitatedShip
        ? "本回合行动已成功提交。舰长失能的舰船已移除人工订单，将按原速直航；其余行动等待裁判审理。"
        : "本回合行动已成功提交，等待裁判审理。你仍可修改并重新提交。");
      try {
        const [nextSummary, nextView] = await Promise.all([
          platformSummary(session.token, summary.match.match_id),
          platformSeatView(session.token, summary.match.match_id, selectedSeatId),
        ]);
        setSummary(nextSummary);
        setView(nextView);
      } catch (reason) {
        setMessage(`本回合行动已成功提交；状态刷新失败：${String(reason)}`);
      }
    });
  };
  const refreshTelegrams = () => {
    if (!session || !summary) return;
    void run(async () => {
      if (session.is_referee) {
        setJudgeTelegrams(await platformJudgeTelegrams(session.token, summary.match.match_id));
      } else if (selectedSeatId) {
        const inbox = await platformTelegramInbox(session.token, summary.match.match_id, selectedSeatId);
        setTelegramInbox(inbox);
        setTelegramDraft(inbox.sent_message ?? "");
      }
    });
  };
  const submitTelegram = () => {
    if (!session || !summary || !selectedSeatId) return;
    void run(async () => {
      const sent = await platformSubmitTelegram(session.token, summary.match.match_id, selectedSeatId, telegramDraft);
      const inbox = await platformTelegramInbox(session.token, summary.match.match_id, selectedSeatId);
      setTelegramInbox(inbox);
      setTelegramDraft(inbox.sent_message ?? "");
      setMessage(`第 ${sent.sent_turn} 回合电报已封存，将在下回合逐席位判定`);
    });
  };
  const refreshReview = () => {
    if (!session || !summary) return;
    void run(async () => {
      setSummary(await platformSummary(session.token, summary.match.match_id));
      setReview(await platformReview(session.token, summary.match.match_id));
      setJudgeTelegrams(await platformJudgeTelegrams(session.token, summary.match.match_id));
      setJudgeView(
        await platformJudgeView(session.token, summary.match.match_id),
      );
    });
  };
  const selectReviewSeat = (seatId: string) => {
    if (!summary) return;
    const seat = summary.seats.find((item) => item.seat_id === seatId);
    if (!seat) return;
    setOrderSeatId(seatId);
    const order = review?.orders.find((item) => item.seat_id === seatId);
    setOrderText(
      order ? pretty(order.batch) : emptyBatch(seat.side, summary.match.phase),
    );
    setOrderValidationErrors([]);
    setMessage(
      order
        ? `正在审理 ${seat.username ?? seat.seat_id} 的订单`
        : `${seat.username ?? seat.seat_id} 尚未提交订单`,
    );
  };
  const judgeOrder = () => {
    if (!session || !summary || !orderSeatId) return;
    void run(async () => {
      const next = await platformJudgeOrder(
        session.token,
        summary.match.match_id,
        orderSeatId,
        {
          phase: summary.match.phase,
          batch: JSON.parse(orderText) as JsonRecord,
          scope: review?.orders.find((item) => item.seat_id === orderSeatId)?.scope
            ?? (summary.match.phase === "reinforcement" ? "turn" : "phase"),
          reason: note || "裁判修订行动",
          expected_version: summary.match.version,
        },
      );
      setSummary({ ...summary, match: next });
      setReview(await platformReview(session.token, summary.match.match_id));
      setMessage(`订单覆盖完成，版本 ${next.version}`);
    });
  };
  const quickOverrideOrder = () => {
    if (!session || !summary || !selectedOrderSeat || !judgeView) return;
    const phase = summary.match.phase;
    if (!["reinforcement", "movement_planning", "gunnery"].includes(phase)) return;
    const batch = JSON.parse(emptyBatch(selectedOrderSeat.side, phase)) as JsonRecord;
    const ships = judgeShips.filter((ship) => selectedOrderSeat.controlled_ship_ids.includes(ship.id));
    const incapacitated = (ship: Ship) => Boolean(ship.captain_command_lost);
    if (phase === "reinforcement") {
      const currentDraft = orderDraft;
      batch.reinforcements = Array.isArray(currentDraft?.reinforcements) ? currentDraft.reinforcements : [];
    }
    if (phase === "reinforcement" || phase === "movement_planning") {
      const reinforcementShipIds = new Set(
        ((batch.reinforcements as JsonRecord[] | undefined) ?? []).map((order) => String(order.ship_id ?? "")),
      );
      batch.movement = ships
        .filter((ship) => !incapacitated(ship) && (ship.position || reinforcementShipIds.has(ship.id)) && !ship.sunk)
        .map((ship) => ({
          ship_id: ship.id,
          plan: String(ship.max_legal_speed ?? 0),
          speed: ship.max_legal_speed ?? 0,
        }));
    }
    if (phase === "reinforcement" || phase === "gunnery") {
      const candidates = judgeView.gunnery_candidates ?? [];
      batch.gunnery = ships.flatMap((ship) => {
        if (incapacitated(ship)) return [];
        const targetId = judgeView.previous_gunnery_targets?.[ship.id];
        if (!targetId) return [];
        const legalTarget = candidates
          .find((candidate) => candidate.ship_id === ship.id)
          ?.targets.find((target) => target.target_id === targetId);
        if (!legalTarget || legalTarget.mount_ids.length === 0) return [];
        return [{
          ship_id: ship.id,
          primary_target: targetId,
          secondary_target: null,
          searchlight_target: null,
          mounts: legalTarget.mount_ids.map((mountId) => ({ mount_id: mountId, target_id: targetId })),
        }];
      });
    }
    const scope = phase === "reinforcement" ? "turn" : "phase";
    void run(async () => {
      const next = await platformJudgeOrder(session.token, summary.match.match_id, selectedOrderSeat.seat_id, {
        phase,
        batch,
        scope,
        reason: "裁判快速覆盖：最高合法速度直航，并复用上次炮击目标",
        expected_version: summary.match.version,
      });
      setSummary({ ...summary, match: next });
      setOrderText(pretty(batch));
      setReview(await platformReview(session.token, summary.match.match_id));
      setMessage("快速覆盖完成：最高合法速度直航；仅对仍可合法炮击的上次目标开火，其余行动已清空。");
    });
  };
  const approve = () => {
    if (!session || !summary) return;
    void run(async () => {
      await platformApprove(session.token, summary.match.match_id, {
        expected_version: summary.match.version,
        note: note || undefined,
      });
      const next = await platformSummary(session.token, summary.match.match_id);
      setSummary(next);
      setReview(await platformReview(session.token, summary.match.match_id));
      setJudgeTelegrams(await platformJudgeTelegrams(session.token, summary.match.match_id));
      setTurnArchives(await platformTurnArchives(session.token, summary.match.match_id));
      const nextJudge = await platformJudgeView(session.token, summary.match.match_id);
      setJudgeView(nextJudge);
      syncJudgeVisibility(nextJudge);
      setMessage(`第 ${nextJudge.adjudication_turn ?? nextJudge.turn} 回合已裁决，待检查骰点并发布给玩家`);
    });
  };
  const publish = () => {
    if (!session || !summary) return;
    void run(async () => {
      const previousTurn = judgeView?.turn;
      const published = await platformPublish(session.token, summary.match.match_id, {
        expected_version: summary.match.version,
      });
      setSummary({ ...summary, match: published });
      setReview(await platformReview(session.token, summary.match.match_id));
      setJudgeTelegrams(await platformJudgeTelegrams(session.token, summary.match.match_id));
      setTurnArchives(await platformTurnArchives(session.token, summary.match.match_id));
      const nextJudge = await platformJudgeView(session.token, summary.match.match_id);
      setJudgeView(nextJudge);
      syncJudgeVisibility(nextJudge);
      if (previousTurn != null && nextJudge.turn > previousTurn) {
        try {
          const archive = await platformDownloadTurnArchive(session.token, summary.match.match_id, nextJudge.turn);
          saveDownload(archive, `iron-bottom-sound-${summary.match.match_id}-turn-${String(nextJudge.turn).padStart(2, "0")}.ibs-save.json`);
          setMessage(`第 ${nextJudge.turn} 回合结果已发布给玩家，最终存档已下载`);
        } catch {
          setMessage(`第 ${nextJudge.turn} 回合结果已发布给玩家；存档自动下载失败，可在裁判下载区重下`);
        }
      } else {
        setMessage(`第 ${nextJudge.turn} 回合裁决结果已发布给玩家`);
      }
    });
  };
  const reject = () => {
    if (!session || !summary) return;
    void run(async () => {
      const next = await platformReject(session.token, summary.match.match_id, {
        expected_version: summary.match.version,
        reason: note || "退回重填",
      });
      setSummary({ ...summary, match: next });
      setReview(await platformReview(session.token, summary.match.match_id));
      setMessage("已退回当前阶段");
    });
  };
  const patchShip = () => {
    if (!session || !summary || !patchShipId.trim()) return;
    void run(async () => {
      const result = await platformJudgePatchShip(
        session.token,
        summary.match.match_id,
        patchShipId.trim(),
        {
          patch: JSON.parse(patchText) as JsonRecord,
          reason: note || "裁判状态修正",
          expected_version: summary.match.version,
        },
      );
      setSummary({
        ...summary,
        match: result.match as PlatformSummary["match"],
      });
      setMessage(`舰船状态已覆盖：${patchShipId.trim()}`);
    });
  };
  const patchVisibility = () => {
    if (!session || !summary) return;
    const axis = Number(visibilityAxis);
    const allies = Number(visibilityAllies);
    if (!Number.isFinite(axis) || !Number.isFinite(allies) || axis < 0 || allies < 0) {
      setError("视野半径必须是 0 到 99 的数字");
      return;
    }
    void run(async () => {
      const result = await platformJudgePatchVisibility(
        session.token,
        summary.match.match_id,
        {
          axis,
          allies,
          storm: stormEnabled,
          hidden_contacts: hiddenContactsEnabled,
          reason: note || "裁判调整视野设置",
          expected_version: summary.match.version,
        },
      );
      setSummary({ ...summary, match: result.match });
      const nextJudge = await platformJudgeView(session.token, summary.match.match_id);
      setJudgeView(nextJudge);
      syncJudgeVisibility(nextJudge);
      setMessage(`视野设置已更新：轴心 ${axis} 格、同盟 ${allies} 格${stormEnabled ? "，风暴降临已开启" : ""}，隐藏接敌${hiddenContactsEnabled ? "已开启" : "已关闭"}`);
    });
  };
  const reroll = () => {
    if (!session || !summary || !rerollSequence.trim()) return;
    void run(async () => {
      const result = await platformJudgeReroll(session.token, summary.match.match_id, {
        event_sequence: Number(rerollSequence),
        reason: note || "裁判重投",
        expected_version: summary.match.version,
      });
      const [nextSummary, nextJudge] = await Promise.all([
        platformSummary(session.token, summary.match.match_id),
        platformJudgeView(session.token, summary.match.match_id),
      ]);
      setSummary(nextSummary);
      setJudgeView(nextJudge);
      setJudgeTelegrams(await platformJudgeTelegrams(session.token, summary.match.match_id));
      setTurnArchives(await platformTurnArchives(session.token, summary.match.match_id));
      syncJudgeVisibility(nextJudge);
      const revised = result.event as JsonRecord;
      setRerollSequence(String(revised.sequence ?? ""));
      setMessage(`已重骰并重新裁决：${String(revised.message ?? `事件 #${rerollSequence}`)}${Number(result.invalidated_rerolls ?? 0) ? `；另有 ${String(result.invalidated_rerolls)} 个后续重骰因事件变化失效` : ""}`);
    });
  };
  const signOut = () => {
    setSession(undefined);
    setSummary(undefined);
    setView(undefined);
    setJudgeView(undefined);
    setReview(undefined);
    setError("");
  };

  const ownShips = useMemo(() => {
    if (!view) return [] as Ship[];
    return (view.ships as unknown as Ship[]).filter(
      (ship) => view.own_ship_ids.includes(ship.id) && !ship.sunk,
    );
  }, [view]);
  const visibleTargets = useMemo(() => {
    if (!view) return [] as Ship[];
    return (view.ships as unknown as Ship[]).filter(
      (ship) => !ship.sunk && Boolean(ship.position) && !view.own_ship_ids.includes(ship.id),
    );
  }, [view]);
  const selectedActionShip =
    ownShips.find((ship) => ship.id === actionShipId) ?? ownShips[0];
  const movementCostPreview = movementPlanCost(movementPlan);
  const movementPlanWarnings = selectedActionShip ? [
    movementCostPreview.error,
    movementCostPreview.cost < (selectedActionShip.min_legal_speed ?? 0)
      || movementCostPreview.cost > (selectedActionShip.max_legal_speed ?? Number.POSITIVE_INFINITY)
      ? `航路消耗 ${movementCostPreview.cost} MF，超出本回合合法范围 ${selectedActionShip.min_legal_speed ?? "—"}–${selectedActionShip.max_legal_speed ?? "—"} MF。`
      : undefined,
    movementSpeed.trim() !== "" && Number(movementSpeed) !== movementCostPreview.cost
      ? `声明速度 ${movementSpeed} MF 与航路消耗 ${movementCostPreview.cost} MF 不一致。`
      : undefined,
  ].filter((warning): warning is string => Boolean(warning)) : [];
  const captainCommandLost = Boolean(selectedActionShip?.captain_command_lost);
  const selectedActionTarget =
    visibleTargets.find((ship) => ship.id === actionTargetId) ?? visibleTargets[0];
  const selectedLauncher = selectedActionShip?.torpedo_launchers.find(
    (launcher) => launcher.id === torpedoLauncherId,
  ) ?? selectedActionShip?.torpedo_launchers.find((launcher) => !launcher.destroyed && launcher.loaded > 0);
  const availableMounts = selectedActionShip?.gun_mounts.filter(
    (mount) => !mount.destroyed && !mount.fired_this_phase,
  ) ?? [];
  const gunneryCandidate = (view?.gunnery_candidates ?? []).find(
    (candidate) => candidate.ship_id === selectedActionShip?.id,
  );
  const gunneryTarget = gunneryCandidate?.targets.find(
    (candidate) => candidate.target_id === selectedActionTarget?.id,
  );
  const fireableMountIds = new Set(gunneryTarget?.mount_ids ?? []);
  const fireableMounts = availableMounts.filter((mount) => fireableMountIds.has(mount.id));
  const illuminationDrafts = useMemo(() => {
    try {
      const batch = JSON.parse(orderText) as JsonRecord;
      return Array.isArray(batch.illumination)
        ? batch.illumination.filter(
            (item): item is JsonRecord => Boolean(item && typeof item === "object"),
          )
        : [];
    } catch {
      return [];
    }
  }, [orderText]);
  const searchlightDrafts = useMemo(() => {
    try {
      const batch = JSON.parse(orderText) as JsonRecord;
      return Array.isArray(batch.searchlights)
        ? batch.searchlights.filter(
            (item): item is JsonRecord => Boolean(item && typeof item === "object"),
          )
        : [];
    } catch {
      return [];
    }
  }, [orderText]);
  const torpedoDrafts = useMemo(() => {
    try {
      const batch = JSON.parse(orderText) as JsonRecord;
      return Array.isArray(batch.torpedoes)
        ? batch.torpedoes.filter(
            (item): item is JsonRecord => Boolean(item && typeof item === "object"),
          )
        : [];
    } catch {
      return [];
    }
  }, [orderText]);
  useEffect(() => {
    if (!session || session.is_referee || !summary || !selectedSeatId || view?.phase !== "reinforcement") {
      setTorpedoDraftCandidates(undefined);
      return;
    }
    let movement: unknown[];
    try {
      const batch = JSON.parse(orderText) as JsonRecord;
      movement = Array.isArray(batch.movement) ? batch.movement : [];
    } catch {
      setTorpedoDraftCandidates([]);
      return;
    }
    if (movement.length === 0) {
      setTorpedoDraftCandidates(undefined);
      return;
    }
    let active = true;
    setTorpedoDraftCandidates([]);
    const timer = window.setTimeout(() => {
      void platformTorpedoCandidatePreview(
        session.token, summary.match.match_id, selectedSeatId, movement,
      ).then((candidates) => {
        if (active) setTorpedoDraftCandidates(candidates as unknown as PlatformTorpedoCandidate[]);
      }).catch(() => {
        if (active) setTorpedoDraftCandidates([]);
      });
    }, 180);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [orderText, session, summary, selectedSeatId, view?.phase]);
  const selectedLauncherUsed = torpedoDrafts
    .filter((item) => item.ship_id === selectedActionShip?.id && item.launcher_id === selectedLauncher?.id)
    .reduce((total, item) => total + Number(item.count ?? 0), 0);
  const selectedLauncherRemaining = Math.max(0, (selectedLauncher?.loaded ?? 0) - selectedLauncherUsed);
  const usedIlluminationMountIds = new Set(
    illuminationDrafts
      .filter((item) => item.ship_id === selectedActionShip?.id)
      .map((item) => String(item.mount_id ?? "")),
  );
  const illuminationMountChoice = selectedActionShip?.gun_mounts
    .filter((mount) =>
      ["primary", "secondary"].includes(mount.kind)
      && !mount.destroyed
      && !mount.fired_this_phase
      && !usedIlluminationMountIds.has(mount.id),
    )
    .sort((left, right) =>
      Number(fireableMountIds.has(left.id)) - Number(fireableMountIds.has(right.id))
      || left.firepower - right.firepower
      || left.id.localeCompare(right.id),
    )[0];
  const torpedoCandidates = torpedoDraftCandidates
    ?? (view?.torpedo_candidates ?? []) as unknown as PlatformTorpedoCandidate[];
  const selectedTorpedoCandidate = torpedoCandidates.find(
    (candidate) => candidate.ship_id === selectedActionShip?.id,
  );
  const selectedLauncherCandidate = selectedTorpedoCandidate?.launchers.find(
    (launcher) => launcher.launcher_id === selectedLauncher?.id,
  ) ?? selectedTorpedoCandidate?.launchers[0];
  const launchPositions = selectedTorpedoCandidate?.launch_positions ?? [];
  const selectedLaunchPosition = launchPositions.find(
    (position) => String(position.mf) === torpedoLaunchAtMf,
  ) ?? launchPositions[0];
  const launchSides = selectedLauncherCandidate?.sides ?? [];
  const launchAngles = selectedLauncherCandidate?.angles ?? [];
  // 发射器下拉必须列出全部可用发射器：引擎候选优先，候选缺失时回落到舰船自身状态。
  // 历史 bug：候选存在时只渲染当前那一个 option，玩家因此只能"选"第一个发射器。
  const launcherOptions = selectedTorpedoCandidate?.launchers.length
    ? selectedTorpedoCandidate.launchers.map((candidate) => ({
        id: candidate.launcher_id,
        label: `${candidate.launcher_id} · 可用 ${candidate.loaded} 枚`,
      }))
    : (selectedActionShip?.torpedo_launchers ?? [])
        .filter((launcher) => !launcher.destroyed && launcher.loaded > 0)
        .map((launcher) => ({
          id: launcher.id,
          label: `${launcher.id} · 可用 ${launcher.loaded} 枚${launcher.reload_turns_remaining ? " · 再装填中" : ""}`,
        }));
  const selectedLaunchSide = torpedoLaunchSide || launchSides[0] || "";
  const selectedLaunchAngle = torpedoLaunchAngle || launchAngles[0] || "";
  const selectedSettingIndex = torpedoSettingIndex === ""
    ? selectedTorpedoCandidate?.settings[0]?.index ?? 0
    : Number(torpedoSettingIndex);
  const updateOrderDraft = (change: (batch: JsonRecord) => void, notice: string) => {
    try {
      const batch = JSON.parse(orderText) as JsonRecord;
      change(batch);
      setOrderText(pretty(batch));
      setOrderValidationErrors([]);
      if (view && summary && selectedSeatId) {
        const draft: JsonRecord = {};
        for (const field of orderDraftFields) draft[field] = batch[field] ?? [];
        try {
          localStorage.setItem(
            orderDraftKey(summary.match.match_id, selectedSeatId, view.turn),
            JSON.stringify(draft),
          );
        } catch {
          setError("浏览器无法保存本机炮击预编排草稿；请在炮击阶段重新填写。");
        }
      }
      setMessage(notice);
    } catch {
      setError("当前订单草稿不是合法 JSON，请先在高级 JSON 中修复。");
    }
  };
  const orderDraft = useMemo<JsonRecord | null>(() => {
    try {
      const parsed = JSON.parse(orderText) as unknown;
      return parsed && typeof parsed === "object" ? parsed as JsonRecord : null;
    } catch {
      return null;
    }
  }, [orderText]);
  const draftCommands = orderDraftFields.flatMap((field) => {
    const rows = orderDraft?.[field];
    return Array.isArray(rows)
      ? rows.map((value, index) => ({ field, value, index }))
      : [];
  });
  const judgeShips = useMemo(
    () => (judgeView?.ships as unknown as Ship[] | undefined) ?? [],
    [judgeView],
  );
  const judgeTorpedoCandidates = (judgeView?.torpedo_candidates ?? []) as unknown as PlatformTorpedoCandidate[];
  const judgeTorpedoCandidate = (shipId: unknown) =>
    judgeTorpedoCandidates.find((candidate) => candidate.ship_id === String(shipId ?? ""));
  const selectedOrderSeat = summary?.seats.find((seat) => seat.seat_id === orderSeatId);
  const orderSeatShips = judgeShips.filter((ship) => selectedOrderSeat?.controlled_ship_ids.includes(ship.id));
  const defaultOrderShipId = selectedOrderSeat?.controlled_ship_ids[0] ?? judgeShips[0]?.id ?? "";
  const defaultOrderShip = judgeShips.find((ship) => ship.id === defaultOrderShipId);
  const defaultTargetId = judgeShips.find((ship) => ship.id !== defaultOrderShipId)?.id ?? judgeShips[0]?.id ?? "";
  const judgeShipLabel = (shipId: unknown) => {
    const id = String(shipId ?? "");
    const ship = judgeShips.find((item) => item.id === id);
    return ship ? `${ship.name} · ${ship.id}` : id || "未指定舰船";
  };
  const draftRows = (key: string) => (
    Array.isArray(orderDraft?.[key])
      ? (orderDraft?.[key] as unknown[]).filter(
          (item): item is JsonRecord => Boolean(item && typeof item === "object"),
        )
      : []
  );
  const draftShipName = (shipId: unknown) =>
    ownShips.find((ship) => ship.id === String(shipId ?? ""))?.name
      ?? String(shipId ?? "本舰");
  const draftTargetName = (targetId: unknown) => {
    const target = visibleTargets.find((ship) => ship.id === String(targetId ?? ""));
    return target ? targetLabel(target) : String(targetId ?? "目标待刷新");
  };
  const draftCommandLabels: Record<string, string> = {
    reinforcements: "增援",
    movement: "移动",
    gunnery: "炮击",
    torpedoes: "鱼雷",
    smoke_ships: "烟幕",
    smoke: "烟幕",
    illumination: "照明弹",
    searchlights: "探照灯",
    formation_setup: "编队设置",
    formation_movement: "编队移动",
    formation_speed_decisions: "编队速度",
  };
  const draftCommandDescription = (field: string, value: unknown) => {
    const row = value && typeof value === "object" ? value as JsonRecord : { ship_id: value };
    const ship = draftShipName(row.ship_id);
    switch (field) {
      case "reinforcements": return `${ship} · ${positionLabel(row.entry_hex ?? row.position)}`;
      case "movement": return `${ship} · ${String(row.plan ?? "未设航路")}${row.speed == null ? "" : ` · ${String(row.speed)} MF`}`;
      case "gunnery": return `${ship} → ${draftTargetName(row.primary_target)} · ${Array.isArray(row.mounts) ? row.mounts.length : 0} 个炮位`;
      case "torpedoes": return `${ship} · ${String(row.launcher_id ?? "发射器未指定")} · ${String(row.count ?? 1)} 枚 · MF ${String(row.launch_at_mf ?? "?")}`;
      case "illumination": return `${ship} · ${String(row.mount_id ?? "自动炮位")} → ${positionLabel(row.target_hex)}`;
      case "searchlights": return `${ship} → ${draftTargetName(row.target_id)}`;
      case "smoke": return `${ship} · ${row.deploy === false ? "停止施放" : "施放"}`;
      case "smoke_ships": return ship;
      case "formation_setup":
      case "formation_movement":
      case "formation_speed_decisions": return String(row.name ?? row.formation_id ?? row.ship_id ?? "编队行动");
      default: return ship;
    }
  };
  const draftSummarySections = view ? [
    {
      key: "reinforcements", label: "增援", visible: view.phase === "reinforcement",
      rows: draftRows("reinforcements"),
      describe: (row: JsonRecord) => `${draftShipName(row.ship_id)} · ${positionLabel(row.entry_hex ?? row.position)}`,
    },
    {
      key: "movement", label: "移动", visible: view.phase === "reinforcement" || view.phase === "movement_planning",
      rows: draftRows("movement"),
      describe: (row: JsonRecord) => `${draftShipName(row.ship_id)} · ${String(row.plan ?? "未设航路")}${row.speed == null ? "" : ` · ${String(row.speed)} MF`}`,
    },
    {
      key: "gunnery", label: "炮击", visible: view.phase === "reinforcement" || view.phase === "gunnery",
      rows: draftRows("gunnery"),
      describe: (row: JsonRecord) => {
        const mounts = Array.isArray(row.mounts) ? row.mounts.length : 0;
        return `${draftShipName(row.ship_id)} → ${draftTargetName(row.primary_target)} · ${mounts} 个炮位`;
      },
    },
    {
      key: "torpedoes", label: "鱼雷", visible: view.phase === "reinforcement" || view.phase === "torpedo_planning",
      rows: draftRows("torpedoes"),
      describe: (row: JsonRecord) => `${draftShipName(row.ship_id)} · ${String(row.launcher_id ?? "发射器未指定")} · ${String(row.count ?? 1)} 枚 · MF ${String(row.launch_at_mf ?? "?")}`,
    },
    {
      key: "illumination", label: "照明弹", visible: view.phase === "reinforcement" || view.phase === "gunnery",
      rows: draftRows("illumination"),
      describe: (row: JsonRecord) => `${draftShipName(row.ship_id)} · ${String(row.mount_id ?? "自动炮位")} → ${positionLabel(row.target_hex)}`,
    },
    {
      key: "searchlights", label: "探照灯", visible: view.phase === "reinforcement" || view.phase === "gunnery",
      rows: draftRows("searchlights"),
      describe: (row: JsonRecord) => `${draftShipName(row.ship_id)} → ${draftTargetName(row.target_id)}`,
    },
    {
      key: "smoke", label: "烟幕与其他行动", visible: view.phase === "reinforcement" || view.phase === "gunnery",
      rows: [
        ...draftRows("smoke"),
        ...(Array.isArray(orderDraft?.smoke_ships) ? orderDraft.smoke_ships : [])
          .map((shipId) => ({ ship_id: typeof shipId === "object" && shipId ? (shipId as JsonRecord).ship_id : shipId })),
      ],
      describe: (row: JsonRecord) => draftShipName(row.ship_id),
    },
    {
      key: "formations", label: "编队", visible: view.phase === "formation_setup" || view.phase === "movement_planning",
      rows: ["formation_setup", "formation_movement", "formation_speed_decisions"].flatMap((key) => draftRows(key)),
      describe: (row: JsonRecord) => String(row.name ?? row.formation_id ?? row.ship_id ?? "编队行动"),
    },
  ].filter((section) => section.visible) : [];
  const nextJudgeTorpedoLauncher = defaultOrderShip?.torpedo_launchers.find((launcher) =>
    !launcher.destroyed && launcher.loaded > 0 && !launcher.reload_turns_remaining,
  );
  const usedJudgeIlluminationMounts = new Set(
    draftRows("illumination")
      .filter((row) => row.ship_id === defaultOrderShipId)
      .map((row) => String(row.mount_id ?? "")),
  );
  const nextJudgeIlluminationMount = defaultOrderShip?.gun_mounts.find((mount) =>
    ["primary", "secondary"].includes(mount.kind) && !mount.destroyed
      && !mount.fired_this_phase && !usedJudgeIlluminationMounts.has(mount.id),
  );
  const updateOrderRow = (key: string, index: number, patch: JsonRecord, notice = "已更新可视化订单") => {
    updateOrderDraft((batch) => {
      const rows = Array.isArray(batch[key]) ? [...(batch[key] as unknown[])] : [];
      const row = rows[index];
      if (!row || typeof row !== "object") return;
      rows[index] = { ...(row as JsonRecord), ...patch };
      batch[key] = rows;
    }, notice);
  };
  const removeOrderRow = (key: string, index: number) => {
    updateOrderDraft((batch) => {
      const rows = Array.isArray(batch[key]) ? [...(batch[key] as unknown[])] : [];
      rows.splice(index, 1);
      batch[key] = rows;
    }, "已移除一条订单");
  };
  const appendOrderRow = (key: string, row: JsonRecord, notice = "已添加一条可视化订单") => {
    updateOrderDraft((batch) => {
      const rows = Array.isArray(batch[key]) ? [...(batch[key] as unknown[])] : [];
      rows.push(row);
      batch[key] = rows;
    }, notice);
  };
  // 玩家端拉烟（烟幕）：引擎在每回合推进时把 ship.smoke 清零，所以烟幕只在当回合有效、
  // 需要逐回合声明。施放资格（仅 DD/CL）与阶段合法性一律交给规则核心校验，
  // 界面不复制规则常量（agent.md：引擎是唯一规则权威）。
  const writeSmokeOrder = () => {
    if (!smokeShipId) return;
    const shipName = ownShips.find((ship) => ship.id === smokeShipId)?.name ?? smokeShipId;
    appendOrderRow("smoke", { ship_id: smokeShipId, deploy: smokeDeploy }, `已加入拉烟指令：${shipName}`);
  };
  const updateIlluminationHex = (index: number, label: string, input: HTMLInputElement) => {
    const targetHex = parseMapHexLabel(label);
    if (!targetHex) {
      input.value = positionLabel((draftRows("illumination")[index] as JsonRecord | undefined)?.target_hex);
      setError("照明弹目标格号无效，请使用地图坐标，例如 U40。");
      return;
    }
    updateOrderDraft((batch) => {
      const rows = Array.isArray(batch.illumination) ? [...(batch.illumination as unknown[])] : [];
      const row = rows[index];
      if (!row || typeof row !== "object") return;
      rows[index] = { ...(row as JsonRecord), target_hex: targetHex };
      batch.illumination = rows;
    }, "已更新照明弹目标格");
  };
  const updateTorpedoHex = (index: number, axis: "q" | "r", value: string) => {
    updateOrderDraft((batch) => {
      const rows = Array.isArray(batch.torpedoes) ? [...(batch.torpedoes as unknown[])] : [];
      const row = rows[index];
      if (!row || typeof row !== "object") return;
      const currentHex = (row as JsonRecord).launch_hex;
      const launchHex = currentHex && typeof currentHex === "object" ? { ...(currentHex as JsonRecord) } : {};
      launchHex[axis] = Number(value);
      rows[index] = { ...(row as JsonRecord), launch_hex: launchHex };
      batch.torpedoes = rows;
    }, "已更新鱼雷发射格");
  };
  const updateGunneryMount = (index: number, mountIndex: number, patch: JsonRecord) => {
    updateOrderDraft((batch) => {
      const rows = Array.isArray(batch.gunnery) ? [...(batch.gunnery as unknown[])] : [];
      const row = rows[index];
      if (!row || typeof row !== "object") return;
      const mounts = Array.isArray((row as JsonRecord).mounts) ? [...((row as JsonRecord).mounts as unknown[])] : [];
      const mount = mounts[mountIndex];
      if (!mount || typeof mount !== "object") return;
      mounts[mountIndex] = { ...(mount as JsonRecord), ...patch };
      rows[index] = { ...(row as JsonRecord), mounts };
      batch.gunnery = rows;
    }, "已更新炮位订单");
  };
  const appendGunneryMount = (index: number, shipId: string) => {
    const ship = judgeShips.find((item) => item.id === shipId);
    if (!ship) return;
    const row = draftRows("gunnery")[index];
    const usedMounts = new Set(
      (Array.isArray(row?.mounts) ? row.mounts as JsonRecord[] : []).map((mount) => String(mount.mount_id ?? "")),
    );
    const mount = ship.gun_mounts.find((item) => !usedMounts.has(item.id));
    if (!mount) return;
    updateOrderDraft((batch) => {
      const rows = Array.isArray(batch.gunnery) ? [...(batch.gunnery as unknown[])] : [];
      const current = rows[index];
      if (!current || typeof current !== "object") return;
      const currentOrder = current as JsonRecord;
      const mounts = Array.isArray(currentOrder.mounts) ? [...currentOrder.mounts as unknown[]] : [];
      mounts.push({ mount_id: mount.id, target_id: String(currentOrder.primary_target ?? "") || null });
      rows[index] = { ...currentOrder, mounts };
      batch.gunnery = rows;
    }, `已加入 ${mount.id} 炮位`);
  };
  const removeGunneryMount = (index: number, mountIndex: number) => {
    updateOrderDraft((batch) => {
      const rows = Array.isArray(batch.gunnery) ? [...(batch.gunnery as unknown[])] : [];
      const row = rows[index];
      if (!row || typeof row !== "object") return;
      const mounts = Array.isArray((row as JsonRecord).mounts) ? [...((row as JsonRecord).mounts as unknown[])] : [];
      mounts.splice(mountIndex, 1);
      rows[index] = { ...(row as JsonRecord), mounts };
      batch.gunnery = rows;
    }, "已移除炮位订单");
  };
  const updateGunneryMountTarget = (index: number, mountIndex: number, targetId: string) => {
    updateOrderDraft((batch) => {
      const rows = Array.isArray(batch.gunnery) ? [...(batch.gunnery as unknown[])] : [];
      const row = rows[index];
      if (!row || typeof row !== "object") return;
      const mounts = Array.isArray((row as JsonRecord).mounts) ? [...((row as JsonRecord).mounts as unknown[])] : [];
      const mount = mounts[mountIndex];
      if (!mount || typeof mount !== "object") return;
      mounts[mountIndex] = { ...(mount as JsonRecord), target_id: targetId };
      rows[index] = { ...(row as JsonRecord), mounts };
      batch.gunnery = rows;
    }, "已更新炮位目标");
  };
  const replaceShipOrder = (
    key: string,
    shipId: string,
    order: JsonRecord,
    notice: string,
  ) => {
    updateOrderDraft((batch) => {
      const rows = Array.isArray(batch[key]) ? [...(batch[key] as unknown[])] : [];
      const index = rows.findIndex(
        (row) => row && typeof row === "object" && (row as JsonRecord).ship_id === shipId,
      );
      if (index >= 0) rows[index] = order;
      else rows.push(order);
      batch[key] = rows;
    }, notice);
  };
  const writeMovementOrder = () => {
    const ship = selectedActionShip;
    if (!ship) return;
    const speed = movementSpeed.trim() === "" ? null : Number(movementSpeed);
    replaceShipOrder(
      "movement",
      ship.id,
      { ship_id: ship.id, plan: movementPlan.trim() || "0", speed: Number.isFinite(speed) ? speed : null },
      `已写入 ${ship.name} 的移动计划`,
    );
  };
  const writeGunneryOrder = () => {
    const ship = selectedActionShip;
    const target = selectedActionTarget;
    if (!ship || !target) return;
    const mountIds = fireableMounts.map((mount) => mount.id);
    replaceShipOrder(
      "gunnery",
      ship.id,
      {
        ship_id: ship.id,
        primary_target: target.id,
        secondary_target: null,
        searchlight_target: null,
        mounts: mountIds.map((mountId) => ({ mount_id: mountId, target_id: target.id })),
      },
      `已写入 ${ship.name} → ${targetLabel(target)} 的炮击草稿`,
    );
  };
  const writeIlluminationOrder = () => {
    const ship = selectedActionShip;
    const targetHex = parseMapHexLabel(illuminationHexLabel);
    if (!ship || !targetHex || !illuminationMountChoice) return;
    const mountId = illuminationMountChoice.id;
    updateOrderDraft((batch) => {
      const rows = Array.isArray(batch.illumination)
        ? [...(batch.illumination as unknown[])]
        : [];
      const order = { ship_id: ship.id, mount_id: mountId, target_hex: targetHex };
      const index = rows.findIndex(
        (row) => row && typeof row === "object"
          && (row as JsonRecord).ship_id === ship.id
          && (row as JsonRecord).mount_id === mountId,
      );
      if (index >= 0) rows[index] = order;
      else rows.push(order);
      batch.illumination = rows;
    }, `已加入 ${ship.name} 的第 ${illuminationDrafts.length + 1} 发照明弹`);
  };
  const removeIlluminationOrder = (index: number) => {
    updateOrderDraft((batch) => {
      const rows = Array.isArray(batch.illumination) ? [...(batch.illumination as unknown[])] : [];
      rows.splice(index, 1);
      batch.illumination = rows;
    }, "已移除一条照明弹订单");
  };
  const writeSearchlightOrder = () => {
    const ship = selectedActionShip;
    const target = visibleTargets.find((item) => item.id === searchlightTargetId) ?? selectedActionTarget;
    if (!ship || !target) return;
    replaceShipOrder(
      "searchlights",
      ship.id,
      { ship_id: ship.id, target_id: target.id, active: true },
      `已写入 ${ship.name} → ${targetLabel(target)} 的探照灯订单`,
    );
  };
  const writeTorpedoOrder = () => {
    const ship = selectedActionShip;
    const launcher = selectedLauncher;
    const position = selectedLaunchPosition;
    // 只接受"由移动草稿预览算出的合法 MF"：引擎校验要求 launch_hex 必须等于该 MF 处的航路位置
    // （engine.py:1797-1802）。若允许手填 MF 而不给发射格，就会写出 launch_hex 为空的行，
    // 提交时被引擎以"launch_hex does not match its MF position"拒绝——玩家从未见过发射格控件却报格错。
    if (!ship || !launcher || !selectedLauncherCandidate || !position || !selectedLaunchSide || !selectedLaunchAngle) return;
    const order = {
      ship_id: ship.id,
      count: Math.max(1, Math.min(selectedLauncherRemaining, Number(torpedoCount) || 1)),
      speed: "fast",
      launcher_id: launcher.id,
      launch_at_mf: position.mf,
      launch_hex: position.hex,
      bearing: position.heading,
      launch_side: selectedLaunchSide,
      launch_angle: selectedLaunchAngle,
      setting_index: selectedSettingIndex,
    };
    updateOrderDraft((batch) => {
      const rows = Array.isArray(batch.torpedoes) ? [...(batch.torpedoes as unknown[])] : [];
      rows.push(order);
      batch.torpedoes = rows;
    }, `已追加 ${ship.name} ${launcher.id} 鱼雷发射订单，航迹重合对象由引擎逐舰裁决`);
  };

  const submittedSeatCount = summary?.match.stage === "awaiting_publication"
    ? judgeView?.submitted_seat_count ?? 0
    : review?.orders.length ?? judgeView?.submitted_seat_count ?? 0;
  const requiredSeatCount = summary?.match.required_seat_ids.length ?? 0;
  const reviewReady = summary?.match.stage === "ready_for_review";
  const awaitingPublication = summary?.match.stage === "awaiting_publication";
  const canReject = summary?.match.stage === "collecting" || reviewReady;

  if (scenarioWorkshopOpen)
    return (
      <CustomScenarioEditor
        onClose={() => setScenarioWorkshopOpen(false)}
        onLaunch={(id) => {
          setScenarioId(id);
          setScenarioWorkshopOpen(false);
          setMessage(`想定已保存：${id}；可在裁判台用该 ID 创建对局`);
        }}
        error={error}
      />
    );

  if (!session)
    return (
      <main className="platform-shell">
        <header className="platform-header">
          <div>
            <p className="eyebrow">MULTI-PLAYER WAR ROOM</p>
            <h1>铁底湾多人网推</h1>
            <p>玩家独立席位 · 裁判人工推进 · 事件可审计</p>
          </div>
          <button className="quiet" onClick={onClose}>
            返回单局大厅
          </button>
        </header>
        <section className="platform-login">
          <div className="platform-login-copy">
            <span className="platform-kicker">席位入口</span>
            <h2>登录玩家或裁判席位</h2>
            <p>账号和密码由裁判单独下发；裁判账号会自动进入裁判审理台，玩家账号只看到绑定舰船及其视野。</p>
          </div>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              login();
            }}
          >
            <label>
              账号
              <input
                value={username}
                autoComplete="username"
                onChange={(event) => setUsername(event.target.value)}
              />
            </label>
            <label>
              密码
              <input
                type="password"
                value={password}
                autoComplete="current-password"
                onChange={(event) => setPassword(event.target.value)}
              />
            </label>
            <button
              className="primary"
              disabled={busy || !username || !password}
            >
              {busy ? "登录中…" : "登录"}
            </button>
          </form>
        </section>
        {error && <pre className="platform-error">{error}</pre>}
      </main>
    );

  const isJudge = session.is_referee;
  return (
    <main className="platform-shell">
      <header className="platform-header">
        <div>
          <p className="eyebrow">MULTI-PLAYER WAR ROOM</p>
          <h1>{isJudge ? "裁判审理台" : "玩家指挥席"}</h1>
          <p>
            {session.username} · {isJudge ? "裁判权限" : "独立舰船席位"}
          </p>
        </div>
        <div className="platform-header-actions">
          <span className={`platform-status ${summary?.match.stage ?? "idle"}`}>
            {summary?.match.stage ?? "未载入对局"}
          </span>
          <button className="quiet" onClick={signOut}>
            退出登录
          </button>
          <button className="quiet" onClick={onClose}>
            返回单局大厅
          </button>
        </div>
      </header>
      <section className="platform-toolbar">
        <label>
          对局 ID
          <input
            value={matchId}
            placeholder="粘贴裁判下发的 match_id"
            onChange={(event) => setMatchId(event.target.value)}
          />
        </label>
        <button
          className="primary"
          disabled={busy || !matchId.trim()}
          onClick={loadSummary}
        >
          载入对局
        </button>
        {message && <span className="platform-message">{message}</span>}
      </section>
      {isJudge ? (
        <div className="platform-grid judge-grid">
          <PlatformMap
            view={judgeView}
            fullMap
            onSelect={(ship) => setMessage(`已选中 ${ship.name}`)}
          />
          <section className="platform-panel judge-fleet-state">
            <div className="panel-heading">
              <div>
                <span className="platform-kicker">AUTHORITATIVE FLEET STATE</span>
                <h2>全舰状态</h2>
              </div>
              <span className="match-meta">{judgeView?.referee_ship_states?.length ?? 0} 艘</span>
            </div>
            {judgeView?.referee_ship_states?.length ? (
              <div className="judge-fleet-list">
                {judgeView.referee_ship_states.map((ship) => <RefereeShipCard key={String(ship.id)} ship={ship} />)}
              </div>
            ) : (
              <p className="empty-plan">载入对局后显示全体舰船的权威状态。</p>
            )}
          </section>
          <section className="platform-panel">
            <div className="panel-heading">
              <div>
                <span className="platform-kicker">MATCH CONTROL</span>
                <h2>创建对局与席位</h2>
              </div>
              {summary && <code>{summary.match.match_id}</code>}
            </div>
            <div className="inline-fields">
              <label>
                想定
                <input
                  value={scenarioId}
                  onChange={(event) => setScenarioId(event.target.value)}
                />
              </label>
              <button className="quiet" onClick={() => setScenarioWorkshopOpen(true)}>
                打开剧本工坊
              </button>
              <label>
                Seed
                <input
                  type="number"
                  value={seed}
                  onChange={(event) => setSeed(event.target.value)}
                />
              </label>
              <button onClick={createMatch}>创建对局</button>
            </div>
            <div className="panel-divider" />
            {summary && (
              <>
                <div className="seat-create">
                  <h3>想定席位与玩家账密</h3>
                  <p className="field-hint">
                    这些席位在制作想定时已经按舰船自动建立。新建想定会自动生成并绑定账号密码；历史测试席位若未保存明文密码，会明确标注为不可恢复。
                  </p>
                  <p className="seat-credential-help">
                    看到“裁判代理”表示该舰当前由裁判代控，不会生成玩家登录账号。要把舰船交给玩家，请先打开剧本工坊，在“每舰席位与玩家账密”中为每艘船填写账号和密码，保存想定后再创建新对局；新对局创建完成后，这里会显示可直接分发的完整账密。
                  </p>
                </div>
                <div className="seat-list">
                  {summary.seats.map((seat) => (
                    <button
                      key={seat.seat_id}
                      className={orderSeatId === seat.seat_id ? "selected" : ""}
                      onClick={() => selectReviewSeat(seat.seat_id)}
                    >
                      <b>
                        {seat.side === "axis" ? "轴心" : "同盟"} ·{" "}
                        {seat.controller_type}
                      </b>
                      <small className="credential-line">
                        账号：{seat.username ?? (seat.user_id ? "已绑定玩家" : "裁判代理无需登录")}
                      </small>
                      <small className="credential-line">
                        密码：{seat.credential_password ?? (seat.controller_type === "referee_proxy" ? "由裁判代理控制" : "历史席位未保存明文密码")}
                      </small>
                      <code>{seat.controlled_ship_ids.join(", ")}</code>
                    </button>
                  ))}
                </div>
                <section className="judge-downloads" aria-label="裁判本地存档与账密">
                  <div className="judge-download-heading">
                    <h3>裁判下载</h3>
                    <button className="quiet" onClick={downloadSeatCredentials}>
                      下载席位账密 TXT
                    </button>
                  </div>
                  <div className="turn-archive-heading">
                    <div>
                      <b>逐回合存档</b>
                      <span>新建对局与进入新回合时自动下载；列表可补下每回合最新快照。</span>
                    </div>
                    <button className="quiet" onClick={refreshTurnArchives} disabled={busy}>
                      刷新
                    </button>
                  </div>
                  {turnArchives.length ? (
                    <div className="turn-archive-list">
                      {turnArchives.map((archive) => (
                        <div key={archive.turn}>
                          <span>第 {archive.turn} 回合 · {archive.phase}</span>
                          <button onClick={() => downloadTurnArchive(archive.turn)} disabled={busy}>
                            下载存档
                          </button>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="field-hint">载入对局后显示已保存的回合存档。</p>
                  )}
                </section>
              </>
            )}
          </section>
          <section className="platform-panel review-panel">
            <div className="panel-heading">
              <div>
                <span className="platform-kicker">REFEREE REVIEW</span>
                <h2>订单审理</h2>
              </div>
              <button onClick={refreshReview} disabled={!summary}>
                刷新
              </button>
            </div>
            {summary && (
              <div className="match-meta">
                <b>{summary.match.phase}</b>
                <span>版本 {summary.match.version}</span>
                <span>{summary.match.required_seat_ids.length} 个席位</span>
              </div>
            )}
            {summary && (
              <div className={`review-readiness${reviewReady || awaitingPublication ? " ready" : ""}`}>
                <b>{awaitingPublication ? "本回合已裁决，等待裁判发布" : reviewReady ? "可以审理并批准推进" : "等待全部席位提交"}</b>
                <span>
                  已提交 {submittedSeatCount}/{requiredSeatCount} 个席位。
                  {awaitingPublication
                    ? `第 ${judgeView?.adjudication_turn ?? judgeView?.turn ?? "—"} 回合结果仅裁判可见；完成重骰与状态修正后，点击“发布本回合结果”发送给玩家。`
                    : reviewReady
                    ? "批准时仍会由规则引擎执行最终合法性校验。"
                    : "批准推进会在所有必需席位提交后启用；当前不是因为订单非法而禁用。"}
                </span>
              </div>
            )}
            {summary && error && (
              <div className="review-error" role="alert">
                <b>推进未完成，对局没有变化</b>
                <span>{error.replace(/^Error:\s*/u, "")}</span>
                <p>订单仍保留。请选中相关席位修正或补齐订单，再次批准推进。</p>
              </div>
            )}
            {summary && (
              <div className="review-seat-picker">
                <h3>选择要审理的席位</h3>
                {summary.seats.map((seat) => {
                  const submitted = Boolean(review?.orders.some((item) => item.seat_id === seat.seat_id));
                  return (
                    <button
                      key={seat.seat_id}
                      className={orderSeatId === seat.seat_id ? "selected" : ""}
                      onClick={() => selectReviewSeat(seat.seat_id)}
                    >
                      <b>{seat.username ?? seat.seat_id.slice(0, 8)}</b>
                      <span>{seat.side === "axis" ? "轴心" : "同盟"} · {seat.controlled_ship_ids.join(", ")}</span>
                      <small>{submitted ? "已提交，点击载入订单" : "未提交，点击查看空表"}</small>
                    </button>
                  );
                })}
              </div>
            )}
            {review?.orders.length ? (
              <div className="review-orders">
                {review.orders.map((order) => (
                  <button
                    key={order.seat_id}
                    className={orderSeatId === order.seat_id ? "selected" : ""}
                    onClick={() => selectReviewSeat(order.seat_id)}
                  >
                    <b>{order.seat_id.slice(0, 8)}</b>
                    <span>{order.validation_errors?.length ? `⚠ ${order.validation_errors.length} 项报错` : order.request_id}</span>
                  </button>
                ))}
              </div>
            ) : (
              <p className="empty-plan">载入对局后刷新订单审理。</p>
            )}
            {orderDraft && orderSeatId && (
              <section className="referee-order-editor">
                <div className="referee-order-editor-heading">
                  <div>
                    <span className="platform-kicker">VISUAL ORDER EDITOR</span>
                    <h3>可视化订单编辑</h3>
                  </div>
                  <span>{review?.orders.find((item) => item.seat_id === orderSeatId)?.scope === "turn" ? "整回合订单" : "阶段订单"} · {String(orderDraft.side ?? "—")}</span>
                </div>
                {Boolean(review?.orders.find((item) => item.seat_id === orderSeatId)?.validation_errors?.length) && (
                  <div className="review-error order-validation-note" role="alert">
                    <b>该订单提交时忽略了校验报错</b>
                    {(review?.orders.find((item) => item.seat_id === orderSeatId)?.validation_errors ?? []).map((item, index) => (
                      <span key={`order-validation-${index}`}>{item}</span>
                    ))}
                    <p>请核对或覆盖订单；批准时规则引擎仍会执行最终合法性校验。</p>
                  </div>
                )}
                <div className="referee-add-actions">
                  <span>添加行动</span>
                  <button type="button" className="quiet" disabled={!defaultOrderShipId} onClick={() => appendOrderRow("movement", { ship_id: defaultOrderShipId, plan: "0", speed: null })}>＋移动</button>
                  <button type="button" className="quiet" disabled={!defaultOrderShipId} onClick={() => appendOrderRow("gunnery", { ship_id: defaultOrderShipId, primary_target: defaultTargetId || null, secondary_target: null, searchlight_target: null, mounts: defaultOrderShip?.gun_mounts[0] ? [{ mount_id: defaultOrderShip.gun_mounts[0].id, target_id: defaultTargetId || null }] : [] })}>＋炮击</button>
                  <button type="button" className="quiet" disabled={!defaultOrderShipId || !nextJudgeTorpedoLauncher} onClick={() => appendOrderRow("torpedoes", { ship_id: defaultOrderShipId, count: 1, speed: "fast", launcher_id: nextJudgeTorpedoLauncher?.id ?? null, launch_at_mf: 1, launch_hex: defaultOrderShip?.position ? { ...defaultOrderShip.position } : null, bearing: null, launch_side: nextJudgeTorpedoLauncher?.arcs.includes("port") ? "port" : "starboard", launch_angle: "A", setting_index: 0 })}>＋鱼雷发射</button>
                  <button type="button" className="quiet" disabled={!defaultOrderShipId || !nextJudgeIlluminationMount} onClick={() => appendOrderRow("illumination", { ship_id: defaultOrderShipId, mount_id: nextJudgeIlluminationMount?.id ?? null, target_hex: defaultOrderShip?.position ? { ...defaultOrderShip.position } : { q: 0, r: 0 } }, "已加入一发照明弹；可继续添加其他发射")}>＋再加一发照明弹</button>
                  <button type="button" className="quiet" disabled={!defaultOrderShipId} onClick={() => appendOrderRow("searchlights", { ship_id: defaultOrderShipId, target_id: defaultTargetId || null, active: true })}>＋探照灯</button>
                  <button type="button" className="quiet" disabled={!defaultOrderShipId} onClick={() => appendOrderRow("smoke", { ship_id: defaultOrderShipId, deploy: true })}>＋烟幕</button>
                  <button
                    type="button"
                    className="quick-override-button"
                    disabled={!selectedOrderSeat || !judgeView || !["reinforcement", "movement_planning", "gunnery"].includes(summary?.match.phase ?? "")}
                    onClick={quickOverrideOrder}
                    title="直接覆盖所选席位订单：最高合法速度直航；若上次炮击目标仍可合法射击则继续炮击，否则不射击；清除其他行动。"
                  >快速覆盖</button>
                </div>
                {draftRows("movement").length > 0 && (
                  <section className="referee-order-group">
                    <h4>移动计划</h4>
                    {draftRows("movement").map((row, index) => (
                      <div className="referee-order-row" key={`movement-${index}`}>
                        <label>舰船<select value={String(row.ship_id ?? "")} onChange={(event) => updateOrderRow("movement", index, { ship_id: event.target.value })}>
                          {orderSeatShips.map((ship) => <option key={ship.id} value={ship.id}>{ship.name} · {ship.id}</option>)}
                        </select></label>
                        <label>计划<input value={String(row.plan ?? "0")} onChange={(event) => updateOrderRow("movement", index, { plan: event.target.value })} /></label>
                        <label>速度<input type="number" value={row.speed == null ? "" : String(row.speed)} onChange={(event) => updateOrderRow("movement", index, { speed: event.target.value === "" ? null : Number(event.target.value) })} /></label>
                        <button type="button" className="quiet" onClick={() => removeOrderRow("movement", index)}>移除</button>
                      </div>
                    ))}
                  </section>
                )}
                {draftRows("gunnery").length > 0 && (
                  <section className="referee-order-group">
                    <h4>炮击订单</h4>
                    {draftRows("gunnery").map((row, index) => {
                      const mounts = Array.isArray(row.mounts) ? row.mounts.filter((item): item is JsonRecord => Boolean(item && typeof item === "object")) : [];
                      const ship = judgeShips.find((item) => item.id === String(row.ship_id ?? ""));
                      return (
                        <div className="referee-order-row referee-gunnery-row" key={`gunnery-${index}`}>
                          <label>舰船<select value={String(row.ship_id ?? "")} onChange={(event) => {
                            const nextShip = judgeShips.find((item) => item.id === event.target.value);
                            updateOrderRow("gunnery", index, {
                              ship_id: event.target.value,
                              mounts: nextShip?.gun_mounts[0] ? [{ mount_id: nextShip.gun_mounts[0].id, target_id: String(row.primary_target ?? "") || null }] : [],
                            });
                          }}>
                            {orderSeatShips.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}
                          </select></label>
                          <label>主目标<select value={String(row.primary_target ?? "")} onChange={(event) => updateOrderRow("gunnery", index, { primary_target: event.target.value })}>
                            <option value="">未指定</option>
                            {judgeShips.map((ship) => <option key={ship.id} value={ship.id}>{ship.name} · {ship.id}</option>)}
                          </select></label>
                          <label>副目标<select value={String(row.secondary_target ?? "")} onChange={(event) => updateOrderRow("gunnery", index, { secondary_target: event.target.value || null })}>
                            <option value="">未指定</option>
                            {judgeShips.map((target) => <option key={target.id} value={target.id}>{target.name} · {target.id}</option>)}
                          </select></label>
                          <div className="referee-mount-targets">
                            <div className="referee-mount-heading">
                              <span>炮位与各自目标</span>
                              <button type="button" className="quiet" disabled={!ship || mounts.length >= ship.gun_mounts.length} onClick={() => appendGunneryMount(index, String(row.ship_id ?? ""))}>＋炮位</button>
                            </div>
                            {mounts.length ? mounts.map((mount, mountIndex) => (
                              <div className="referee-mount-row" key={`${String(mount.mount_id)}-${mountIndex}`}>
                                <label>炮位<select value={String(mount.mount_id ?? "")} onChange={(event) => updateGunneryMount(index, mountIndex, { mount_id: event.target.value })}>
                                  <option value="">未指定</option>
                                  {ship?.gun_mounts.map((gun) => <option key={gun.id} value={gun.id}>{gun.id} · GF {gun.firepower} {gun.destroyed ? "· 损毁" : ""}</option>)}
                                </select></label>
                                <label>目标<select value={String(mount.target_id ?? "")} onChange={(event) => updateGunneryMountTarget(index, mountIndex, event.target.value)}>
                                  <option value="">未指定</option>
                                  {judgeShips.map((target) => <option key={target.id} value={target.id}>{target.name} · {target.id}</option>)}
                                </select>
                                </label>
                                <button type="button" className="quiet" onClick={() => removeGunneryMount(index, mountIndex)}>移除炮位</button>
                              </div>
                            )) : <span className="muted">无炮位订单</span>}
                          </div>
                          <button type="button" className="quiet" onClick={() => removeOrderRow("gunnery", index)}>移除</button>
                        </div>
                      );
                    })}
                  </section>
                )}
                {draftRows("torpedoes").length > 0 && (
                  <section className="referee-order-group">
                    <h4>鱼雷订单</h4>
                    {draftRows("torpedoes").map((row, index) => {
                      const ship = judgeShips.find((item) => item.id === String(row.ship_id ?? ""));
                      const candidate = judgeTorpedoCandidate(row.ship_id);
                      const launcher = ship?.torpedo_launchers.find((item) => item.id === String(row.launcher_id ?? ""));
                      const otherLauncherCount = draftRows("torpedoes")
                        .filter((item, itemIndex) => itemIndex !== index
                          && item.ship_id === row.ship_id && item.launcher_id === row.launcher_id)
                        .reduce((total, item) => total + Number(item.count ?? 0), 0);
                      const launcherRemaining = Math.max(0, (launcher?.loaded ?? 0) - otherLauncherCount);
                      const launchPositions = candidate?.launch_positions ?? [];
                      const selectedPosition = launchPositions.find((item) => item.mf === Number(row.launch_at_mf));
                      const settings = candidate?.settings ?? [];
                      const angleOptions = candidate?.launchers.find((item) => item.launcher_id === launcher?.id)?.angles
                        ?? candidate?.launchers[0]?.angles
                        ?? ["A", "B", "X", "Y"];
                      return (
                        <div className="referee-order-row referee-torpedo-row" key={`torpedoes-${index}`}>
                          <label>舰船<select value={String(row.ship_id ?? "")} onChange={(event) => {
                            const nextShip = judgeShips.find((item) => item.id === event.target.value);
                            const nextLauncher = nextShip?.torpedo_launchers.find((item) => !item.destroyed && item.loaded > 0);
                            updateOrderRow("torpedoes", index, {
                              ship_id: event.target.value,
                              launcher_id: nextLauncher?.id ?? null,
                              count: nextLauncher ? Math.min(Number(row.count ?? 1), nextLauncher.loaded) : 1,
                              launch_hex: nextShip?.position ? { ...nextShip.position } : null,
                            });
                          }}>
                            {orderSeatShips.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}
                          </select></label>
                          <label>发射器<select value={String(row.launcher_id ?? "")} onChange={(event) => {
                            const nextLauncher = ship?.torpedo_launchers.find((item) => item.id === event.target.value);
                            updateOrderRow("torpedoes", index, {
                              launcher_id: event.target.value || null,
                              count: nextLauncher ? Math.min(Number(row.count ?? 1), Math.max(1, nextLauncher.loaded)) : 1,
                              launch_side: nextLauncher?.arcs.includes(String(row.launch_side ?? "")) ? row.launch_side : nextLauncher?.arcs[0] ?? null,
                            });
                          }}>
                            <option value="">请选择</option>
                            {ship?.torpedo_launchers.map((item) => <option key={item.id} value={item.id} disabled={item.destroyed || item.loaded <= 0}>{item.id} · 装载 {item.loaded}/{item.torpedoes}{item.destroyed ? " · 损毁" : item.reload_turns_remaining ? " · 再装填中" : ""}</option>)}
                          </select></label>
                          <label>枚数<input type="number" min="1" max={Math.max(1, launcherRemaining)} value={String(row.count ?? 1)} onChange={(event) => updateOrderRow("torpedoes", index, { count: Math.min(launcherRemaining, Math.max(1, Number(event.target.value))) })} /></label>
                          <label>发射 MF{launchPositions.length ? (
                            <select value={String(row.launch_at_mf ?? "")} onChange={(event) => {
                              const position = launchPositions.find((item) => item.mf === Number(event.target.value));
                              updateOrderRow("torpedoes", index, {
                                launch_at_mf: Number(event.target.value),
                                launch_hex: position?.hex ?? row.launch_hex,
                                bearing: position?.heading ?? row.bearing,
                              });
                            }}>
                              {launchPositions.map((item) => <option key={item.mf} value={item.mf}>MF {item.mf} · {hexLabel(item.hex)} · 航向 {item.heading}</option>)}
                            </select>
                          ) : <input type="number" min="0" max="8" value={String(row.launch_at_mf ?? 0)} onChange={(event) => updateOrderRow("torpedoes", index, { launch_at_mf: Number(event.target.value) })} />}</label>
                          <label>舷侧<select value={String(row.launch_side ?? "")} onChange={(event) => updateOrderRow("torpedoes", index, { launch_side: event.target.value || null })}>
                            <option value="">未指定</option>
                            {(launcher?.arcs ?? []).filter((arc) => arc === "port" || arc === "starboard").map((arc) => <option key={arc} value={arc}>{arc === "port" ? "左舷" : "右舷"}</option>)}
                          </select></label>
                          <label>发射角<select value={String(row.launch_angle ?? "")} onChange={(event) => updateOrderRow("torpedoes", index, { launch_angle: event.target.value || null })}>
                            <option value="">未指定</option>
                            {angleOptions.map((angle) => <option key={angle} value={angle}>{angle}</option>)}
                          </select></label>
                          <label>速度设定<select value={String(row.setting_index ?? 0)} onChange={(event) => updateOrderRow("torpedoes", index, { setting_index: Number(event.target.value) })}>
                            {settings.map((item) => <option key={item.index} value={item.index}>{item.index + 1} · {item.speed.join("-")} MF · 射程 {item.range}</option>)}
                            {!settings.length && <option value={String(row.setting_index ?? 0)}>设定 {Number(row.setting_index ?? 0) + 1}</option>}
                          </select></label>
                          <div className="referee-launch-hex">
                            <span>发射格</span>
                            <label>q<input type="number" value={row.launch_hex && typeof row.launch_hex === "object" ? String((row.launch_hex as JsonRecord).q ?? "") : ""} onChange={(event) => updateTorpedoHex(index, "q", event.target.value)} /></label>
                            <label>r<input type="number" value={row.launch_hex && typeof row.launch_hex === "object" ? String((row.launch_hex as JsonRecord).r ?? "") : ""} onChange={(event) => updateTorpedoHex(index, "r", event.target.value)} /></label>
                          </div>
                          <button type="button" className="quiet" onClick={() => removeOrderRow("torpedoes", index)}>移除</button>
                        </div>
                      );
                    })}
                  </section>
                )}
                {draftRows("illumination").length > 0 && (
                  <section className="referee-order-group">
                    <h4>照明弹订单</h4>
                    {draftRows("illumination").map((row, index) => {
                      const targetHex = row.target_hex && typeof row.target_hex === "object" ? row.target_hex as JsonRecord : {};
                      const ship = judgeShips.find((item) => item.id === String(row.ship_id ?? ""));
                      return (
                        <div className="referee-order-row" key={`illumination-${index}`}>
                          <label>舰船<select value={String(row.ship_id ?? "")} onChange={(event) => {
                            const nextShip = judgeShips.find((item) => item.id === event.target.value);
                            updateOrderRow("illumination", index, {
                              ship_id: event.target.value,
                              mount_id: nextShip?.gun_mounts.find((item) => ["primary", "secondary"].includes(item.kind) && !item.destroyed)?.id ?? null,
                            });
                          }}>
                            {orderSeatShips.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}
                          </select></label>
                          <label>发射炮位<select value={String(row.mount_id ?? "")} onChange={(event) => updateOrderRow("illumination", index, { mount_id: event.target.value || null })}>
                            <option value="">未指定</option>
                            {ship?.gun_mounts.filter((item) => ["primary", "secondary"].includes(item.kind)).map((item) => <option key={item.id} value={item.id}>{item.id} · GF {item.firepower}{item.destroyed ? " · 损毁" : ""}</option>)}
                          </select></label>
                          <label>目标格号<input defaultValue={positionLabel(targetHex)} placeholder="例如 U40" onBlur={(event) => updateIlluminationHex(index, event.currentTarget.value, event.currentTarget)} /></label>
                          <small className="field-hint">按海图字母+数字输入；保存时自动换算为引擎坐标。</small>
                          <button type="button" className="quiet" onClick={() => removeOrderRow("illumination", index)}>移除本发</button>
                        </div>
                      );
                    })}
                  </section>
                )}
                {draftRows("searchlights").length > 0 && (
                  <section className="referee-order-group">
                    <h4>探照灯订单</h4>
                    {draftRows("searchlights").map((row, index) => (
                      <div className="referee-order-row" key={`searchlights-${index}`}>
                        <label>舰船<select value={String(row.ship_id ?? "")} onChange={(event) => updateOrderRow("searchlights", index, { ship_id: event.target.value })}>
                          {orderSeatShips.map((ship) => <option key={ship.id} value={ship.id}>{ship.name} · {ship.id}</option>)}
                        </select></label>
                        <label>目标<select value={String(row.target_id ?? "")} onChange={(event) => updateOrderRow("searchlights", index, { target_id: event.target.value || null })}>
                          <option value="">未指定</option>
                          {judgeShips.map((ship) => <option key={ship.id} value={ship.id}>{ship.name} · {ship.id}</option>)}
                        </select></label>
                        <label className="check-label"><input type="checkbox" checked={row.active !== false} onChange={(event) => updateOrderRow("searchlights", index, { active: event.target.checked })} />启用</label>
                        <button type="button" className="quiet" onClick={() => removeOrderRow("searchlights", index)}>移除</button>
                      </div>
                    ))}
                  </section>
                )}
                {draftRows("smoke").length > 0 && (
                  <section className="referee-order-group">
                    <h4>烟幕订单</h4>
                    {draftRows("smoke").map((row, index) => (
                      <div className="referee-order-row" key={`smoke-${index}`}>
                        <label>舰船<select value={String(row.ship_id ?? "")} onChange={(event) => updateOrderRow("smoke", index, { ship_id: event.target.value })}>
                          {orderSeatShips.map((ship) => <option key={ship.id} value={ship.id}>{ship.name} · {ship.id}</option>)}
                        </select></label>
                        <label className="check-label"><input type="checkbox" checked={row.deploy !== false} onChange={(event) => updateOrderRow("smoke", index, { deploy: event.target.checked })} />施放烟幕</label>
                        <button type="button" className="quiet" onClick={() => removeOrderRow("smoke", index)}>移除</button>
                      </div>
                    ))}
                  </section>
                )}
                {!draftRows("movement").length && !draftRows("gunnery").length && !draftRows("torpedoes").length && !draftRows("illumination").length && !draftRows("searchlights").length && !draftRows("smoke").length && (
                  <p className="empty-plan">当前订单没有可视化行动项，可在高级 JSON 中查看确认字段。</p>
                )}
              </section>
            )}
            <details className="advanced-json" open={false}>
              <summary>高级 JSON · 精确编辑当前席位订单</summary>
              <textarea
                aria-label="当前席位订单 JSON"
                value={orderText}
                onChange={(event) => setOrderText(event.target.value)}
              />
            </details>
            <label className="wide-label">
              裁判备注
              <input
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder="修订原因、驳回原因或状态修正原因"
              />
            </label>
            <div className="action-row">
              <button onClick={judgeOrder} disabled={!summary || !orderSeatId || awaitingPublication} title="可替未提交或已提交席位直接写入裁判订单">
                覆盖/代提交该席位
              </button>
              <button
                className="primary"
                onClick={approve}
                disabled={!summary || !reviewReady}
                title={reviewReady ? "提交给规则引擎审理并推进阶段" : "等待所有必需席位提交订单"}
              >
                批准推进
              </button>
              <button
                className="primary"
                onClick={publish}
                disabled={!summary || !awaitingPublication || busy}
                title={awaitingPublication ? "发布当前已审定的回合状态，玩家随后自动收到" : "先批准推进并完成裁判审理"}
              >
                发布本回合结果
              </button>
              <button
                className="danger"
                onClick={reject}
                disabled={!summary || !canReject}
                title={canReject ? "清空当前已提交订单并退回重填" : "当前阶段已在裁决中，不能驳回"}
              >
                驳回重填
              </button>
            </div>
            <p className="field-hint review-action-hint">
              驳回重填可在订单收集或审理阶段随时使用，会清空当前阶段已提交订单；裁判也可以选中未提交席位，直接覆盖/代提交一份新订单。
            </p>
          </section>
          <section className="platform-panel judge-tools">
            <div className="panel-heading">
              <div>
                <span className="platform-kicker">OVERRIDE TOOLS</span>
                <h2>裁判覆盖</h2>
              </div>
            </div>
            <div className="judge-event-log">
              <div className="override-subheading">
                <h3>可重投事件</h3>
                <span>点击事件序号自动填入重投框</span>
              </div>
              {judgeView?.rerollable_events?.length ? (
                <div className="judge-event-list">
                  {[...judgeView.rerollable_events].reverse().map((event, index) => (
                    <button
                      key={`${String(event.sequence)}-${index}`}
                      className={rerollSequence === String(event.sequence) ? "selected" : ""}
                      onClick={() => setRerollSequence(String(event.sequence))}
                    >
                      <b>#{String(event.sequence)}</b>
                      <span>{String(event.phase ?? "")} · {String(event.message ?? event.type ?? "事件")}</span>
                      {Boolean(event.dice) && <small>骰点：{String((event.dice as JsonRecord).notation ?? "")} · 原始 {String((event.dice as JsonRecord).raw ?? "—")} · 调整后 {String((event.dice as JsonRecord).adjusted ?? "—")}</small>}
                    </button>
                  ))}
                </div>
              ) : (
                <p className="empty-plan">批准推进后，这里显示本回合待发布裁决中的骰点事件。</p>
              )}
            </div>
            <div className="judge-event-log referee-event-history">
              <div className="override-subheading">
                <h3>裁判事件日志</h3>
                <span>最近 {judgeView?.recent_events.length ?? 0} 条 · 仅裁判可见</span>
              </div>
              {judgeView?.recent_events.length ? (
                <div className="judge-event-list referee-event-list">
                  {[...judgeView.recent_events].reverse().map((event, index) => {
                    const dice = stateRecord(event.dice);
                    const rule = stateRecord(event.rule);
                    return (
                      <article className="referee-event-entry" key={`${String(event.sequence)}-${index}`}>
                        <b>#{String(event.sequence)}</b>
                        <span>T{String(event.turn)} · {String(event.phase)} · {String(event.message ?? event.type ?? "事件")}</span>
                        {Object.keys(dice).length > 0 && (
                          <small>骰点 {displayValue(dice.notation)}：原始 {displayValue(dice.raw)} · 调整后 {displayValue(dice.adjusted)}</small>
                        )}
                        {Object.keys(rule).length > 0 && (
                          <small>规则 {displayValue(rule.rule_id)}{rule.pdf_page ? ` · p.${String(rule.pdf_page)}` : ""}</small>
                        )}
                      </article>
                    );
                  })}
                </div>
              ) : (
                <p className="empty-plan">当前没有可显示的裁决事件。</p>
              )}
            </div>
            <div className="override-form">
              <label>
                舰船 ID
                <input
                  value={patchShipId}
                  onChange={(event) => setPatchShipId(event.target.value)}
                  placeholder="需要修正的舰船"
                />
              </label>
              <label className="wide-label">
                状态补丁 JSON
                <textarea
                  value={patchText}
                  onChange={(event) => setPatchText(event.target.value)}
                />
              </label>
              <button
                onClick={patchShip}
                disabled={!summary || !patchShipId.trim()}
              >
                覆盖舰船状态
              </button>
            </div>
            <div className="visibility-control-panel">
              <div className="override-subheading">
                <h3>玩家视野设置</h3>
                <span>{stormEnabled ? "风暴降临：有效半径减半" : "当前未启用风暴降临"}</span>
              </div>
              <div className="visibility-control-grid">
                <label>
                  轴心基础半径
                  <input type="number" min={0} max={99} step={stormEnabled ? 2 : 1} value={visibilityAxis} onChange={(event) => setVisibilityAxis(event.target.value)} />
                  <small>当前有效：{stormEnabled ? Math.floor(Number(visibilityAxis || 0) / 2) : Number(visibilityAxis || 0)} 格</small>
                </label>
                <label>
                  同盟基础半径
                  <input type="number" min={0} max={99} step={stormEnabled ? 2 : 1} value={visibilityAllies} onChange={(event) => setVisibilityAllies(event.target.value)} />
                  <small>当前有效：{stormEnabled ? Math.floor(Number(visibilityAllies || 0) / 2) : Number(visibilityAllies || 0)} 格</small>
                </label>
                <label className="visibility-storm-label">
                  <span><input type="checkbox" checked={hiddenContactsEnabled} onChange={(event) => setHiddenContactsEnabled(event.target.checked)} /> 隐藏接敌</span>
                </label>
                <label className="visibility-storm-label">
                  <span><input type="checkbox" checked={stormEnabled} onChange={(event) => setStormEnabled(event.target.checked)} /> 风暴降临</span>
                  <small>开启后全体玩家的有效视野半径按基础值减半。</small>
                </label>
              </div>
              <p className="field-hint">平台新对局默认轴心 14 格、同盟 12 格；裁判可在任意阶段覆盖。风暴降临开启时两边基础半径必须为偶数。</p>
              <button onClick={patchVisibility} disabled={!summary || !visibilityAxis.trim() || !visibilityAllies.trim()}>保存视野设置</button>
            </div>
            <div className="override-form">
              <label>
                重投事件序号
                <input
                  type="number"
                  value={rerollSequence}
                  onChange={(event) => setRerollSequence(event.target.value)}
                  placeholder="例如 33"
                />
              </label>
              <button
                onClick={reroll}
                disabled={!summary || !awaitingPublication || !rerollSequence.trim() || busy}
              >
                重骰并重新裁决
              </button>
              <button
                className="quiet"
                onClick={() =>
                  summary &&
                  void run(async () => {
                    const records = await platformOverrides(
                      session.token,
                      summary.match.match_id,
                    );
                    setMessage(`已读取 ${records.length} 条覆盖记录`);
                  })
                }
              >
                读取覆盖审计
              </button>
            </div>
          </section>
          <section className="platform-panel telegram-judge-panel">
            <div className="panel-heading">
              <div>
                <span className="platform-kicker">SIGNALS</span>
                <h2>阵营电报审阅</h2>
              </div>
              <button className="quiet" onClick={refreshTelegrams} disabled={!summary || busy}>刷新</button>
            </div>
            {judgeTelegrams.length ? (
              <div className="telegram-judge-list">
                {[...judgeTelegrams].reverse().map((telegram) => (
                  <article key={telegram.telegram_id}>
                    <header>
                      <b>第 {telegram.sent_turn} 回合 · {telegram.side === "axis" ? "轴心" : "同盟"}</b>
                      <span>{telegram.sender_username ?? telegram.sender_seat_id}</span>
                    </header>
                    <p>{telegram.message}</p>
                    <small>{telegram.rule_id}</small>
                    {telegram.resolved_turn == null ? (
                      <small>待下一回合接收判定 · {telegram.char_count} 字</small>
                    ) : (
                      <div className="telegram-judge-results">
                        {telegram.deliveries.length ? telegram.deliveries.map((delivery) => (
                          <span key={`${telegram.telegram_id}-${delivery.recipient_seat_id}`} className={delivery.delivered ? "delivered" : "failed"}>
                            {delivery.recipient_username ?? delivery.recipient_seat_id} · {delivery.delivered ? "送达" : "未送达"} · {delivery.char_count} 字 / D{delivery.die_sides} {delivery.roll} − 8×{delivery.hit_count} = {delivery.adjusted_roll}
                          </span>
                        )) : <small>本阵营没有其他接收席位</small>}
                      </div>
                    )}
                  </article>
                ))}
              </div>
            ) : <p className="empty-plan">本局尚无电报。</p>}
          </section>
        </div>
      ) : (
        <div className="platform-grid player-grid">
          <PlatformMap
            view={view}
            fullMap={false}
            onSelect={(ship) => setMessage(`已选中 ${ship.name}`)}
          />
          <section className="platform-panel player-seat-panel">
            <div className="panel-heading">
              <div>
                <span className="platform-kicker">YOUR SEAT</span>
                <h2>我的舰船席位</h2>
              </div>
              <button onClick={() => refreshView()} disabled={!selectedSeatId}>
                刷新视野
              </button>
            </div>
            {summary && ownSeats.length > 0 ? (
              <div className="seat-list">
                {ownSeats.map((seat) => (
                  <button
                    key={seat.seat_id}
                    className={
                      selectedSeatId === seat.seat_id ? "selected" : ""
                    }
                    onClick={() => {
                      setSelectedSeatId(seat.seat_id);
                      refreshView(seat.seat_id);
                    }}
                  >
                    <b>{seat.side === "axis" ? "轴心" : "同盟"} 席位</b>
                    <small>{seat.controller_type}</small>
                    <code>{seat.controlled_ship_ids.join(", ")}</code>
                  </button>
                ))}
              </div>
            ) : (
              <p className="empty-plan">载入对局后显示裁判绑定给你的席位。</p>
            )}
            {view && (
              <div className="player-snapshot">
                <div>
                  <b>{view.scenario_title}</b>
                  <span>
                    第 {view.turn}/{view.max_turns} 回合 · {view.phase}
                  </span>
                </div>
                <div className="snapshot-stats">
                  <span>可见舰船 {view.ships.length}</span>
                  <span>
                    已提交 {view.submitted_seat_count}/
                    {view.required_seat_count}
                  </span>
                </div>
                <div className="player-ships">
                  {(view.ships as unknown as Ship[]).map((ship) => {
                    const own = view.own_ship_ids.includes(ship.id);
                    return (
                      <article key={ship.id} className={own ? "own-ship" : "contact-ship"}>
                        <b>{ship.name ?? ship.id}</b>
                        <span>{own ? "本舰" : ship.signal_only ? ship.illuminated ? "照明接触 · 可炮击" : "信号标记 · 可炮击" : "可见接触"} · {positionLabel(ship.position)}</span>
                        {own ? (
                          <>
                            <div className="ship-core-status">
                              <span><b>舰体</b>{ship.hull ?? "—"}/{ship.max_hull ?? "—"}</span>
                              <span><b>航速</b>{ship.current_speed ?? "—"}/{ship.max_speed ?? "—"} MF</span>
                              <span><b>航向</b>{ship.heading ?? "—"}</span>
                              <span><b>本阶段</b>{ship.sunk ? "已沉没" : ship.fired ? "已开火" : "可行动"}</span>
                            </div>
                            <div className="ship-state-tags" aria-label="舰船损伤状态">
                              <span className={ship.sunk ? "bad" : "good"}>{ship.sunk ? "沉没" : "漂浮"}</span>
                              <span className={ship.fire_markers > 0 ? "bad" : "good"}>{ship.fire_markers > 0 ? `起火 ${ship.fire_markers}` : "无火灾"}</span>
                              {ship.rudder_destroyed && <span className="bad">舵机损毁</span>}
                              {ship.bridge_destroyed && <span className="bad">舰桥损毁</span>}
                              {ship.radar_destroyed && <span className="bad">雷达损毁</span>}
                              {ship.mfc_destroyed && <span className="bad">射击指挥仪损毁</span>}
                              {ship.captain_status && ship.captain_status !== "fit" && <span className="bad">舰长{captainStatusLabel[ship.captain_status] ?? ship.captain_status}</span>}
                              {!ship.rudder_destroyed && !ship.bridge_destroyed && !ship.radar_destroyed && !ship.mfc_destroyed && (!ship.captain_status || ship.captain_status === "fit") && <span className="good">主要模块正常</span>}
                            </div>
                            <div className="ship-module-block">
                              <strong>炮位</strong>
                              <div className="ship-module-list">
                                {ship.gun_mounts.length ? ship.gun_mounts.map((mount) => (
                                  <span className={mount.destroyed ? "bad" : mount.fired_this_phase ? "used" : "good"} key={mount.id}>
                                    {mount.id} · {gunKindLabel[mount.kind] ?? mount.kind} · {mount.caliber}&quot; · {mount.destroyed ? "损毁" : mount.fired_this_phase ? "本阶段已射击" : "可用"}
                                  </span>
                                )) : <span className="muted">暂无炮位数据</span>}
                              </div>
                            </div>
                            <div className="ship-module-block">
                              <strong>其他模块</strong>
                              <div className="ship-module-list">
                                <span className={ship.mfc_destroyed ? "bad" : "good"}>射击指挥仪 · {ship.mfc_destroyed ? "损毁" : "正常"}</span>
                                <span className={ship.radar_destroyed ? "bad" : "good"}>雷达 · {ship.radar_destroyed ? "损毁" : "正常"}</span>
                                <span className={ship.bridge_destroyed ? "bad" : "good"}>舰桥 · {ship.bridge_destroyed ? "损毁" : "正常"}</span>
                                <span className={ship.rudder_destroyed ? "bad" : "good"}>舵机 · {ship.rudder_destroyed ? "损毁" : "正常"}</span>
                              <span className={ship.captain_status === "killed" ? "bad" : ship.captain_status === "wounded" ? "used" : "good"}>舰长 · {captainStatusLabel[ship.captain_status ?? "fit"] ?? "正常"}</span>
                              {ship.captain_command_lost && <span className="bad">本回合失去指挥 · 原速直航</span>}
                              </div>
                            </div>
                            <div className="ship-module-block">
                              <strong>鱼雷发射器</strong>
                              <div className="ship-module-list">
                                {ship.torpedo_launchers.length ? ship.torpedo_launchers.map((launcher) => (
                                  <span className={launcher.destroyed ? "bad" : "good"} key={launcher.id}>
                                    {launcher.id} · {launcher.destroyed ? "损毁" : `装填 ${launcher.loaded}/${launcher.torpedoes} · 备雷 ${launcher.reloads_remaining}`}
                                  </span>
                                )) : <span className="muted">无鱼雷发射器</span>}
                              </div>
                            </div>
                          </>
                        ) : (
                          <>
                            <span>航速 {String(ship.current_speed ?? "—")}</span>
                            <span>
                              上回合航迹（可见段）：{(view.ship_trajectories?.[ship.id] ?? []).length
                                ? (view.ship_trajectories?.[ship.id] ?? []).join(" → ")
                                : "未发现"}
                            </span>
                            <span>仅显示本席位视野内的匿名接触</span>
                          </>
                        )}
                      </article>
                    );
                  })}
                </div>
              </div>
            )}
          </section>
          <section className="platform-panel order-panel">
            <div className="panel-heading">
              <div>
                <span className="platform-kicker">SECRET ORDERS</span>
                <h2>封存本席位行动</h2>
              </div>
              {view && <span className="match-meta">{view.stage}</span>}
            </div>
            {view ? (
              <>
                <div className="phase-banner">
                  <b>{view.phase === "movement_planning" ? "移动阶段" : view.phase === "gunnery" ? "炮击阶段" : view.phase === "torpedo_planning" ? "鱼雷阶段" : view.phase === "reinforcement" ? "增援 / 预编排" : "本阶段确认"}</b>
                  <span>
                    {view.phase === "movement_planning"
                      ? "为本席位的每条舰船写入航向计划；没有动作的舰船保持 0。"
                      : view.phase === "gunnery"
                        ? "选择可见接触和炮位。目标列表中的接触不区分阵营，误伤由规则核心照常裁定。"
                      : view.phase === "torpedo_planning"
                          ? "鱼雷不指定目标；航迹与重合舰船接触后由规则核心逐舰裁决。"
                          : view.phase === "reinforcement"
                            ? "在回合开始一次填写并提交本回合全部行动：增援、移动、鱼雷、炮击、照明弹和探照灯。裁判批准后，规则引擎按各阶段依次结算。"
                          : "本阶段没有专用控件时，直接确认或使用高级 JSON 补充行动。"}
                  </span>
                </div>
                <section className="player-order-summary" aria-label="本阶段订单填写状态">
                  <header>
                    <b>{view.phase === "reinforcement" ? "本回合订单清单" : "本阶段订单清单"}</b>
                    <span>{draftSummarySections.reduce((total, section) => total + section.rows.length, 0)} 项已写入</span>
                  </header>
                  {!orderDraft ? (
                    <p className="draft-summary-error">订单草稿格式有误，请展开高级 JSON 修复后再提交。</p>
                  ) : (
                    <div className="player-order-summary-grid">
                      {draftSummarySections.map((section) => (
                        <article className={section.rows.length ? "has-orders" : "no-orders"} key={section.key}>
                          <div><b>{section.label}</b><span>{section.rows.length ? `已填写 ${section.rows.length} 项` : "尚未填写"}</span></div>
                          {section.rows.length > 0 && (
                            <ul>{section.rows.slice(0, 4).map((row, index) => (
                              <li key={`${section.key}-${index}`}>{section.describe(row)}</li>
                            ))}{section.rows.length > 4 && <li>另有 {section.rows.length - 4} 项</li>}</ul>
                          )}
                        </article>
                      ))}
                    </div>
                  )}
                </section>
                {draftCommands.length > 0 && (
                  <section className="player-draft-commands" aria-label="已写入命令">
                    <header>
                      <b>已写入命令 · {draftCommands.length}</b>
                      <span>可逐条取消；取消后再次提交，才会更新已交订单</span>
                    </header>
                    <div>
                      {draftCommands.map(({ field, value, index }) => (
                        <div className="player-draft-command" key={`${field}-${index}`}>
                          <span><b>{draftCommandLabels[field]}</b>{draftCommandDescription(field, value)}</span>
                          <button type="button" className="quiet" onClick={() => removeOrderRow(field, index)} aria-label={`取消${draftCommandLabels[field]}命令`}>
                            取消
                          </button>
                        </div>
                      ))}
                    </div>
                  </section>
                )}
                {view.stage === "awaiting_publication" && (
                  <div className="review-readiness" role="status">
                    裁判正在审理本回合结果。此处仍显示上一次已发布状态；裁判发布后地图、舰船状态和电报会自动更新。
                  </div>
                )}
                {captainCommandLost && (
                  <div className="review-error captain-command-notice" role="status">
                    本回合舰长失能，无法下达人工行动。提交时该舰订单会自动移除，规则引擎将令其保持原速直航。
                  </div>
                )}
                {(view.phase === "movement_planning" || view.phase === "reinforcement") && (
                  <section className="player-action-builder">
                    <div className="action-card">
                      <h3>{ownShips.length === 1 ? "本舰移动计划" : "舰船移动计划"}</h3>
                      <div className="action-grid">
                        {ownShips.length > 1 ? (
                          <label>
                            舰船
                            <select
                              value={selectedActionShip?.id ?? ""}
                              onChange={(event) => setActionShipId(event.target.value)}
                            >
                              {ownShips.map((ship) => (
                                <option key={ship.id} value={ship.id}>
                                  {ship.name} · {ship.id}
                                </option>
                              ))}
                            </select>
                          </label>
                        ) : (
                          <div className="action-readonly">
                            <span>本舰</span>
                            <b>{selectedActionShip?.name ?? "未绑定舰船"}</b>
                          </div>
                        )}
                        <label>
                          移动计划
                          <input
                            value={movementPlan}
                            onChange={(event) => setMovementPlan(event.target.value)}
                            placeholder="例如 0、3PP2"
                          />
                        </label>
                        <label>
                          速度
                          <input
                            type="number"
                            value={movementSpeed}
                            onChange={(event) => setMovementSpeed(event.target.value)}
                            placeholder={selectedActionShip?.current_speed != null ? String(selectedActionShip.current_speed) : "沿用规则"}
                          />
                        </label>
                      </div>
                      <p className="field-hint movement-format-hint">
                        格式：<code>0</code> 不移动；数字表示直航 MF；<code>P/S</code> 是左/右转 60°；<code>PP/SS</code> 是左/右转 120°。转向后必须再推进 1 MF，例如 <code>3PP2</code>。
                      </p>
                      <p className="field-hint">
                        {selectedActionShip
                          ? `当前航向 ${selectedActionShip.heading} · 合法速度 ${selectedActionShip.min_legal_speed ?? "—"}–${selectedActionShip.max_legal_speed ?? "—"}`
                          : "请先选择一条舰船。"}
                      </p>
                      {movementPlanWarnings.length > 0 && (
                        <div className="review-error" role="alert">
                          <strong>移动计划有待修正</strong>
                          <ul>{movementPlanWarnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
                          <span>最终提交仍会由规则引擎校验；如需强行送交裁判，请在提交时报错后明确选择忽略。</span>
                        </div>
                      )}
                      <button onClick={writeMovementOrder} disabled={!selectedActionShip || captainCommandLost}>
                        写入移动计划
                      </button>
                    </div>
                  </section>
                )}
                {(view.phase === "gunnery" || view.phase === "reinforcement") && (
                  <section className="player-action-builder">
                    <div className="action-card">
                      <h3>炮击目标</h3>
                      <div className="action-grid">
                        <label>
                          开火舰船
                          <select
                            value={selectedActionShip?.id ?? ""}
                            onChange={(event) => {
                              setActionShipId(event.target.value);
                            }}
                          >
                            {ownShips.map((ship) => (
                              <option key={ship.id} value={ship.id}>
                                {ship.name} · {ship.id}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label>
                          可见目标
                          <select
                            value={selectedActionTarget?.id ?? ""}
                            onChange={(event) => setActionTargetId(event.target.value)}
                          >
                            {visibleTargets.map((ship) => (
                              <option key={ship.id} value={ship.id}>
                                {targetLabel(ship)}
                              </option>
                            ))}
                          </select>
                        </label>
                      </div>
                      <div className="mount-picker">
                        <span>自动计算的可射炮位</span>
                        {selectedActionShip?.gun_mounts.length ? selectedActionShip.gun_mounts.map((mount) => {
                          const status = mount.destroyed
                            ? "已损毁"
                            : mount.fired_this_phase
                              ? "已使用"
                              : fireableMountIds.has(mount.id)
                                ? "可射击"
                                : "目标/视野条件不满足";
                          return <small className={fireableMountIds.has(mount.id) ? "mount-ready" : "mount-unavailable"} key={mount.id}>
                            {mount.id} · GF {mount.firepower} · {status}
                          </small>;
                        }) : <small>当前舰船没有炮位资料</small>}
                      </div>
                      <p className="field-hint">选择目标后，炮位可射性由规则引擎按当前视野和射界自动计算；订单自动包含全部可射炮位。</p>
                      <button onClick={writeGunneryOrder} disabled={!selectedActionShip || captainCommandLost || !selectedActionTarget || !fireableMounts.length}>
                        写入炮击草稿
                      </button>
                      <div className="special-fire-options">
                        <h4>照明与探照灯</h4>
                        {view.phase !== "gunnery" && view.phase !== "reinforcement" && (
                          <p className="field-hint">照明弹和探照灯在回合开始时与其他行动一并提交。</p>
                        )}
                        {view.phase === "reinforcement" && (
            <p className="field-hint">照明弹和探照灯现在直接写入本回合订单，并与移动、鱼雷、炮击一同提交给裁判。</p>
                        )}
                        <div className="action-grid">
                          <label>
                            照明弹（闪光弹）目标格
                            <input
                              value={illuminationHexLabel}
                              onChange={(event) => setIlluminationHexLabel(event.target.value.toUpperCase())}
                              placeholder="例如 U40"
                              disabled={(view.phase !== "gunnery" && view.phase !== "reinforcement") || !view.optional_rules?.star_shells}
                            />
                          </label>
                          <div className="action-readonly">
                            <span>照明弹自动炮位</span>
                            <b>{illuminationMountChoice
                              ? `${illuminationMountChoice.id} · GF ${illuminationMountChoice.firepower}`
                              : "没有可用主炮/副炮组"}</b>
                          </div>
                          <label>
                            探照灯目标
                            <select
                              value={searchlightTargetId}
                              onChange={(event) => setSearchlightTargetId(event.target.value)}
                              disabled={(view.phase !== "gunnery" && view.phase !== "reinforcement") || !view.optional_rules?.searchlights}
                            >
                              <option value="">请选择可见舰船</option>
                              {visibleTargets.map((ship) => <option key={ship.id} value={ship.id}>{targetLabel(ship)}</option>)}
                            </select>
                          </label>
                        </div>
                        <p className="field-hint">目标格按海图编号输入，例如 U40；系统自动换算。照明弹优先占用不能对当前炮击目标开火的主/副炮组；同级按 GF 从低到高选，多发时自动改用下一组。</p>
                        <div className="action-row">
                          <button
                            onClick={writeIlluminationOrder}
                            disabled={captainCommandLost || (view.phase !== "gunnery" && view.phase !== "reinforcement") || !view.optional_rules?.star_shells || !selectedActionShip || !illuminationMountChoice || !parseMapHexLabel(illuminationHexLabel)}
                          >
                            {illuminationDrafts.some((order) => order.ship_id === selectedActionShip?.id) ? "再加入一发照明弹" : "写入第一发照明弹"}
                          </button>
                          <button
                            onClick={writeSearchlightOrder}
                            disabled={captainCommandLost || (view.phase !== "gunnery" && view.phase !== "reinforcement") || !view.optional_rules?.searchlights || !selectedActionShip || !searchlightTargetId}
                          >
                            写入探照灯
                          </button>
                        </div>
                        <div className="illumination-draft-list">
                          <b>本回合照明弹草稿（{illuminationDrafts.length} 发）</b>
                          <span className="field-hint">每次写入一发，系统会自动分配下一组未占用炮位；下方逐发列出并可取消。</span>
                          {illuminationDrafts.length ? illuminationDrafts.map((order, index) => (
                            <div className="illumination-draft-row" key={`${String(order.ship_id)}-${String(order.mount_id)}-${index}`}>
                              <span>
                                {String(order.ship_id)} · 炮位 {String(order.mount_id ?? "未指定")} · 格点 {positionLabel(order.target_hex)}
                              </span>
                              <button type="button" className="quiet" onClick={() => removeIlluminationOrder(index)}>
                                移除
                              </button>
                            </div>
                          )) : <span className="field-hint">可加入多条；不同炮位可分别投放，裁判会逐条审理。</span>}
                        </div>
                        <div className="illumination-draft-list">
                          <b>本回合探照灯草稿（{searchlightDrafts.length}）</b>
                          {searchlightDrafts.length ? searchlightDrafts.map((order, index) => {
                            const ship = ownShips.find((item) => item.id === order.ship_id);
                            const target = visibleTargets.find((item) => item.id === order.target_id);
                            return (
                              <div className="illumination-draft-row" key={`searchlight-${String(order.ship_id)}-${index}`}>
                                <span>{ship?.name ?? "本舰"} → {target ? targetLabel(target) : "目标待刷新"}</span>
                                <button type="button" className="quiet" onClick={() => removeOrderRow("searchlights", index)}>移除</button>
                              </div>
                            );
                          }) : <span className="field-hint">可加入多条探照灯订单。</span>}
                        </div>
                        <p className="field-hint">
                          照明弹（引擎字段 illumination）支持同一阶段多发，每条订单独立掷骰：{view.optional_rules?.star_shells ? "已启用" : "本想定未启用（IBS-R-09.3）"}；探照灯规则：{view.optional_rules?.searchlights ? "已启用" : "本想定未启用（IBS-R-09.4）"}。
                        </p>
                      </div>
                    </div>
                  </section>
                )}
                {(view.phase === "torpedo_planning" || view.phase === "reinforcement") && (
                  <section className="player-action-builder">
                    <div className="action-card">
                      <h3>鱼雷发射草稿</h3>
                      <div className="action-grid">
                        <label>
                          发射舰船
                          <select
                            value={selectedActionShip?.id ?? ""}
                            onChange={(event) => {
                              const ship = ownShips.find((item) => item.id === event.target.value);
                              setActionShipId(event.target.value);
                              setTorpedoLauncherId(ship?.torpedo_launchers.find((launcher) => !launcher.destroyed && launcher.loaded > 0)?.id ?? "");
                              // 换舰船时清空发射器相关选择，避免沿用上一艘舰的舷侧/发射角/枚数。
                              setTorpedoLaunchSide("");
                              setTorpedoLaunchAngle("");
                              setTorpedoCount("1");
                            }}
                          >
                            {ownShips.filter((ship) => ship.torpedo_launchers.some((launcher) => !launcher.destroyed && launcher.loaded > 0)).map((ship) => (
                              <option key={ship.id} value={ship.id}>
                                {ship.name} · {ship.id}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label>
                          发射器
                          <select
                            value={selectedLauncher?.id ?? ""}
                            onChange={(event) => {
                              const nextId = event.target.value;
                              setTorpedoLauncherId(nextId);
                              // 换发射器后按新发射器的合法值重置依赖字段：舷侧与发射角随发射器变化，
                              // 枚数不能超过新发射器的装载量；发射 MF 属舰级（随航路），保持不变。
                              const nextCandidate = selectedTorpedoCandidate?.launchers.find(
                                (candidate) => candidate.launcher_id === nextId,
                              );
                              const nextLauncher = selectedActionShip?.torpedo_launchers.find(
                                (launcher) => launcher.id === nextId,
                              );
                              const nextSides = nextCandidate?.sides.length
                                ? nextCandidate.sides
                                : (nextLauncher?.arcs ?? []).filter((arc) => arc === "port" || arc === "starboard");
                              setTorpedoLaunchSide(nextSides[0] ?? "");
                              setTorpedoLaunchAngle(nextCandidate?.angles[0] ?? "");
                              const nextLoaded = Math.max(1, nextCandidate?.loaded ?? nextLauncher?.loaded ?? 1);
                              setTorpedoCount(String(Math.min(Math.max(1, Number(torpedoCount) || 1), nextLoaded)));
                            }}
                          >
                            {launcherOptions.map((option) => (
                              <option key={option.id} value={option.id}>
                                {option.label}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label>
                          枚数
                          <input type="number" min="1" max={Math.max(1, selectedLauncherRemaining)} value={torpedoCount} onChange={(event) => setTorpedoCount(event.target.value)} />
                        </label>
                        <label>
                          发射 MF
                          <select
                            value={selectedLaunchPosition?.mf ?? ""}
                            disabled={launchPositions.length === 0}
                            onChange={(event) => setTorpedoLaunchAtMf(event.target.value)}
                          >
                            {launchPositions.length === 0 && <option value="">请先在移动草稿里写出该舰航路</option>}
                            {launchPositions.map((position) => (
                              <option key={position.mf} value={position.mf}>
                                MF {position.mf} · {position.heading} 舰首
                              </option>
                            ))}
                          </select>
                        </label>
                        <label>
                          舷侧
                          <select value={selectedLaunchSide} onChange={(event) => setTorpedoLaunchSide(event.target.value)}>
                            {launchSides.map((side) => <option key={side} value={side}>{side === "port" ? "左舷" : "右舷"}</option>)}
                          </select>
                        </label>
                        <label>
                          发射角
                          <select value={selectedLaunchAngle} onChange={(event) => setTorpedoLaunchAngle(event.target.value)}>
                            {launchAngles.map((angle) => <option key={angle} value={angle}>{angle}</option>)}
                          </select>
                        </label>
                        <label>
                          速度设定
                          <select value={selectedSettingIndex} onChange={(event) => setTorpedoSettingIndex(event.target.value)}>
                            {(selectedTorpedoCandidate?.settings ?? []).map((setting) => (
                              <option key={setting.index} value={setting.index}>
                                {setting.index + 1} · {setting.speed.join("-")} MF · 射程 {setting.range}
                              </option>
                            ))}
                          </select>
                        </label>
                      </div>
                      <p className="field-hint">
                        鱼雷订单不指定目标：航迹与任何未沉没舰船重合时，规则核心会对重合的每条船分别进行命中裁决，不区分敌我。同一发射器可写入多条独立订单，累计枚数不能超过当前装载量（本发射器剩余 {selectedLauncherRemaining} 枚）。
                        {launchPositions.length > 0
                          ? " 发射格和舰首航向由规则核心从本回合移动草稿投影后自动带出；提交时仍会整体严格校验。"
                          : " 当前拿不到合法 MF 列表：请先在本席位「移动草稿」里写出这艘舰的航路（计划与速度），MF 列表会随草稿自动出现。引擎要求鱼雷行的发射格必须等于该 MF 处的航路位置，所以这里不提供手填 MF——手填会产出没有发射格的行，提交时必然报 launch_hex 不符。"}
                        {selectedTorpedoCandidate?.blocked_reason ? ` 引擎当前判定：${selectedTorpedoCandidate.blocked_reason}。` : ""}
                      </p>
                      <button onClick={writeTorpedoOrder} disabled={!selectedActionShip || captainCommandLost || !selectedLauncherCandidate || !selectedLaunchPosition || selectedLauncherRemaining <= 0 || !selectedLaunchSide || !selectedLaunchAngle}>
                        写入鱼雷草稿
                      </button>
                      {torpedoDrafts.filter((item) => item.ship_id === selectedActionShip?.id).length > 0 && (() => {
                        // 当前移动草稿给出的合法 MF 集合：不在其中的行会被引擎以 launch_hex 不符拒绝，
                        // 这里直接标出来，免得玩家提交时才看到那句看不懂的报错。
                        const planMfs = new Set(launchPositions.map((position) => position.mf));
                        return (
                        <div className="draft-order-list">
                          <strong>已写入发射</strong>
                          {torpedoDrafts.map((item, index) => item.ship_id === selectedActionShip?.id ? (
                            <div className="draft-order-row" key={`torpedo-draft-${index}`}>
                              <span>
                                {String(item.launcher_id)} · {Number(item.count ?? 0)} 枚 · MF {String(item.launch_at_mf ?? "?")} · {String(item.launch_side ?? "")} {String(item.launch_angle ?? "")}
                                {!planMfs.has(Number(item.launch_at_mf)) && (
                                  <b className="draft-order-warning">
                                    ⚠ 该 MF 不在当前移动草稿的航路上（可用：{launchPositions.length ? launchPositions.map((position) => position.mf).join("、") : "尚无，请先写该舰的移动草稿"}）；请移除后按可用 MF 重写，否则提交会被引擎拒绝。
                                  </b>
                                )}
                              </span>
                              <button type="button" className="quiet" onClick={() => updateOrderDraft((batch) => {
                                const rows = Array.isArray(batch.torpedoes) ? [...(batch.torpedoes as unknown[])] : [];
                                rows.splice(index, 1);
                                batch.torpedoes = rows;
                              }, "已移除一条鱼雷发射订单")}>移除</button>
                            </div>
                          ) : null)}
                        </div>
                        );
                      })()}
                    </div>
                  </section>
                )}
                {(view.phase === "gunnery" || view.phase === "reinforcement") && (
                  <section className="player-action-builder">
                    <div className="action-card">
                      <h3>烟幕（拉烟）</h3>
                      <div className="action-grid">
                        <label>
                          施放舰船
                          <select
                            value={smokeShipId}
                            onChange={(event) => setSmokeShipId(event.target.value)}
                          >
                            <option value="">选择舰船</option>
                            {ownShips.map((ship) => (
                              <option key={ship.id} value={ship.id}>
                                {ship.name} · {ship.id}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="check-label">
                          <input
                            type="checkbox"
                            checked={smokeDeploy}
                            onChange={(event) => setSmokeDeploy(event.target.checked)}
                          />
                          本回合施放烟幕
                        </label>
                      </div>
                      <button
                        type="button"
                        onClick={writeSmokeOrder}
                        disabled={!smokeShipId || captainCommandLost || !view.optional_rules?.smoke}
                      >
                        写入拉烟指令
                      </button>
                      {draftRows("smoke").length > 0 && (
                        <div className="draft-order-list">
                          <strong>已写入拉烟</strong>
                          {draftRows("smoke").map((item, index) => (
                            <div className="draft-order-row" key={`smoke-draft-${index}`}>
                              <span>
                                {ownShips.find((ship) => ship.id === item.ship_id)?.name ?? String(item.ship_id ?? "")}
                                {" · "}
                                {item.deploy === false ? "本回合不施放" : "施放"}
                              </span>
                              <button type="button" className="quiet" onClick={() => removeOrderRow("smoke", index)}>移除</button>
                            </div>
                          ))}
                        </div>
                      )}
                      <p className="field-hint">
                        烟幕只持续本回合（引擎在回合推进时清零），需要每回合重新声明；可写多条订单，取消勾选表示本回合对该舰不施放。
                        {view.optional_rules?.smoke
                          ? " 施放资格与阶段合法性由规则核心校验（仅驱逐舰/轻巡可拉烟），提交时若有问题会给出具体原因。"
                          : " 本想定未启用烟幕可选规则，现在写入也会被规则核心拒绝——请让裁判在想定里开启。"}
                      </p>
                    </div>
                  </section>
                )}
                <details className="advanced-json">
                  <summary>高级 JSON · 精确编辑本席位订单</summary>
                  <label className="wide-label">
                    本席位订单 JSON
                    <textarea
                      value={orderText}
                      onChange={(event) => {
                        setOrderText(event.target.value);
                        setOrderValidationErrors([]);
                      }}
                    />
                  </label>
                </details>
                <p className="field-hint">
                  订单只会提交给裁判；同阵营其他席位不会看到你的视野或草稿。
                </p>
                <p className="order-validation-default" role="note">
                  默认严格校验：发现非法订单会停止提交。只有出现报错后，再明确选择“忽略报错并提交”才会送交裁判审理；规则引擎仍会按规则裁决。
                </p>
                <button
                  className="primary"
                  onClick={() => submitOrder(false)}
                  disabled={
                    busy ||
                    view.stage === "resolving" ||
                    view.stage === "awaiting_publication" ||
                    view.stage === "complete"
                  }
                >
                  {view.phase === "reinforcement"
                    ? view.seat_has_submitted ? "更新本回合行动" : "提交本回合全部行动"
                  : view.seat_has_submitted ? "更新本阶段行动" : "提交本阶段行动"}
                </button>
                {orderValidationErrors.length > 0 && (
                  <div className="player-order-validation" role="alert">
                    <b>发现以下校验报错，订单尚未提交</b>
                    <ul>{orderValidationErrors.map((item, index) => <li key={`submit-validation-${index}`}>{item}</li>)}</ul>
                    <p>忽略只会允许把订单送交裁判审理，不会绕过规则引擎的最终裁决。</p>
                    <button type="button" className="danger" disabled={busy || view.stage === "awaiting_publication"} onClick={() => submitOrder(true)}>
                      确认忽略报错并提交给裁判
                    </button>
                  </div>
                )}
                {view.seat_has_submitted && (
                  <p className="order-submit-success" role="status">
                    <span aria-hidden="true">✓</span>
                    已成功提交，等待裁判审理。可修改订单后再次提交。
                  </p>
                )}
              </>
            ) : (
              <p className="empty-plan">先输入对局 ID 并载入。</p>
            )}
          </section>
          <section className="platform-panel telegram-player-panel">
            <div className="panel-heading">
              <div>
                <span className="platform-kicker">SIGNALS</span>
                <h2>阵营电报</h2>
              </div>
              <button className="quiet" onClick={refreshTelegrams} disabled={!selectedSeatId || busy}>刷新</button>
            </div>
            {view ? (
              <>
                <label className="wide-label">
                  第 {view.turn} 回合发给队友的话
                  <textarea value={telegramDraft} onChange={(event) => setTelegramDraft(event.target.value)} placeholder="写给同阵营其他席位；本回合可修改，进入下回合后逐席位判定。" maxLength={10000} />
                </label>
                <div className="telegram-compose-footer">
                  <span>{Array.from(telegramDraft.replace(/\s/gu, "")).length} 字 · {view.storm ? "风暴 D50" : "D100"}；字数必须小于骰值才送达</span>
                  <button className="primary" onClick={submitTelegram} disabled={busy || !telegramDraft.trim() || view.stage === "awaiting_publication" || view.stage === "complete"}>
                    {telegramInbox?.sent_message ? "更新本回合电报" : "封存电报"}
                  </button>
                </div>
                <p className="field-hint">电报会在下回合由每位队友分别掷骰；发送回合接收舰每被命中一次，接收骰点 -8（最低为 1）。失败时只显示未送达提示，不泄露正文。裁判可以查看全文和各席位骰点。</p>
                <div className="telegram-inbox">
                  <h3>收到的队友电报</h3>
                  {telegramInbox?.messages.length ? [...telegramInbox.messages].reverse().map((item) => (
                    <article key={`${item.telegram_id}-${item.received_turn}`} className={item.delivered ? "delivered" : "failed"}>
                      <header><b>第 {item.sent_turn} 回合电报</b><span>第 {item.received_turn} 回合 · {item.delivered ? "已送达" : "接收失败"}</span></header>
                      {item.delivered ? <p>{item.message}</p> : <p>未能接收到电报正文。</p>}
                    </article>
                  )) : <p className="empty-plan">尚未收到电报。</p>}
                </div>
              </>
            ) : <p className="empty-plan">载入对局后可发送和接收电报。</p>}
          </section>
        </div>
      )}
      {error && <pre className="platform-error">{error}</pre>}
    </main>
  );
}
