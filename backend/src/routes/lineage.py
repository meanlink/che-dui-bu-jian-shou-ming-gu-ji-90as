"""装配谱系与召回追溯接口。

写操作要求 Idempotency-Key 头（同号同内容返回首结果，同号换内容 409）；
读操作开放给登录用户；登记与处置要求 admin/analyst。
"""
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select

from src.config.database import get_db
from src.middlewares.auth import current_user, can_write, admin_only
from src.models import (Part, Install, Recall, RecallPath, RecallHit,
                        IsolationEntry)
from src.schemas.lineage import (PartInput, InstallInput, RemoveInput, RecallInput,
                                 CloseInput, ReopenInput, ReleaseInput,
                                 LateRelationInput)
from src.services import lineage
from src.services.audit import record
import time

router = APIRouter(prefix='/lineage')


def idem_key(idempotency_key: str | None = Header(default=None,
                                                alias='Idempotency-Key')):
    return idempotency_key


# ---------------------------------------------------------------- 序列化
def part_dict(p):
    return {'id': p.id, 'serial': p.serial, 'name': p.name, 'kind': p.kind,
            'model': p.model, 'parent_hint': p.parent_hint}


def install_dict(i):
    return {'id': i.id, 'parent_id': i.parent_id, 'child_id': i.child_id,
            'installed_at': i.installed_at, 'removed_at': i.removed_at,
            'open': i.removed_at is None, 'note': i.note,
            'created_by': i.created_by, 'created_at': i.created_at}


def entry_dict(e, machines=None):
    return {'id': e.id, 'recall_id': e.recall_id, 'machine_id': e.machine_id,
            'machine': machines.get(e.machine_id) if machines else None,
            'status': e.status, 'reason': e.reason, 'isolated_at': e.isolated_at,
            'released_at': e.released_at, 'voucher_no': e.voucher_no,
            'voucher_note': e.voucher_note, 'released_by': e.released_by}


def recall_dict(db, r, detailed=False):
    hits = list(db.scalars(select(RecallHit).where(RecallHit.recall_id == r.id)
                           .order_by(RecallHit.machine_id)))
    entries = list(db.scalars(select(IsolationEntry)
                              .where(IsolationEntry.recall_id == r.id)
                              .order_by(IsolationEntry.id)))
    machines = {p.id: part_dict(p) for p in db.scalars(
        select(Part).where(Part.id.in_([e.machine_id for e in entries] or [-1])))}
    pending = sum(1 for e in entries if e.status == 'isolated')
    target = db.get(Part, r.part_id)
    data = {'id': r.id, 'code': r.code, 'part_id': r.part_id,
            'part': part_dict(target) if target else None,
            'started_at': r.started_at, 'frozen_at': r.frozen_at,
            'status': r.status, 'created_by': r.created_by,
            'created_at': r.created_at, 'closed_at': r.closed_at,
            'closed_by': r.closed_by, 'reopen_count': r.reopen_count,
            'version': r.lock_version,
            'hits': [{'machine_id': h.machine_id,
                      'machine': machines.get(h.machine_id),
                      'via_removed': h.via_removed, 'path': h.path,
                      'discovered_at': h.discovered_at} for h in hits],
            'isolation': [entry_dict(e, machines) for e in entries],
            'pending': pending, 'can_close': pending == 0 and r.status != 'closed'}
    if detailed:
        paths = list(db.scalars(select(RecallPath)
                                .where(RecallPath.recall_id == r.id)
                                .order_by(RecallPath.segment)))
        data['segments'] = [{'install_id': p.install_id, 'child_id': p.child_id,
                             'parent_id': p.parent_id, 'depth': p.depth,
                             'relation': p.relation, 'computed_at': p.computed_at}
                            for p in paths]
    return data


# ---------------------------------------------------------------- 部件
@router.get('/parts')
def parts(db=Depends(get_db), u=Depends(current_user), kind: str | None = None):
    q = select(Part).order_by(Part.id)
    if kind:
        q = q.where(Part.kind == kind)
    return [part_dict(p) for p in db.scalars(q)]


@router.post('/parts', status_code=201)
def create_part(data: PartInput, key: str | None = Depends(idem_key),
                db=Depends(get_db), u=Depends(can_write)):
    def action():
        if db.scalar(select(Part).where(Part.serial == data.serial.strip())):
            raise HTTPException(409, '部件序列号已登记')
        p = Part(serial=data.serial.strip(), name=data.name.strip(),
                 kind=data.kind, model=data.model.strip(),
                 parent_hint=data.parent_hint, created_at=time.time())
        db.add(p)
        db.flush()
        record(db, u.email, 'part.create', {'id': p.id, 'serial': p.serial})
        return part_dict(p)
    response, _ = lineage.idempotent(db, 'part.create', key, data.model_dump(), action)
    db.commit()
    return response


