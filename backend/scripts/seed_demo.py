import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import (User, Dataset, Job, Report, Delivery, Audit,
                        Part, Install, Recall, IsolationEntry)
from src.services.lineage import trace as trace_lineage, register_install
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.utils.security import hash_password
from src.utils.serialization import checksum
def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        seed_lineage(db, admin=db.scalar(select(User).where(User.role=='admin')))
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,Part,Install,Recall,IsolationEntry]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts


LINEAGE_PARTS = [
    ('LC-01', '鹭湾 1 号公交车', 'machine', 'XML6105'),
    ('LC-02', '鹭湾 2 号公交车', 'machine', 'XML6105'),
    ('LC-03', '鹭湾 3 号公交车', 'machine', 'XML6105'),
    ('ASM-8-A', '空气压缩机总成 A', 'assembly', 'AC-8'),
    ('ASM-8-B', '空气压缩机总成 B', 'assembly', 'AC-8'),
    ('ASM-12-A', '空气压缩机总成 A', 'assembly', 'AC-12'),
    ('AC-8-SEAL-014', 'AC-8 密封件 014', 'component', 'AC-8'),
    ('AC-8-SEAL-021', 'AC-8 密封件 021', 'component', 'AC-8'),
    ('AC-12-SEAL-033', 'AC-12 密封件 033', 'component', 'AC-12'),
]


def seed_lineage(db, admin):
    """幂等演示数据：密封件 014 召回、已拆下路径、冻结后补录与隔离处置。"""
    if db.scalar(select(Part).limit(1)):
        return
    now = time.time()
    day = 86400
    parts = {}
    for serial, name, kind, model in LINEAGE_PARTS:
        p = Part(serial=serial, name=name, kind=kind, model=model, created_at=now)
        db.add(p); db.flush(); parts[serial] = p
    events = [
        ('ASM-8-A', 'AC-8-SEAL-014', now - 120 * day, now - 40 * day),
        ('ASM-8-B', 'AC-8-SEAL-014', now - 40 * day, None),
        ('LC-01', 'ASM-8-A', now - 119 * day, now - 75 * day),
        ('ASM-8-A', 'AC-8-SEAL-021', now - 90 * day, None),
        ('LC-02', 'ASM-12-A', now - 60 * day, None),
        ('ASM-12-A', 'AC-12-SEAL-033', now - 25 * day, None),
    ]
    for parent, child, started, ended in events:
        db.add(Install(parent_id=parts[parent].id, child_id=parts[child].id,
                       installed_at=started, removed_at=ended, note='演示装卸记录',
                       created_by='seed', created_at=now))
    db.flush()
    recall = Recall(code='RC-2026-014', part_id=parts['AC-8-SEAL-014'].id,
                    started_at=now - 80 * day, frozen_at=now - 70 * day,
                    status='open', created_by='seed', created_at=now - 70 * day)
    db.add(recall); db.flush()
    trace_lineage(db, recall.id)
    # 冻结后补录的装配关系：不影响召回快照，但自动补入隔离名单
    register_install(db, parts['LC-02'].id, parts['ASM-8-A'].id,
                     now - 50 * day, None, 'seed', '召回冻结后换装总成', 'seed')
    register_install(db, parts['LC-03'].id, parts['ASM-8-B'].id,
                     now - 30 * day, now - 10 * day, 'seed', '召回冻结后换装总成', 'seed')
    # 3 号车已登记更换凭证：可解除隔离；其余两台仍待处置
    entry = db.scalar(select(IsolationEntry).where(
        IsolationEntry.recall_id == recall.id,
        IsolationEntry.machine_id == parts['LC-03'].id))
    if entry:
        entry.status = 'released'; entry.voucher_no = 'RCV-2026-0007'
        entry.voucher_note = '密封件已更换并复检'; entry.released_at = now - day
        entry.released_by = 'seed'
    db.add(Audit(actor='seed', action='recall.demo', detail={'id': recall.id}, at=now))
if __name__=='__main__':seed()
