"""装配谱系核心服务：登记装卸、追溯召回、冻结/补录边界、隔离与结案。

时间语义（秒级 Unix 时间戳，可含小数）：
- installed_at / removed_at 是装卸时刻；同一 child 在 [installed_at, removed_at)
  区间内视为装在该 parent 上。removed_at 为空即仍在装。
- 追溯从故障起点 started_at 出发：沿「安装区间与 [started_at, +∞) 有交集」
  的装配关系向上追到整机。已拆下的关系同样留在路径中，不漏列。
- 召回在发起时冻结 frozen_at 作为完整关系边界：边界之后登记的关系不改变
  本次召回快照，但会自动补入隔离名单（边界冻结、名单增长）。
- 结案后补录与本召回相关的旧关系，召回自动重开并重新核验。

并发与幂等：
- 登记装卸用 (parent, child, installed_at) 唯一约束 + 打开态部分唯一索引
  防止重复事件与同件同期多处安装；冲突返回 409，不静默合并。
- 写接口要求 Idempotency-Key：同号同内容返回首次响应，同号换内容 409。
- 召回 lock_version 作为乐观锁，结案时可校验。
"""
import time
from collections import deque

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.models import (Part, Install, Recall, RecallPath, RecallHit,
                        IsolationEntry, IdempotencyKey)
from src.utils.serialization import checksum

EPS = 1e-6
INF = float('inf')
KIND_ORDER = {'component': 0, 'assembly': 1, 'machine': 2}
MAX_DEPTH = 16


# ---------------------------------------------------------------- 幂等
def idempotent(db, scope, key, payload, action):
    """执行 action() 并缓存响应。同号同内容返回缓存，同号不同内容 409。"""
    key = (key or '').strip()
    if not key:
        raise HTTPException(422, '写操作必须提供 Idempotency-Key 请求头')
    if len(key) > 120:
        raise HTTPException(422, 'Idempotency-Key 过长')
    fingerprint = checksum(payload)
    existing = db.scalar(select(IdempotencyKey).where(
        IdempotencyKey.scope == scope, IdempotencyKey.key == key))
    if existing:
        if existing.fingerprint != fingerprint:
            raise HTTPException(409, '同一幂等号已被不同内容使用，拒绝覆盖')
        return existing.response, True
    response = action()
    try:
        db.add(IdempotencyKey(scope=scope, key=key, fingerprint=fingerprint,
                              response=response, at=time.time()))
        db.flush()
    except IntegrityError:
        # 并发同号请求：对方已落库。回滚本次，返回对方结果（内容一致才算幂等）。
        db.rollback()
        existing = db.scalar(select(IdempotencyKey).where(
            IdempotencyKey.scope == scope, IdempotencyKey.key == key))
        if existing and existing.fingerprint == fingerprint:
            return existing.response, True
        raise HTTPException(409, '并发请求冲突，请重试') from None
    return response, False


# ---------------------------------------------------------------- 查询辅助
def part_or_404(db, part_id):
    part = db.get(Part, part_id)
    if not part:
        raise HTTPException(404, '部件不存在')
    return part


def recall_or_404(db, recall_id):
    recall = db.get(Recall, recall_id)
    if not recall:
        raise HTTPException(404, '召回事件不存在')
    return recall


def installs_of_child(db, child_id):
    return list(db.scalars(select(Install).where(Install.child_id == child_id)
                           .order_by(Install.installed_at, Install.id)))


def lock_part(db, part_id):
    """PostgreSQL 下按部件加行锁，串行化同一部件的并发装卸（SQLite 忽略）。"""
    if db.bind.dialect.name == 'postgresql':
        db.execute(select(Part.id).where(Part.id == part_id).with_for_update())
    return db.get(Part, part_id)


def _overlaps(a_start, a_end, b_start, b_end):
    a_end = INF if a_end is None else a_end
    b_end = INF if b_end is None else b_end
    return a_start < b_end - EPS and b_start < a_end - EPS


def _upstream_nodes(db, node):
    for up in db.scalars(select(Install).where(Install.child_id == node)):
        yield up


