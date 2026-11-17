"""装配谱系 / 召回 / 隔离的业务规则回归测试。

运行：backend/ 下 `python -m pytest tests/test_lineage.py -q`
使用 SQLite 内存库共享连接，覆盖：
循环与同件同期拒绝、拆换保留旧关系、已拆下仍追溯、冻结边界、
结案后补录重开、每台凭证解除隔离、待处理未清零拒绝结案、幂等与并发。
"""
import os
import sys
import time

os.environ.setdefault('DATABASE_URL', 'sqlite:///./test-lineage.db')
os.environ.setdefault('ARTIFACT_DIR', '/tmp/lineage-test-artifacts')
os.environ.setdefault('SEED_DEMO', 'false')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from src.config.database import Base, engine, Session
from src.main import app
from src.models import Part, Install, Recall, IsolationEntry
from src.services import lineage

DAY = 86400.0


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as db:
        from src.models import User
        from src.utils.security import hash_password
        for role in ('admin', 'analyst', 'viewer'):
            db.add(User(email=role + '@demo.local', name=role, role=role,
                        password=hash_password('Demo123!')))
        db.commit()
    c = TestClient(app)
    token = c.post('/auth/login', json={'email': 'admin@demo.local',
                                        'password': 'Demo123!'}).json()['access_token']
    c.headers.update({'Authorization': 'Bearer ' + token})
    yield c
    Base.metadata.drop_all(engine)


_counter = {'n': 0}


def uniq(prefix='k'):
    _counter['n'] += 1
    return f'{prefix}-{_counter["n"]}-{time.time_ns()}'


def post(c, path, body, key=None):
    return c.post(path, json=body, headers={'Idempotency-Key': key or uniq()})


def make_part(c, serial, kind, name=None):
    r = post(c, '/lineage/parts', {'serial': serial, 'name': name or serial,
                                   'kind': kind})
    assert r.status_code == 201, r.text
    return r.json()


def install(c, parent, child, at, removed=None, key=None):
    return post(c, '/lineage/installs', {'parent_id': parent, 'child_id': child,
                                         'installed_at': at, 'removed_at': removed},
                key=key)


def stopper(c, install_id, at, key=None):
    return post(c, f'/lineage/installs/{install_id}/remove',
                {'removed_at': at}, key=key)


def tree(c):
    """建两台车 + 总成 + 两个密封件的基线部件（关系由用例显式登记）。"""
    now = time.time()
    car1 = make_part(c, uniq('CAR'), 'machine')
    car2 = make_part(c, uniq('CAR'), 'machine')
    asm = make_part(c, uniq('ASM'), 'assembly')
    seal = make_part(c, uniq('SEAL'), 'component')
    other = make_part(c, uniq('SEAL'), 'component')
    return dict(car1=car1, car2=car2, asm=asm, seal=seal, other=other, now=now)


def baseline(c, t, start=-90, removed=None):
    """car1 ← asm ← seal 的两级基线（seal 另由用例登记）。"""
    return install(c, t['car1']['id'], t['asm']['id'],
                   t['now'] + start * DAY, None if removed is None else t['now'] + removed * DAY)


# ------------------------------------------------------------ 谱系约束
def test_cycle_rejected(client):
    a = make_part(client, uniq('ASM'), 'assembly')
    b = make_part(client, uniq('ASM'), 'assembly')
    assert install(client, a['id'], b['id'], time.time() - 200).status_code == 201
    r = install(client, b['id'], a['id'], time.time() - 100)
    assert r.status_code == 409 and '循环' in r.json()['detail']


def test_same_part_same_time_rejected(client):
    t = tree(client)
    now = t['now']
    assert install(client, t['car1']['id'], t['seal']['id'], now - 50 * DAY).status_code == 201
    # 同期装入另一处：拒绝
    r = install(client, t['car2']['id'], t['seal']['id'], now - 40 * DAY)
    assert r.status_code == 409 and '同件同期' in r.json()['detail']
    # 先拆下再重装：允许，旧关系保留
    first = client.get('/lineage/installs', params={'child_id': t['seal']['id']}).json()[0]
    assert stopper(client, first['id'], now - 45 * DAY).status_code == 200
    assert install(client, t['car2']['id'], t['seal']['id'], now - 30 * DAY).status_code == 201
    history = client.get('/lineage/installs', params={'child_id': t['seal']['id']}).json()
    assert len(history) == 2
    assert any(h['removed_at'] is not None for h in history)


