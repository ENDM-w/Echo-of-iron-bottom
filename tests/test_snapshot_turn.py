"""快照 turn 列：迁移回填与按回合单行直取。

2026-10-19 复盘：`/view` 每请求把该局全部快照（单局 102 条 / 22MB）反序列化
后再筛「上一回合」，单请求 2.5 秒；16 席位轮询即把整机拖死。修法是给
snapshots 加 turn 列并改成单行查询。这里守住三件事：

1. 旧库（没有 turn 列）打开时自动加列并回填；
2. `snapshot_for_turn` 取到「该回合最新的一条」，缺失回合返回 None；
3. 写入后缓存失效，不会读到旧快照。
"""

from __future__ import annotations

import sqlite3

from iron_bottom_sound import storage
from iron_bottom_sound.engine import IronBottomEngine


def _state(turn: int, game_id: str = "g-legacy"):
    state = IronBottomEngine().reset("IBS-S-03", 904, game_id=game_id)
    state.turn = turn
    return state


def test_migration_adds_turn_and_backfills(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    # 造一个「旧版」快照表：没有 turn 列
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE snapshots (
          game_id TEXT NOT NULL,
          sequence INTEGER NOT NULL,
          state_json TEXT NOT NULL,
          PRIMARY KEY (game_id, sequence)
        );
        """
    )
    state = _state(3)
    connection.execute(
        "INSERT INTO snapshots(game_id, sequence, state_json) VALUES (?, ?, ?)",
        ("g-legacy", 30, state.model_dump_json()),
    )
    connection.commit()
    connection.close()

    repository = storage.GameRepository(path)
    try:
        columns = {row[1] for row in repository.connection.execute("PRAGMA table_info(snapshots)")}
        assert "turn" in columns
        assert repository.connection.execute("SELECT turn FROM snapshots").fetchone()[0] == 3

        entry = repository.snapshot_for_turn("g-legacy", 3)
        assert entry is not None
        assert entry[0] == 30
        assert entry[1].turn == 3
        assert repository.snapshot_for_turn("g-legacy", 4) is None
    finally:
        repository.close()


def test_snapshot_for_turn_prefers_newest_and_invalidates_on_write(tmp_path):
    repository = storage.GameRepository(tmp_path / "saves.sqlite3")
    try:
        first = _state(2)
        repository.save(first)
        newest_sequence = first.events[-1].sequence if first.events else 0

        # 同回合再写一条更晚的快照：必须取到最新那条
        second = _state(2)
        second.events.clear()
        repository.connection.execute(
            "INSERT OR REPLACE INTO snapshots(game_id, sequence, turn, state_json) VALUES (?, ?, ?, ?)",
            ("g-legacy", newest_sequence + 50, 2, second.model_dump_json()),
        )
        repository.connection.commit()
        repository._invalidate_turn_cache()

        entry = repository.snapshot_for_turn("g-legacy", 2)
        assert entry is not None
        assert entry[0] == newest_sequence + 50

        # 缓存命中后写入新快照，必须失效
        assert repository.snapshot_for_turn("g-legacy", 2) is entry
        repository.save(second)
        assert repository._turn_snapshot_cache == {}
    finally:
        repository.close()