def cycle_reason(db, parent_id, child_id):
    """若把 child 装到 parent 会形成循环，返回循环说明；否则 None。"""
    if parent_id == child_id:
        return '部件不能装配到自身'
    seen = {parent_id}
    queue = deque([parent_id])
    while queue:
        node = queue.popleft()
        if node == child_id:
            return '该装配会形成循环总成谱系'
        for up in _upstream_nodes(db, node):
            if up.parent_id not in seen:
                seen.add(up.parent_id)
                queue.append(up.parent_id)
    return None


# ---------------------------------------------------------------- 登记装卸
def register_install(db, parent_id, child_id, installed_at, removed_at,
                     token, note, actor):
    # 按 id 升序加锁，避免并发装卸互相等待造成死锁。
    for part_id in sorted({parent_id, child_id}):
        if lock_part(db, part_id) is None:
            raise HTTPException(404, '部件不存在')
    parent = db.get(Part, parent_id)
    child = db.get(Part, child_id)
    now = time.time()
    if installed_at > now + 1:
        raise HTTPException(422, '安装时刻不能晚于当前时间')
    if removed_at is not None and removed_at <= installed_at + EPS:
        raise HTTPException(422, '拆下时刻必须晚于安装时刻')
    if removed_at is not None and removed_at > now + 1:
        raise HTTPException(422, '拆下时刻不能晚于当前时间')
    if KIND_ORDER[child.kind] > KIND_ORDER[parent.kind]:
        raise HTTPException(422, '装配方向不符：上级总成不能装入小件')
    reason = cycle_reason(db, parent_id, child_id)
    if reason:
        raise HTTPException(409, reason)
    # 同件同期多处安装：安装区间重叠即拒绝，拆下关系也不得被覆盖。
    for existing in installs_of_child(db, child_id):
        if _overlaps(installed_at, removed_at,
                     existing.installed_at, existing.removed_at):
            end = (f'{existing.removed_at:.3f} 止' if existing.removed_at is not None
                   else '仍在装')
            raise HTTPException(
                409, '同件同期不允许多处安装：该部件已有重叠安装 '
                     f'#{existing.id}（{existing.installed_at:.3f} 起，{end}）')
    install = Install(parent_id=parent_id, child_id=child_id,
                      installed_at=installed_at, removed_at=removed_at,
                      client_token=(token or '')[:120], note=(note or '')[:500],
                      created_by=actor, created_at=now)
    db.add(install)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '并发登记冲突：同一部件同时刻只能有一处安装（或事件重复）') from None
    after_install(db, install)
    return install


def remove_install(db, install_id, removed_at, actor, note=''):
    install = db.get(Install, install_id)
    if not install:
        raise HTTPException(404, '装卸记录不存在')
    if install.removed_at is not None:
        raise HTTPException(409, '该安装已登记拆下，旧关系保留不可覆盖')
    now = time.time()
    if removed_at <= install.installed_at + EPS:
        raise HTTPException(422, '拆下时刻必须晚于安装时刻')
    if removed_at > now + 1:
        raise HTTPException(422, '拆下时刻不能晚于当前时间')
    install.removed_at = removed_at
    if note:
        install.note = (install.note + ' / ' + note)[:500]
    db.flush()
    return install


# ---------------------------------------------------------------- 关系传播
def _relation_in_recall_window(install, recall):
    """安装区间是否与召回窗口 [started_at, +∞) 有交集。"""
    return install.removed_at is None or install.removed_at >= recall.started_at - EPS


def _is_on_recall_chain(db, recall, child_id):
    """child 与召回目标是否处于同一装配链（任一方向可达）。"""
    target = recall.part_id
    if child_id == target:
        return True
    for root, goal in ((child_id, target), (target, child_id)):
        seen, queue = {root}, deque([root])
        while queue:
            cur = queue.popleft()
            if cur == goal:
                return True
            for up in _upstream_nodes(db, cur):
                if up.parent_id not in seen:
                    seen.add(up.parent_id)
                    queue.append(up.parent_id)
    return False