# ---------------------------------------------------------------- 装卸
@router.get('/installs')
def installs(db=Depends(get_db), u=Depends(current_user),
             child_id: int | None = None, parent_id: int | None = None,
             open_only: bool = False):
    q = select(Install).order_by(Install.installed_at.desc(), Install.id.desc())
    if child_id:
        q = q.where(Install.child_id == child_id)
    if parent_id:
        q = q.where(Install.parent_id == parent_id)
    if open_only:
        q = q.where(Install.removed_at.is_(None))
    return [install_dict(i) for i in db.scalars(q.limit(500))]


@router.post('/installs', status_code=201)
def create_install(data: InstallInput, key: str | None = Depends(idem_key),
                   db=Depends(get_db), u=Depends(can_write)):
    payload = data.model_dump()

    def action():
        i = lineage.register_install(db, data.parent_id, data.child_id,
                                     data.installed_at, data.removed_at,
                                     key, data.note, u.email)
        record(db, u.email, 'install.create',
               {'id': i.id, 'parent': i.parent_id, 'child': i.child_id})
        return install_dict(i)
    try:
        response, _ = lineage.idempotent(db, 'install.create', key, payload, action)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return response


@router.post('/installs/{install_id}/remove')
def remove_install(install_id: int, data: RemoveInput,
                   key: str | None = Depends(idem_key),
                   db=Depends(get_db), u=Depends(can_write)):
    def action():
        i = lineage.remove_install(db, install_id, data.removed_at, u.email, data.note)
        record(db, u.email, 'install.remove', {'id': i.id, 'removed_at': i.removed_at})
        return install_dict(i)
    response, _ = lineage.idempotent(db, 'install.remove', key,
                                     {'install_id': install_id, **data.model_dump()}, action)
    db.commit()
    return response


@router.get('/installs/{install_id}/tree')
def tree(install_id: int, db=Depends(get_db), u=Depends(current_user)):
    """沿「该次安装发生时仍在装」的上游关系还原装配链。"""
    root = db.get(Install, install_id)
    if not root:
        raise HTTPException(404, '装卸记录不存在')
    chain, cur, seen = [], root, set()
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        parent = db.get(Part, cur.parent_id)
        child = db.get(Part, cur.child_id)
        chain.append({'install': install_dict(cur),
                      'parent': part_dict(parent) if parent else None,
                      'child': part_dict(child) if child else None})
        moment = cur.installed_at
        cur = next((up for up in reversed(lineage.installs_of_child(db, cur.parent_id))
                    if up.installed_at <= moment + 1e-6
                    and (up.removed_at is None or up.removed_at >= moment - 1e-6)), None)
    return {'ancestor_chain': chain}


# ---------------------------------------------------------------- 召回
@router.get('/recalls')
def recalls(db=Depends(get_db), u=Depends(current_user)):
    return [recall_dict(db, r) for r in db.scalars(select(Recall).order_by(Recall.id.desc()))]


@router.post('/recalls', status_code=201)
def create_recall(data: RecallInput, key: str | None = Depends(idem_key),
                  db=Depends(get_db), u=Depends(can_write)):
    def action():
        r = lineage.open_recall(db, data.code.strip(), data.part_id,
                                data.started_at, u.email)
        lineage.trace(db, r.id, persist=True)
        if r.status == 'tracing':
            r.status = 'open'
        record(db, u.email, 'recall.open', {'id': r.id, 'code': r.code})
        return recall_dict(db, r, detailed=True)
    try:
        response, _ = lineage.idempotent(db, 'recall.open', key, data.model_dump(), action)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return response


@router.get('/recalls/{recall_id}')
def recall_detail(recall_id: int, db=Depends(get_db), u=Depends(current_user)):
    return recall_dict(db, lineage.recall_or_404(db, recall_id), detailed=True)


@router.post('/recalls/{recall_id}/trace')
def recall_trace(recall_id: int, key: str | None = Depends(idem_key),
                 db=Depends(get_db), u=Depends(can_write)):
    """追溯（可重复调用=中断恢复）；同一幂等号不重复登记路径。"""
    def action():
        lineage.recall_or_404(db, recall_id)
        result = lineage.trace_resume(db, recall_id)
        recall = result['recall']
        if recall.status == 'tracing':
            recall.status = 'open'
        record(db, u.email, 'recall.trace',
               {'id': recall_id, 'machines': len(result['machines'])})
        return recall_dict(db, lineage.recall_or_404(db, recall_id), detailed=True)
    response, _ = lineage.idempotent(db, 'recall.trace', key, {'recall_id': recall_id}, action)
    db.commit()
    return response


@router.post('/recalls/{recall_id}/freeze')
def recall_freeze(recall_id: int, key: str | None = Depends(idem_key),
                  db=Depends(get_db), u=Depends(admin_only)):
    def action():
        r = lineage.freeze_boundary(db, recall_id)
        lineage.trace_resume(db, recall_id)
        record(db, u.email, 'recall.freeze', {'id': recall_id, 'frozen_at': r.frozen_at})
        return recall_dict(db, r, detailed=True)
    response, _ = lineage.idempotent(db, 'recall.freeze', key, {'recall_id': recall_id}, action)
    db.commit()
    return response


