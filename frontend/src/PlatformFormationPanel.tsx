import { useEffect, useState } from "react";
import type { PlatformFormation, PlatformFormationCommand } from "./api";
import type { Ship } from "./types";

type Props = {
  formations: PlatformFormation[];
  commands: PlatformFormationCommand[];
  ships: Ship[];
  phase: string;
  turn: number;
  disabled?: boolean;
  onWrite: (command: PlatformFormationCommand) => void;
};

function FormationRow({ formation, command, ships, phase, disabled, onWrite }: Omit<Props, "formations" | "commands" | "turn"> & {
  formation: PlatformFormation;
  command?: PlatformFormationCommand;
}) {
  const flagship = ships.find((ship) => ship.id === formation.commanding_ship_id);
  const [plan, setPlan] = useState(command?.leader_plan ?? String(flagship?.current_speed ?? 0));
  const [spacing, setSpacing] = useState<1 | 2>(command?.spacing ?? formation.spacing);
  const [heading, setHeading] = useState(command?.heading ?? formation.heading ?? 1);
  const [turnMode, setTurnMode] = useState(command?.turn_mode ?? formation.turn_mode);
  const [reduceSpeed, setReduceSpeed] = useState(command?.reduce_speed == null ? "" : String(command.reduce_speed));
  const savedCommand = JSON.stringify(command);
  useEffect(() => {
    if (!command) return;
    setPlan(command.leader_plan);
    setSpacing(command.spacing);
    setHeading(command.heading ?? formation.heading ?? 1);
    setTurnMode(command.turn_mode);
    setReduceSpeed(command.reduce_speed == null ? "" : String(command.reduce_speed));
  }, [savedCommand]);
  const setup = phase === "formation_setup";
  const canEdit = setup || phase === "reinforcement" || phase === "movement_planning";
  const following = formation.role === "follower";
  const ownShip = ships.find((ship) => formation.own_ship_ids?.includes(ship.id));
  return <div className="action-card">
    <div className="panel-heading">
      <h3>{formation.name}</h3>
      <span>{following ? "自动跟随" : command ? "已写入编队命令" : "旗舰指挥 · 待写入"}</span>
    </div>
    <p>默认移动方式：{formation.turn_mode === "synchronized" ? "全队同步机动" : "鱼贯跟随航迹"} · 舰间距 {formation.spacing} 格</p>
    {formation.ship_ids && <p>{formation.ship_ids.map((id) => ships.find((ship) => ship.id === id)?.name ?? id).join(" / ")}</p>}
    {following ? <p className="field-hint">{ownShip?.command_status === "straggling" ? "本舰暂时掉队，正在追赶队尾" : "本舰移动随旗舰航路执行"}</p> : canEdit && <>
      <div className="action-grid">
        <div className="action-readonly"><span>指挥旗舰</span><b>{flagship?.name ?? "本舰"}</b></div>
        {setup ? <>
          <label>转向方式<select value={turnMode} onChange={(event) => setTurnMode(event.target.value as PlatformFormationCommand["turn_mode"])}>
            <option value="column_follow">鱼贯转向</option><option value="synchronized">同步转向</option>
          </select></label>
          <label>初始航向<select value={heading} onChange={(event) => setHeading(Number(event.target.value))}>{[1, 2, 3, 4, 5, 6].map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        </> : <>
          <label>旗舰航路<input value={plan} onChange={(event) => setPlan(event.target.value.toUpperCase())} placeholder="例如 3、1P2" /></label>
          <label>本回合队形移动<select value={turnMode} onChange={(event) => setTurnMode(event.target.value as PlatformFormationCommand["turn_mode"])}>
            <option value="column_follow">全体跟随领舰航路（鱼贯）</option><option value="synchronized">全体与领舰同步机动</option>
          </select></label>
          <label>全队降至 MF<input type="number" min={0} max={8} value={reduceSpeed} onChange={(event) => setReduceSpeed(event.target.value)} placeholder="不降速时留空" /></label>
        </>}
        <label>舰间距<select value={spacing} onChange={(event) => setSpacing(Number(event.target.value) as 1 | 2)}><option value={1}>1 格</option><option value={2}>2 格</option></select></label>
      </div>
      {!setup && <p className="field-hint">鱼贯模式沿领舰航迹自动跟进；未作加减速准备时，每艘队员航速与紧邻前舰相差最多 2 MF，并遵守自身合法速度与舰间距。同步模式全队执行相同舵令。航路：数字为直航 MF，P/S 为左/右转 60°，PP/SS 为左/右转 120°。领舰合法速度 {flagship?.min_legal_speed ?? "?"}–{flagship?.max_legal_speed ?? "?"} MF。</p>}
      {!setup && plan.trim() === "0" && <p className="field-hint">⚠ 航路填 0 = 本回合原地不动：本席位与跟随舰都不会有鱼雷发射 MF。要打鱼雷请改成有实际机动的航路（例如 3、2P）再写入。</p>}
      <button type="button" disabled={disabled || flagship?.captain_command_lost || (!setup && !plan.trim())}
        onClick={() => onWrite({ formation_id: formation.formation_id, leader_plan: plan, spacing, heading, turn_mode: turnMode, reduce_speed: reduceSpeed === "" ? null : Number(reduceSpeed) })}>
        {setup ? "写入编队设置" : "写入本回合编队移动"}
      </button>
    </>}
  </div>;
}

export function PlatformFormationPanel(props: Props) {
  if (!props.formations.length) return null;
  return <section className="player-action-builder" aria-label="旗舰编队指挥">
    {props.formations.map((formation) => <FormationRow key={`${props.turn}:${props.phase}:${formation.formation_id}`}
      formation={formation} command={props.commands.find((command) => command.formation_id === formation.formation_id)}
      ships={props.ships} phase={props.phase} disabled={props.disabled} onWrite={props.onWrite} />)}
  </section>;
}