def reachable_machines(db, start_node, start_time, max_depth=MAX_DEPTH,
                       until=None, only_coexisting=False):
    """从 start_node 沿安装区间与 [start_time, +∞) 重叠的关系找到整机。

    only_coexisting=True 时再要求每条关系与新事件时段共存（用于判断
    新关系是否为某整机带来新证据）。
    """
    machines = {}
    queue = deque([(start_node, 0, (start_node,))])
    while queue:
        node, depth, path_nodes = queue.popleft()
        if depth >= max_depth:
            continue
        for up in _upstream_nodes(db, node):
            if up.removed_at is not None and up.removed_at < start_time - EPS:
                continue
            if only_coexisting:
                end = INF if up.removed_at is None else up.removed_at
                new_end = INF if until is None else until
                if not (up.installed_at < new_end - EPS and start_time < end - EPS):
                    continue
            if up.parent_id in path_nodes:
                continue
            part = db.get(Part, up.parent_id)
            if part is None:
                continue
            if part.kind == 'machine':
                machines.setdefault(part.id, [])
                continue
            queue.append((up.parent_id, depth + 1, path_nodes + (up.parent_id,)))
    return machines


def after_install(db, install):
    """新登记关系后维护召回边界与隔离名单。

    - 冻结后（或补录后）出现与本召回相关的新关联：自动补入隔离名单。
    - 召回已结案又被补录相关关系：自动重开，等待重新核验。
    """
    now = time.time()
    for recall in db.scalars(select(Recall)):
        if not _relation_in_recall_window(install, recall):
            continue
        if not _is_on_recall_chain(db, recall, install.child_id):
            continue
        if recall.status == 'tracing':
            recall.status = 'open'
        if recall.status == 'closed':
            reopen_recall(db, recall.id, '结案后补录旧关系', 'system')
        reachable = reachable_machines(db, recall.part_id, recall.started_at)
        coexisting = set(reachable_machines(
            db, recall.part_id, install.installed_at,
            until=install.removed_at, only_coexisting=True))
        for machine_id in reachable:
            entry = db.scalar(select(IsolationEntry).where(
                IsolationEntry.recall_id == recall.id,
                IsolationEntry.machine_id == machine_id))
            if entry is None:
                db.add(IsolationEntry(
                    recall_id=recall.id, machine_id=machine_id, status='isolated',
                    reason='late_relation', isolated_at=now, voucher_no='',
                    voucher_note='召回冻结后出现新关联，自动补入隔离名单',
                    released_by=''))
            elif entry.status == 'released' and machine_id in coexisting:
                # 新关系与该整机的装配时段共存：已有更换凭证不再充分，重新隔离。
                entry.status = 'isolated'
                entry.reason = 'late_relation'
                entry.isolated_at = now
                entry.released_at = None
                entry.voucher_no = ''
                entry.voucher_note = '召回冻结后出现同期新关联，隔离重新生效'
                entry.released_by = ''
    db.flush()


# ---------------------------------------------------------------- 追溯
def trace(db, recall_id, persist=True):
    """从故障起点沿「起点后时段重叠」的装配路径追到整机。

    已拆下关系同样参与，不漏列。路径快照按 segment 唯一键写入，
    重复调用等于中断恢复，不会重复登记。
    """
    recall = recall_or_404(db, recall_id)
    start = recall.started_at
    hits = {}
    segments = []
    late = []
    queue = deque([(recall.part_id, [], 0, (recall.part_id,))])
    while queue:
        node, path, depth, path_nodes = queue.popleft()
        part = db.get(Part, node)
        if part is None:
            continue
        if part.kind == 'machine':
            hits.setdefault(node, list(path))
            continue
        if depth >= MAX_DEPTH:
            continue
        for up in installs_of_child(db, node):
            # 与 [start, +∞) 时段重叠：拆下时刻不早于起点。
            if up.removed_at is not None and up.removed_at < start - EPS:
                continue
            if up.parent_id in path_nodes:
                continue
            if up.installed_at >= recall.frozen_at - EPS:
                # 冻结边界之后的关联不进本次召回快照，交给隔离补录。
                late.append({'install_id': up.id, 'child_id': node,
                             'parent_id': up.parent_id, 'depth': depth + 1})
                continue
            segments.append({'install_id': up.id, 'child_id': node,
                             'parent_id': up.parent_id, 'depth': depth + 1,
                             'relation': 'removed' if up.removed_at is not None else 'current'})
            queue.append((up.parent_id, path + [up.id], depth + 1,
                          path_nodes + (up.parent_id,)))
    if persist:
        refresh_recall_snapshot(db, recall, segments, hits)
    return {'recall': recall, 'segments': segments, 'late_relations': late,
            'machines': sorted(hits), 'paths': hits}