def test_removal_cannot_overwrite_history(client):
    t = tree(client)
    now = t['now']
    r = install(client, t['car1']['id'], t['seal']['id'], now - 10 * DAY)
    iid = r.json()['id']
    assert stopper(client, iid, now - 5 * DAY).status_code == 200
    again = stopper(client, iid, now - 2 * DAY)
    assert again.status_code == 409
    row = client.get('/lineage/installs', params={'child_id': t['seal']['id']}).json()[0]
    assert abs(row['removed_at'] - (now - 5 * DAY)) < 1


def test_upstream_cannot_be_installed_into_component(client):
    t = tree(client)
    r = install(client, t['seal']['id'], t['car1']['id'], time.time() - DAY)
    assert r.status_code == 422


# ------------------------------------------------------------ 追溯
def test_trace_includes_removed_relation(client):
    t = tree(client)
    now = t['now']
    assert baseline(client, t, -90).status_code == 201
    r = install(client, t['asm']['id'], t['seal']['id'], now - 80 * DAY, now - 30 * DAY)
    assert r.status_code == 201
    started = now - 60 * DAY          # 起点在该小件已拆下之前
    rec = post(client, '/lineage/recalls', {'code': uniq('RC'), 'part_id': t['seal']['id'],
                                            'started_at': started}).json()
    assert rec['pending'] == 1
    hit = rec['hits'][0]
    assert hit['machine_id'] == t['car1']['id']
    assert hit['via_removed'] is True
    assert len(hit['path']) == 2


def test_trace_skips_relation_closed_before_start(client):
    t = tree(client)
    now = t['now']
    r = install(client, t['asm']['id'], t['seal']['id'], now - 80 * DAY, now - 70 * DAY)
    assert r.status_code == 201
    rec = post(client, '/lineage/recalls', {'code': uniq('RC'), 'part_id': t['seal']['id'],
                                            'started_at': now - 60 * DAY}).json()
    assert rec['pending'] == 0 and rec['hits'] == []


def test_multi_level_path_and_partial_multiple_machines(client):
    t = tree(client)
    now = t['now']
    # 总成 A 先后装在两台车：不同时段，历史各留一条路径
    install(client, t['asm']['id'], t['seal']['id'], now - 100 * DAY, None)
    assert baseline(client, t, -95, -50).status_code == 201
    second = install(client, t['car2']['id'], t['asm']['id'], now - 40 * DAY)
    assert second.status_code == 201
    rec = post(client, '/lineage/recalls', {'code': uniq('RC'), 'part_id': t['seal']['id'],
                                            'started_at': now - 60 * DAY}).json()
    machines = {h['machine_id']: h for h in rec['hits']}
    assert set(machines) == {t['car1']['id'], t['car2']['id']}
    assert machines[t['car1']['id']]['via_removed'] is True
    assert machines[t['car2']['id']]['via_removed'] is False
    # 起点早于换车：两条路径都在，已拆下路径不漏列
    assert len(rec['segments']) == 3


def test_trace_resume_does_not_duplicate(client):
    t = tree(client)
    now = t['now']
    assert baseline(client, t).status_code == 201
    install(client, t['asm']['id'], t['seal']['id'], now - 80 * DAY)
    rec = post(client, '/lineage/recalls', {'code': uniq('RC'), 'part_id': t['seal']['id'],
                                            'started_at': now - 70 * DAY}).json()
    key = uniq('trace')
    a = post(client, f'/lineage/recalls/{rec["id"]}/trace', {}, key=key).json()
    b = post(client, f'/lineage/recalls/{rec["id"]}/trace', {}, key=key).json()
    assert a['pending'] == b['pending'] == 1
    assert len(a['segments']) == len(b['segments']) == 2
    # 不同幂等号重跑：仍不新增隔离条目
    c = post(client, f'/lineage/recalls/{rec["id"]}/trace', {}).json()
    assert len(c['isolation']) == 1


