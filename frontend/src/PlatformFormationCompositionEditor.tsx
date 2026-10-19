import { useEffect, useMemo, useState } from "react";
import type { PlatformJudgeFormationGroup, PlatformView } from "./api";

type Props = {
  view?: PlatformView;
  disabled?: boolean;
  onSave: (side: "axis" | "allies", formations: PlatformJudgeFormationGroup[]) => void;
};

type GroupDraft = PlatformJudgeFormationGroup;

export function PlatformFormationCompositionEditor({ view, disabled, onSave }: Props) {
  const [side, setSide] = useState<"axis" | "allies">("axis");
  const ships = useMemo(() => (view?.referee_ship_states ?? [])
    .filter((ship) => ship.side === side)
    .map((ship) => ({ id: String(ship.id), name: String(ship.name ?? ship.id) })), [view, side]);
  const [groups, setGroups] = useState<GroupDraft[]>([]);
  useEffect(() => {
    setGroups((view?.formations ?? [])
      .filter((formation) => formation.side === side)
      .map((formation, index) => {
        const ids = formation.ship_ids ?? [];
        const flagship = formation.flagship_id ?? ids[0] ?? "";
        const reserve = formation.reserve_flagship_id ?? ids.find((id) => id !== flagship) ?? "";
        return {
          formation_id: formation.formation_id,
          name: formation.name || `第 ${index + 1} 编队`,
          ship_ids: ids,
          flagship_id: flagship,
          reserve_flagship_id: reserve,
          spacing: formation.spacing,
          turn_mode: formation.turn_mode,
        };
      }));
  }, [view?.version, view?.phase, side]);
  const update = (index: number, patch: Partial<GroupDraft>) => setGroups((current) => current.map((group, row) => row === index ? { ...group, ...patch } : group));
  const allIds = new Set(groups.flatMap((group) => group.ship_ids));
  const canSave = Boolean(view?.realistic_command && groups.length > 0 && groups.every((group) =>
    group.ship_ids.length >= 2 && group.ship_ids.includes(group.flagship_id)
    && group.ship_ids.includes(group.reserve_flagship_id) && group.flagship_id !== group.reserve_flagship_id,
  ) && ships.every((ship) => allIds.has(ship.id)) && allIds.size === ships.length);

  if (!view?.realistic_command) return null;
  return <section className="platform-panel formation-composition-editor" aria-label="裁判编队成员管理">
    <div className="panel-heading">
      <div><span className="platform-kicker">FORMATION COMMAND</span><h2>编队组成</h2></div>
      <select aria-label="编辑阵营" value={side} onChange={(event) => setSide(event.target.value as "axis" | "allies")}>
        <option value="axis">轴心</option><option value="allies">同盟</option>
      </select>
    </div>
    <p className="field-hint">每次保存整侧编组。领舰席位提交全队行动；调整成员后，当前阶段订单会清空并按新领舰重新收单。</p>
    <div className="formation-composition-list">
      {groups.map((group, index) => <article className="formation-composition-row" key={`${side}-${group.formation_id}`}>
        <div className="formation-composition-heading">
          <label>编队名称<input value={group.name} onChange={(event) => update(index, { name: event.target.value })} /></label>
          <label>编号<input value={group.formation_id} onChange={(event) => update(index, { formation_id: event.target.value })} /></label>
          <button type="button" className="quiet" onClick={() => setGroups((current) => current.filter((_item, row) => row !== index))} aria-label={`删除${group.name}`}>删除</button>
        </div>
        <label>成员舰船
          <select multiple size={Math.min(5, Math.max(2, ships.length))} value={group.ship_ids} onChange={(event) => {
            const selected = Array.from(event.currentTarget.selectedOptions, (option) => option.value);
            const flagship = selected.includes(group.flagship_id) ? group.flagship_id : selected[0] ?? "";
            const reserve = selected.includes(group.reserve_flagship_id) && group.reserve_flagship_id !== flagship
              ? group.reserve_flagship_id : selected.find((id) => id !== flagship) ?? "";
            update(index, { ship_ids: selected, flagship_id: flagship, reserve_flagship_id: reserve });
          }}>
            {ships.map((ship) => <option key={ship.id} value={ship.id}>{ship.name} · {ship.id}{allIds.has(ship.id) && !group.ship_ids.includes(ship.id) ? " · 已分组" : ""}</option>)}
          </select>
        </label>
        <div className="action-grid">
          <label>领舰<select value={group.flagship_id} onChange={(event) => update(index, { flagship_id: event.target.value, reserve_flagship_id: group.reserve_flagship_id === event.target.value ? group.ship_ids.find((id) => id !== event.target.value) ?? "" : group.reserve_flagship_id })}>
            {group.ship_ids.map((id) => <option key={id} value={id}>{ships.find((ship) => ship.id === id)?.name ?? id}</option>)}
          </select></label>
          <label>预备领舰<select value={group.reserve_flagship_id} onChange={(event) => update(index, { reserve_flagship_id: event.target.value })}>
            {group.ship_ids.filter((id) => id !== group.flagship_id).map((id) => <option key={id} value={id}>{ships.find((ship) => ship.id === id)?.name ?? id}</option>)}
          </select></label>
          <label>转向模式<select value={group.turn_mode} onChange={(event) => update(index, { turn_mode: event.target.value as GroupDraft["turn_mode"] })}>
            <option value="column_follow">鱼贯转向</option><option value="synchronized">同步转向</option>
          </select></label>
          <label>舰间距<select value={group.spacing} onChange={(event) => update(index, { spacing: Number(event.target.value) as 1 | 2 })}>
            <option value={1}>1 格</option><option value={2}>2 格</option>
          </select></label>
        </div>
      </article>)}
      {!groups.length && <p className="empty-plan">该阵营暂无编队。</p>}
    </div>
    <div className="action-row">
      <button type="button" className="quiet" disabled={disabled || ships.length < 2} onClick={() => {
        const available = ships.filter((ship) => !allIds.has(ship.id)).map((ship) => ship.id);
        if (available.length < 2) return;
        const flagship = available[0];
        setGroups((current) => [...current, {
          formation_id: `${side}-formation-${current.length + 1}`,
          name: `第 ${current.length + 1} 编队`, ship_ids: available,
          flagship_id: flagship, reserve_flagship_id: available[1], spacing: 1, turn_mode: "column_follow",
        }]);
      }}>＋新增编队</button>
      <button type="button" disabled={disabled || !canSave} onClick={() => onSave(side, groups)}>保存编队组成</button>
    </div>
  </section>;
}