def refresh_recall_snapshot(db, recall, segments, hits):
    """按 segment 唯一键覆盖写入路径与命中；已有隔离记录不重复创建。"""
    now = time.time()
    existing = {s.segment: s for s in db.scalars(
        select(RecallPath).where(RecallPath.recall_id == recall.id))}
    for segment in segments:
        row = existing.get(segment['install_id'])
        if row is None:
            db.add(RecallPath(recall_id=recall.id, segment=segment['install_id'],
                              install_id=segment['install_id'],
                              child_id=segment['child_id'],
                              parent_id=segment['parent_id'],
                              depth=segment['depth'],
                              relation=segment['relation'], computed_at=now))
        else:
            row.child_id = segment['child_id']
            row.parent_id = segment['parent_id']
            row.depth = segment['depth']
            row.relation = segment['relation']
            row.computed_at = now
    for machine_id, path in hits.items():
        hit = db.scalar(select(RecallHit).where(
            RecallHit.recall_id == recall.id, RecallHit.machine_id == machine_id))
        chain = []
        for install_id in path:
            inst = db.get(Install, install_id)
            if inst:
                chain.append({'install_id': inst.id, 'parent_id': inst.parent_id,
                              'child_id': inst.child_id,
                              'installed_at': inst.installed_at,
                              'removed_at': inst.removed_at})
        via_removed = any(c['removed_at'] is not None for c in chain)
        if hit is None:
            db.add(RecallHit(recall_id=recall.id, machine_id=machine_id,
                             path=chain, via_removed=via_removed, discovered_at=now))
        else:
            hit.path = chain
            hit.via_removed = via_removed
        entry = db.scalar(select(IsolationEntry).where(
            IsolationEntry.recall_id == recall.id,
            IsolationEntry.machine_id == machine_id))
        if entry is None:
            db.add(IsolationEntry(recall_id=recall.id, machine_id=machine_id,
                                  status='isolated', reason='hit',
                                  isolated_at=now, voucher_no='', voucher_note='',
                                  released_by=''))
    db.flush()


def trace_resume(db, recall_id):
    """中断后恢复：按 segment 唯一键续写，不重复登记。"""
    return trace(db, recall_id, persist=True)


# ---------------------------------------------------------------- 召回与冻结
def open_recall(db, code, part_id, started_at, actor, frozen_at=None):
    part = part_or_404(db, part_id)
    if part.kind != 'component':
        raise HTTPException(422, '召回对象必须是小件（component）')
    now = time.time()
    if started_at > now + 1:
        raise HTTPException(422, '故障起点不能晚于当前时间')
    if db.scalar(select(Recall).where(Recall.code == code)):
        raise HTTPException(409, '召回编号已存在')
    frozen = now if frozen_at is None else min(frozen_at, now)
    if frozen < started_at:
        frozen = started_at
    recall = Recall(code=code, part_id=part_id, started_at=started_at,
                    frozen_at=frozen, status='tracing', created_by=actor,
                    created_at=now)
    db.add(recall)
    db.flush()
    return recall


def freeze_boundary(db, recall_id):
    """再次冻结完整关系边界：此后登记的关系只进隔离名单，不改召回快照。"""
    recall = recall_or_404(db, recall_id)
    if recall.status == 'closed':
        raise HTTPException(409, '已结案召回不能重新冻结边界')
    recall.frozen_at = max(time.time(), recall.frozen_at)
    recall.lock_version += 1
    db.flush()
    return recall


# ---------------------------------------------------------------- 隔离与处置
def release_isolation(db, entry_id, voucher_no, voucher_note, actor):
    entry = db.get(IsolationEntry, entry_id)
    if not entry:
        raise HTTPException(404, '隔离记录不存在')
    if entry.status == 'released':
        raise HTTPException(409, '该整机已解除隔离')
    voucher_no = (voucher_no or '').strip()
    if len(voucher_no) < 3:
        raise HTTPException(422, '解除隔离必须登记更换凭证编号（至少 3 个字符）')
    entry.status = 'released'
    entry.voucher_no = voucher_no[:120]
    entry.voucher_note = (voucher_note or '')[:500]
    entry.released_at = time.time()
    entry.released_by = actor
    db.flush()
    return entry