# ------------------------------------------------------------ 冻结与补录
def test_freeze_excludes_later_relations_but_isolates(client):
    t = tree(client)
    now = t['now']
    # 总成在起点后仍在 1 号车上，之后拆下再装到 2 号车（冻结后登记）
    assert baseline(client, t, -90, -60).status_code == 201
    install(client, t['asm']['id'], t['seal']['id'], now - 80 * DAY, None)
    rec = post(client, '/lineage/recalls', {'code': uniq('RC'), 'part_id': t['seal']['id'],
                                            'started_at': now - 70 * DAY}).json()
    # 冻结边界之后：整车装此总成
    late = install(client, t['car2']['id'], t['asm']['id'], time.time() - 1)
    assert late.status_code == 201
    detail = client.get(f'/lineage/recalls/{rec["id"]}').json()
    assert t['car2']['id'] not in {h['machine_id'] for h in detail['hits']}
    entry = [e for e in detail['isolation'] if e['machine_id'] == t['car2']['id']]
    assert entry and entry[0]['status'] == 'isolated'
    assert entry[0]['reason'] == 'late_relation'


def test_released_machine_reisolated_only_when_new_relation_coexists(client):
    t = tree(client)
    now = t['now']
    assert baseline(client, t).status_code == 201
    install(client, t['asm']['id'], t['seal']['id'], now - 80 * DAY, None)
    rec = post(client, '/lineage/recalls', {'code': uniq('RC'), 'part_id': t['seal']['id'],
                                            'started_at': now - 70 * DAY}).json()
    entry = rec['isolation'][0]
    post(client, f'/lineage/isolation/{entry["id"]}/release', {'voucher_no': 'RCV-1'})
    # 与整车时段不共存的旧总成关系：不改变隔离
    lonely = make_part(client, uniq('ASM'), 'assembly')
    install(client, t['car2']['id'], lonely['id'], now - 300 * DAY, now - 400 * DAY)
    detail = client.get(f'/lineage/recalls/{rec["id"]}').json()
    still = [e for e in detail['isolation'] if e['machine_id'] == t['car1']['id']][0]
    assert still['status'] == 'released'


# ------------------------------------------------------------ 隔离与结案
def test_release_requires_voucher(client):
    t = tree(client)
    now = t['now']
    assert baseline(client, t).status_code == 201
    install(client, t['asm']['id'], t['seal']['id'], now - 80 * DAY, None)
    rec = post(client, '/lineage/recalls', {'code': uniq('RC'), 'part_id': t['seal']['id'],
                                            'started_at': now - 70 * DAY}).json()
    entry = rec['isolation'][0]
    assert post(client, f'/lineage/isolation/{entry["id"]}/release',
                {'voucher_no': 'ab'}).status_code == 422
    ok = post(client, f'/lineage/isolation/{entry["id"]}/release',
              {'voucher_no': 'RCV-2026-9', 'voucher_note': '更换复检'}).json()
    assert ok['status'] == 'released' and ok['voucher_no'] == 'RCV-2026-9'


def test_close_blocked_until_pending_zero(client):
    t = tree(client)
    now = t['now']
    assert baseline(client, t).status_code == 201
    install(client, t['asm']['id'], t['seal']['id'], now - 80 * DAY, None)
    rec = post(client, '/lineage/recalls', {'code': uniq('RC'), 'part_id': t['seal']['id'],
                                            'started_at': now - 70 * DAY}).json()
    blocked = post(client, f'/lineage/recalls/{rec["id"]}/close', {})
    assert blocked.status_code == 409 and '待处理' in blocked.json()['detail']
    entry = rec['isolation'][0]
    post(client, f'/lineage/isolation/{entry["id"]}/release', {'voucher_no': 'RCV-X1'})
    closed = post(client, f'/lineage/recalls/{rec["id"]}/close', {})
    assert closed.status_code == 200 and closed.json()['status'] == 'closed'


def test_late_old_relation_reopens_closed_recall(client):
    t = tree(client)
    now = t['now']
    # 总线 A 在 car1 已于起点后拆下，可改装到 car2；该关系事后补录
    assert baseline(client, t, -90, -65).status_code == 201
    install(client, t['asm']['id'], t['seal']['id'], now - 80 * DAY, None)
    rec = post(client, '/lineage/recalls', {'code': uniq('RC'), 'part_id': t['seal']['id'],
                                            'started_at': now - 70 * DAY}).json()
    entry = rec['isolation'][0]
    post(client, f'/lineage/isolation/{entry["id"]}/release', {'voucher_no': 'RCV-X1'})
    post(client, f'/lineage/recalls/{rec["id"]}/close', {})
    # 事后补录一条旧关系（起点后时段重叠），把另一台车拉进来
    resp = install(client, t['car2']['id'], t['asm']['id'], now - 60 * DAY, None,
                   key=uniq('late-old'))
    assert resp.status_code == 201, resp.text
    late_old = resp.json()
    reopen = post(client, f'/lineage/recalls/{rec["id"]}/late-relation',
                  {'install_id': late_old['id']})
    assert reopen.status_code == 200
    body = reopen.json()
    assert body['status'] == 'open' and body['reopen_count'] == 1
    assert t['car2']['id'] in {e['machine_id'] for e in body['isolation']}


