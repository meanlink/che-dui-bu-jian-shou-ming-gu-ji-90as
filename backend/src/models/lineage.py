"""装配谱系、召回与隔离的持久化模型。

设计约束（对应业务规则）：
- 装卸关系一律追加，拆下只写 removed_at，旧关系不删除、不改写。
- 同一部件同一时刻最多一处安装，由数据库部分唯一索引兜底。
- 召回冻结时点后的新关系写入隔离名单，召回关系边界不被后续补录改变。
- 每台整机需一条更换凭证才能解除隔离；待处理未清零不得结案。
"""
from sqlalchemy import (String, Double, ForeignKey, Boolean, Integer, JSON,
                        Index, UniqueConstraint, Text, text)
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base

# 局部唯一索引：removed_at 为空表示仍在装。SQLite / PostgreSQL 均支持。
INSTALL_OPEN_UNIQUE = Index(
    'ux_install_open_slot', 'child_id',
    unique=True, sqlite_where=text('removed_at IS NULL'),
    postgresql_where=text('removed_at IS NULL'))


class Part(Base):
    """总成节点：整机 / 总成 / 小件。parent_hint 仅用于建树前的人工标注。"""
    __tablename__ = 'parts'
    __table_args__ = (UniqueConstraint('serial', name='uq_part_serial'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    serial: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(20))  # machine / assembly / component
    model: Mapped[str] = mapped_column(String(80), default='')
    parent_hint: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[float] = mapped_column(Double)


class Install(Base):
    """一次装卸事件：child 装入 parent 于 installed_at，removed_at 为空表示仍在装。

    relation_key 为整条装配路径上「父件 + 时刻」的稳定指纹，用于并发召回冻结时
    精确锁定关系边界，也用于重试幂等（同一 client_token 不重复登记）。
    """
    __tablename__ = 'installs'
    __table_args__ = (
        UniqueConstraint('parent_id', 'child_id', 'installed_at', name='uq_install_event'),
        INSTALL_OPEN_UNIQUE,
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey('parts.id'))
    child_id: Mapped[int] = mapped_column(ForeignKey('parts.id'))
    installed_at: Mapped[float] = mapped_column(Double)
    removed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    client_token: Mapped[str] = mapped_column(String(120), default='')
    note: Mapped[str] = mapped_column(String(500), default='')
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)


class Recall(Base):
    """召回事件：frozen_at 是关系边界；started_at 是故障起点（追溯时点）。"""
    __tablename__ = 'recalls'
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True)
    part_id: Mapped[int] = mapped_column(ForeignKey('parts.id'))
    started_at: Mapped[float] = mapped_column(Double)   # 故障起点
    frozen_at: Mapped[float] = mapped_column(Double)    # 关系冻结边界
    status: Mapped[str] = mapped_column(String(20), default='tracing')
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)
    closed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    closed_by: Mapped[str] = mapped_column(String(150), default='')
    reopen_count: Mapped[int] = mapped_column(Integer, default=0)
    lock_version: Mapped[int] = mapped_column(Integer, default=0)


class RecallPath(Base):
    """召回路径快照。分批落库便于中断后续跑（segment 唯一）。"""
    __tablename__ = 'recall_paths'
    __table_args__ = (UniqueConstraint('recall_id', 'segment', name='uq_recall_segment'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    recall_id: Mapped[int] = mapped_column(ForeignKey('recalls.id'))
    segment: Mapped[int] = mapped_column(Integer)
    install_id: Mapped[int] = mapped_column(ForeignKey('installs.id'))
    child_id: Mapped[int] = mapped_column(ForeignKey('parts.id'))
    parent_id: Mapped[int] = mapped_column(ForeignKey('parts.id'))
    depth: Mapped[int] = mapped_column(Integer)
    relation: Mapped[str] = mapped_column(String(20))  # current / removed / isolated / hits
    computed_at: Mapped[float] = mapped_column(Double)


class RecallHit(Base):
    """召回路径命中的整机。path 为该整机到小件的装配链，便于复核。"""
    __tablename__ = 'recall_hits'
    __table_args__ = (UniqueConstraint('recall_id', 'machine_id', name='uq_recall_hit'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    recall_id: Mapped[int] = mapped_column(ForeignKey('recalls.id'))
    machine_id: Mapped[int] = mapped_column(ForeignKey('parts.id'))
    path: Mapped[list] = mapped_column(JSON, default=list)
    via_removed: Mapped[bool] = mapped_column(Boolean, default=False)
    discovered_at: Mapped[float] = mapped_column(Double)


class IsolationEntry(Base):
    """隔离名单。一个召回下一台整机一条；release 凭证必填。"""
    __tablename__ = 'isolation_entries'
    __table_args__ = (UniqueConstraint('recall_id', 'machine_id', name='uq_isolation'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    recall_id: Mapped[int] = mapped_column(ForeignKey('recalls.id'))
    machine_id: Mapped[int] = mapped_column(ForeignKey('parts.id'))
    status: Mapped[str] = mapped_column(String(20), default='isolated')
    reason: Mapped[str] = mapped_column(String(80), default='hit')
    isolated_at: Mapped[float] = mapped_column(Double)
    released_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    voucher_no: Mapped[str] = mapped_column(String(120), default='')
    voucher_note: Mapped[str] = mapped_column(String(500), default='')
    released_by: Mapped[str] = mapped_column(String(150), default='')


class IdempotencyKey(Base):
    """写操作幂等：同一 key 同内容返回首次结果，同号不同内容拒绝。"""
    __tablename__ = 'idempotency_keys'
    __table_args__ = (UniqueConstraint('scope', 'key', name='uq_idem_scope_key'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(60))
    key: Mapped[str] = mapped_column(String(120))
    fingerprint: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[float] = mapped_column(Double)