def reopen_isolation(db, entry_id, reason, actor):
    entry = db.get(IsolationEntry, entry_id)
    if not entry:
        raise HTTPException(404, '隔离记录不存在')
    if entry.status != 'released':
        raise HTTPException(409, '该整机当前已在隔离中')
    old = entry.voucher_no
    entry.status = 'isolated'
    entry.reason = (reason or 'manual_reopen')[:80]
    entry.isolated_at = time.time()
    entry.released_at = None
    entry.voucher_no = ''
    entry.voucher_note = (entry.voucher_note + f' / 重新隔离，原凭证 {old}').strip(' /')[:500]
    entry.released_by = ''
    db.flush()
    return entry


# ---------------------------------------------------------------- 结案
def pending_count(db, recall_id):
    return len(list(db.scalars(select(IsolationEntry.id).where(
        IsolationEntry.recall_id == recall_id,
        IsolationEntry.status == 'isolated'))))


def close_recall(db, recall_id, note, actor, expected_version=None):
    recall = recall_or_404(db, recall_id)
    if recall.status == 'closed':
        raise HTTPException(409, '该召回已结案')
    if expected_version is not None and recall.lock_version != expected_version:
        raise HTTPException(409, '召回状态已被其他操作更新，请刷新后重试')
    pending = pending_count(db, recall.id)
    if pending:
        raise HTTPException(409, f'仍有 {pending} 台整机待处置，待处理未清零拒绝结案')
    recall.status = 'closed'
    recall.closed_at = time.time()
    recall.closed_by = actor
    recall.lock_version += 1
    db.flush()
    return recall


def reopen_recall(db, recall_id, reason, actor):
    recall = recall_or_404(db, recall_id)
    if recall.status != 'closed':
        raise HTTPException(409, '仅已结案召回可重开')
    recall.status = 'open'
    recall.closed_at = None
    recall.reopen_count += 1
    recall.lock_version += 1
    db.flush()
    return recall


def attach_old_relation(db, recall_id, install_id, actor):
    """结案后补录旧关系：重开召回并重新核验命中与隔离。"""
    recall = recall_or_404(db, recall_id)
    install = db.get(Install, install_id)
    if not install:
        raise HTTPException(404, '装卸记录不存在')
    if not _relation_in_recall_window(install, recall):
        raise HTTPException(422, '该装卸关系与召回时段不重叠，无法列入追查')
    if not _is_on_recall_chain(db, recall, install.child_id):
        raise HTTPException(422, '该装卸关系与召回部件不在同一装配链，无法列入追查')
    reopened = False
    if recall.status == 'closed':
        reopen_recall(db, recall.id, '结案后补录旧关系', actor)
        reopened = True
    if recall.status == 'tracing':
        recall.status = 'open'
    after_install(db, install)
    trace(db, recall.id, persist=True)
    return recall, reopened


# ---------------------------------------------------------------- 估计隔离
def isolated_machine_ids(db):
    """当前处于隔离中的整机部件 id。"""
    return {e.machine_id for e in db.scalars(select(IsolationEntry).where(
        IsolationEntry.status == 'isolated'))}


# ---------------------------------------------------------------- 估计隔离
def blocked_asset_serials(db):
    """当前隔离整机上仍在装的部件序列号；这些样本不进入新估计。"""
    isolated = isolated_machine_ids(db)
    if not isolated:
        return set()
    blocked = set()
    queue = deque(isolated)
    seen = set(isolated)
    while queue:
        node = queue.popleft()
        part = db.get(Part, node)
        if part is None:
            continue
        if part.kind == 'component':
            blocked.add(part.serial)
        for down in db.scalars(select(Install).where(
                Install.parent_id == node, Install.removed_at.is_(None))):
            if down.child_id not in seen:
                seen.add(down.child_id)
                queue.append(down.child_id)
    return blocked