@router.post('/recalls/{recall_id}/late-relation')
def late_relation(recall_id: int, data: LateRelationInput,
                  key: str | None = Depends(idem_key),
                  db=Depends(get_db), u=Depends(admin_only)):
    """结案后补录旧关系：重开召回并重新核验隔离（幂等）。"""
    def action():
        r, reopened = lineage.attach_old_relation(db, recall_id, data.install_id, u.email)
        record(db, u.email, 'recall.late_relation',
               {'id': recall_id, 'install': data.install_id, 'reopened': reopened})
        return recall_dict(db, r, detailed=True)
    response, _ = lineage.idempotent(db, 'recall.late_relation', key, data.model_dump(), action)
    db.commit()
    return response


# ---------------------------------------------------------------- 隔离
@router.get('/isolation')
def isolation_list(db=Depends(get_db), u=Depends(current_user),
                   recall_id: int | None = None, status: str | None = None):
    q = select(IsolationEntry).order_by(IsolationEntry.id.desc())
    if recall_id:
        q = q.where(IsolationEntry.recall_id == recall_id)
    if status:
        q = q.where(IsolationEntry.status == status)
    entries = list(db.scalars(q.limit(500)))
    machines = {p.id: part_dict(p) for p in db.scalars(
        select(Part).where(Part.id.in_([e.machine_id for e in entries] or [-1])))}
    return [entry_dict(e, machines) for e in entries]


@router.post('/isolation/{entry_id}/release')
def release(entry_id: int, data: ReleaseInput, key: str | None = Depends(idem_key),
            db=Depends(get_db), u=Depends(admin_only)):
    def action():
        e = lineage.release_isolation(db, entry_id, data.voucher_no,
                                      data.voucher_note, u.email)
        record(db, u.email, 'isolation.release',
               {'id': entry_id, 'voucher': e.voucher_no})
        return entry_dict(e)
    response, _ = lineage.idempotent(db, 'isolation.release', key,
                                     {'entry_id': entry_id, **data.model_dump()}, action)
    db.commit()
    return response


@router.post('/isolation/{entry_id}/reopen')
def reopen(entry_id: int, data: ReopenInput, key: str | None = Depends(idem_key),
           db=Depends(get_db), u=Depends(admin_only)):
    def action():
        e = lineage.reopen_isolation(db, entry_id, data.reason, u.email)
        record(db, u.email, 'isolation.reopen', {'id': entry_id})
        return entry_dict(e)
    response, _ = lineage.idempotent(db, 'isolation.reopen', key,
                                     {'entry_id': entry_id, **data.model_dump()}, action)
    db.commit()
    return response


# ---------------------------------------------------------------- 结案
@router.post('/recalls/{recall_id}/close')
def close(recall_id: int, data: CloseInput, key: str | None = Depends(idem_key),
          db=Depends(get_db), u=Depends(admin_only)):
    def action():
        r = lineage.close_recall(db, recall_id, data.note, u.email,
                                 data.expected_version)
        record(db, u.email, 'recall.close', {'id': recall_id})
        return recall_dict(db, r)
    response, _ = lineage.idempotent(db, 'recall.close', key,
                                     {'recall_id': recall_id, **data.model_dump()}, action)
    db.commit()
    return response


@router.post('/recalls/{recall_id}/reopen')
def reopen_recall(recall_id: int, data: ReopenInput,
                  key: str | None = Depends(idem_key),
                  db=Depends(get_db), u=Depends(admin_only)):
    def action():
        r = lineage.reopen_recall(db, recall_id, data.reason, u.email)
        lineage.trace_resume(db, recall_id)
        record(db, u.email, 'recall.reopen', {'id': recall_id})
        return recall_dict(db, r)
    response, _ = lineage.idempotent(db, 'recall.reopen', key,
                                     {'recall_id': recall_id, **data.model_dump()}, action)
    db.commit()
    return response


@router.get('/summary')
def summary(db=Depends(get_db), u=Depends(current_user)):
    recalls_ = list(db.scalars(select(Recall)))
    entries = list(db.scalars(select(IsolationEntry)))
    return {'parts': len(list(db.scalars(select(Part.id)))),
            'installs': len(list(db.scalars(select(Install.id)))),
            'open_installs': len(list(db.scalars(
                select(Install.id).where(Install.removed_at.is_(None))))),
            'recalls': len(recalls_),
            'active_recalls': sum(1 for r in recalls_ if r.status != 'closed'),
            'isolated': sum(1 for e in entries if e.status == 'isolated'),
            'released': sum(1 for e in entries if e.status == 'released')}