def test_new_relation_after_close_reopens_automatically(client):
    t = tree(client)
    now = t['now']
    assert baseline(client, t, -90, -60).status_code == 201
    install(client, t['asm']['id'], t['seal']['id'], now - 80 * DAY, None)
    rec = post(client, '/lineage/recalls', {'code': uniq('RC'), 'part_id': t['seal']['id'],
                                            'started_at': now - 70 * DAY}).json()
    post(client, f'/lineage/isolation/{rec["isolation"][0]["id"]}/release',
         {'voucher_no': 'RCV-77'})
    assert post(client, f'/lineage/recalls/{rec["id"]}/close', {}).status_code == 200
    install(client, t['car2']['id'], t['asm']['id'], time.time() - 1)
    detail = client.get(f'/lineage/recalls/{rec["id"]}').json()
    assert detail['status'] == 'open' and detail['pending'] == 1


# ------------------------------------------------------------ 幂等与权限
def test_idempotent_same_key_same_content(client):
    t = tree(client)
    now = t['now']
    key = uniq('idem')
    a = install(client, t['car1']['id'], t['seal']['id'], now - 10 * DAY, key=key)
    b = install(client, t['car1']['id'], t['seal']['id'], now - 10 * DAY, key=key)
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()['id'] == b.json()['id']
    rows = client.get('/lineage/installs', params={'child_id': t['seal']['id']}).json()
    assert len(rows) == 1


def test_idempotent_same_key_different_content_rejected(client):
    t = tree(client)
    now = t['now']
    key = uniq('idem')
    install(client, t['car1']['id'], t['seal']['id'], now - 10 * DAY, key=key)
    r = install(client, t['car1']['id'], t['seal']['id'], now - 9 * DAY, key=key)
    assert r.status_code == 409 and '不同内容' in r.json()['detail']


def test_idempotency_key_required(client):
    t = tree(client)
    r = client.post('/lineage/installs', json={
        'parent_id': t['car1']['id'], 'child_id': t['seal']['id'],
        'installed_at': time.time() - DAY})
    assert r.status_code == 422


def test_viewer_cannot_write(client):
    viewer = TestClient(app)
    token = viewer.post('/auth/login', json={'email': 'viewer@demo.local',
                                             'password': 'Demo123!'}).json()['access_token']
    viewer.headers.update({'Authorization': 'Bearer ' + token})
    r = viewer.post('/lineage/parts', json={'serial': uniq('X'), 'name': 'x',
                                            'kind': 'component'},
                    headers={'Idempotency-Key': uniq()})
    assert r.status_code == 403


def test_isolated_samples_excluded_from_new_estimate(client):
    """隔离整机上的在装部件资产号不进入新的分析任务参数。"""
    t = tree(client)
    now = t['now']
    seal = make_part(client, 'AC-8-777', 'component')   # 资产号与数据集对齐
    assert baseline(client, t).status_code == 201
    install(client, t['asm']['id'], seal['id'], now - 80 * DAY, None)
    rec = post(client, '/lineage/recalls', {'code': uniq('RC'), 'part_id': seal['id'],
                                            'started_at': now - 70 * DAY}).json()
    assert rec['pending'] == 1
    ds = client.post('/datasets', json={
        'name': uniq('批次'), 'records':
            [{'asset': 'AC-8-777', 'model': 'AC-8', 'km': 60000, 'failed': 0}]
            + [{'asset': f'AC-8-{i:03}', 'model': 'AC-8', 'km': 50000 + i, 'failed': 0}
               for i in range(1, 31)]}).json()
    job = client.post('/jobs', json={'name': '隔离排除校验', 'dataset_id': ds['id'],
                                     'params': {}}).json()
    assert 'AC-8-777' in job['params'].get('excluded_assets', [])
