import {lineage} from '../api/lineage.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {heading} from '../components/common/shell.js';
import {recallStatus,isolationStatus} from '../components/common/status.js';
import {PartKind} from '../constants/enums.js';
import {esc,date,stamp,fromInput,toInput,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const canWrite=()=>session.user.role!=='viewer';
const canAdmin=()=>session.user.role==='admin';
const DAY=86400;

function partOptions(parts,selected){
 return parts.map(p=>'<option value="'+p.id+'" '+(p.id===selected?'selected':'')+'>'+esc(p.serial)+' · '+esc(PartKind[p.kind]||p.kind)+' · '+esc(p.name)+'</option>').join('');
}

function relationLabel(r){return r==='removed'?'已拆下':r==='late'?'冻结后补录':'当前在装';}

function pathText(path, byId){
 if(!path?.length)return '—';
 const name=id=>byId[id]?byId[id].serial:'#'+id;
 return '<ol class="path-chain">'+path.map(s=>'<li>'+esc(name(s.child_id))+' → '+esc(name(s.parent_id))+'</li>').join('')+'</ol>';
}

function recallBlock(r, parts){
 const byId=Object.fromEntries(parts.map(p=>[p.id,p]));
 const legend='<p class="hint">结案条件：待处置为 0；每台解除隔离须逐台登记更换凭证；结案后补录旧关系自动重开。</p>';
 const hits=r.hits?.length?r.hits.map(h=>'<tr><td>'+esc(h.machine?.serial||h.machine_id)+'</td><td>'+esc(h.machine?.name||'')+'</td><td>'+(h.via_removed?'含已拆下关系':'当前在装')+'</td><td>'+pathText(h.path,byId)+'</td></tr>').join(''):'<tr><td colspan="4" class="muted">尚未追到整机。</td></tr>';
 const entries=r.isolation?.length?r.isolation.map(e=>'<tr><td>'+esc(e.machine?.serial||e.machine_id)+'</td><td>'+isolationStatus(e.status)+'</td><td>'+esc(e.reason==='hit'?'追溯命中':e.reason==='late_relation'?'冻结后补录':'处置补录')+'</td><td>'+esc(e.voucher_no||'—')+'</td><td>'+date(e.released_at)+'</td><td>'+(e.status==='isolated'&&canAdmin()?'<button class="text-button" data-release="'+e.id+'">登记凭证解除</button>':'')+(e.status==='released'&&canAdmin()?'<button class="text-button" data-reisolate="'+e.id+'">重新隔离</button>':'')+'</td></tr>').join(''):'<tr><td colspan="6" class="muted">隔离名单为空。</td></tr>';
 return '<article class="recall-card" data-recall="'+r.id+'"><header><div><span class="eyebrow">'+esc(r.code)+'</span><h3>'+esc(r.part?.serial||('部件 #'+r.part_id))+' · '+esc(r.part?.name||'')+'</h3></div>'+recallStatus(r.status)+'</header><div class="recall-meta"><span>故障起点 '+date(r.started_at)+'</span><span>关系冻结 '+date(r.frozen_at)+'</span><span>待处置 '+r.pending+' 台</span><span>重开 '+r.reopen_count+' 次</span></div><h4>追到的整机</h4><div class="table-wrap small"><table><thead><tr><th>整机</th><th>名称</th><th>路径特征</th><th>装配链（小件→整机）</th></tr></thead><tbody>'+hits+'</tbody></table></div><h4>隔离名单</h4><div class="table-wrap small"><table><thead><tr><th>整机</th><th>状态</th><th>来源</th><th>更换凭证</th><th>解除时间</th><th>处置</th></tr></thead><tbody>'+entries+'</tbody></table></div><div class="recall-actions">'+(canWrite()?'<button class="button small" data-trace="'+r.id+'">重新追溯</button>':'')+(canAdmin()&&r.status!=='closed'?'<button class="button small" data-freeze="'+r.id+'">冻结关系边界</button>':'')+(canAdmin()&&r.status!=='closed'?'<button class="button small primary" data-close="'+r.id+'" '+(r.pending?'disabled title="待处理未清零"':'')+'>结案</button>':'')+(canAdmin()&&r.status==='closed'?'<button class="button small" data-reopen-recall="'+r.id+'">重开召回</button>':'')+(r.status==='closed'&&canAdmin()?'<button class="text-button" data-late="'+r.id+'">补录旧关系</button>':'')+'</div>'+legend+'</article>';
}

export async function lineagePage(){
 const [parts,recalls,installs,summary]=await Promise.all([lineage.parts(),lineage.recalls(),lineage.installs(),lineage.summary()]);
 workspace.parts=parts;workspace.recalls=recalls;
 const seals=parts.filter(p=>p.kind==='component');
 const installRows=installs.slice(0,40).map(i=>'<tr><td>'+esc(parts.find(p=>p.id===i.child_id)?.serial||i.child_id)+'</td><td>'+esc(parts.find(p=>p.id===i.parent_id)?.serial||i.parent_id)+'</td><td>'+date(i.installed_at)+'</td><td>'+date(i.removed_at)+'</td><td>'+(i.open?'<span class="status running">在装</span>':'<span class="status">已拆下</span>')+'</td><td>'+(i.open&&canWrite()?'<button class="text-button" data-remove="'+i.id+'">登记拆下</button>':'')+'</td></tr>').join('');
 return heading('06 / LINEAGE & RECALL','装卸谱系与召回追查','每次装卸按时刻留痕，拆换只追加不覆盖。召回从故障起点沿时段重叠的装配路径追到整机，已拆下也不漏列。','<a class="button" href="#/recalls">召回处置台 ↗</a>')
 +'<section class="overview-hero compact-hero"><div class="narrative"><p class="eyebrow">当前谱系规模</p><h2>'+summary.parts+' 个部件，'+summary.installs+' 条装卸关系，'+summary.active_recalls+' 个召回进行中。</h2><p>隔离中 '+summary.isolated+' 台整机 · 已解除 '+summary.released+' 台 · 在装关系 '+summary.open_installs+' 条。</p></div><div class="numbers">'+[['整机',parts.filter(p=>p.kind==='machine').length],['总成',parts.filter(p=>p.kind==='assembly').length],['小件',seals.length],['隔离中',summary.isolated]].map(([k,v])=>'<div><span>'+k+'</span><strong>'+v+'</strong></div>').join('')+'</div></section>'
 +'<div class="dataset-grid"><section class="panel"><div class="section-title"><h2>登记装卸</h2><span>幂等提交 · 服务端校验</span></div><form id="install-form"><label>上级（整机/总成）<select name="parent_id" required>'+partOptions(parts.filter(p=>p.kind!=='component'))+'</select></label><label>下级部件<select name="child_id" required>'+partOptions(parts.filter(p=>p.kind!=='machine'))+'</select></label><div class="parameter-grid"><label>安装时刻<input type="datetime-local" name="installed_at" value="'+toInput(Date.now()/1000)+'" required></label><label>拆下时刻（可空）<input type="datetime-local" name="removed_at"></label></div><label>备注<input name="note" maxlength="500" placeholder="例如：A 级保养换装"></label><button type="submit" class="primary" '+(canWrite()?'':'disabled')+'>登记关系</button><p class="hint">同一部件同一时刻只允许一处安装；循环装配会被拒绝；拆换保留旧关系。</p></form><div class="section-title"><h2>登记新部件</h2></div><form id="part-form"><div class="parameter-grid"><label>序列号<input name="serial" required minlength="2"></label><label>名称<input name="name" required></label><label>类型<select name="kind"><option value="machine">整机</option><option value="assembly">总成</option><option value="component">小件</option></select></label><label>型号<input name="model" placeholder="AC-8 / AC-12"></label></div><button type="submit" '+(canWrite()?'':'disabled')+'>登记部件</button></form></section><section class="panel"><div class="section-title"><h2>最近装卸关系</h2><span>'+installs.length+' 条</span></div><div class="table-wrap"><table><thead><tr><th>下级</th><th>上级</th><th>安装</th><th>拆下</th><th>状态</th><th>操作</th></tr></thead><tbody>'+installRows+'</tbody></table></div></section></div>'
 +'<section class="panel"><div class="section-title"><h2>召回事件</h2><span>'+recalls.length+' 个</span></div><div class="recall-list">'+(recalls.map(r=>recallBlock(r,parts)).join('')||'<p class="empty">尚无召回。发起召回后，从故障起点追溯装配路径。</p>')+'</div></section>'
 +'<section class="panel"><div class="section-title"><h2>发起召回</h2><span>需 admin / analyst</span></div><form id="recall-form"><div class="parameter-grid"><label>召回编号<input name="code" required minlength="2" placeholder="RC-2026-014"></label><label>召回小件<select name="part_id" required>'+partOptions(seals)+'</select></label><label>故障起点<input type="datetime-local" name="started_at" value="'+toInput(Date.now()/1000-DAY)+'" required></label><label>说明<input name="note" placeholder="密封件批次缺陷"></label></div><button type="submit" class="primary" '+(canWrite()?'':'disabled')+'>发起并立即追溯</button><p class="hint">发起时冻结完整关系边界；此后补录的关系不改变本次边界，但会自动补入隔离名单。</p></form></section>';
}

export function bindLineage(refresh){
 document.querySelector('#install-form')&&submit(document.querySelector('#install-form'),async f=>{
   await lineage.install({parent_id:Number(f.get('parent_id')),child_id:Number(f.get('child_id')),installed_at:fromInput(f.get('installed_at')),removed_at:f.get('removed_at')?fromInput(f.get('removed_at')):null,note:f.get('note')||''});
   notify('装卸关系已登记');await refresh();
 });
 document.querySelector('#part-form')&&submit(document.querySelector('#part-form'),async f=>{
   await lineage.createPart({serial:f.get('serial'),name:f.get('name'),kind:f.get('kind'),model:f.get('model')||''});
   notify('部件已登记');await refresh();
 });
 document.querySelector('#recall-form')&&submit(document.querySelector('#recall-form'),async f=>{
   const r=await lineage.openRecall({code:f.get('code'),part_id:Number(f.get('part_id')),started_at:fromInput(f.get('started_at'))});
   notify('召回已发起，已追到 '+r.hits.length+' 台整机');location.hash='#/recalls/'+r.id;
 });
 document.querySelectorAll('[data-remove]').forEach(b=>b.onclick=async()=>{
   const at=prompt('拆下时刻（ISO，如 2026-11-17T10:00）',new Date().toISOString().slice(0,16));
   if(!at)return;
   try{await lineage.remove(b.dataset.remove,{removed_at:fromInput(at)});notify('已登记拆下，旧关系保留');await refresh();}catch(e){notify(e.message,true);}
 });
 document.querySelectorAll('[data-trace]').forEach(b=>b.onclick=async()=>{try{const r=await lineage.trace(b.dataset.trace);notify('已重新追溯，命中 '+r.hits.length+' 台');await refresh();}catch(e){notify(e.message,true);}});
 document.querySelectorAll('[data-freeze]').forEach(b=>b.onclick=async()=>{try{await lineage.freeze(b.dataset.freeze);notify('关系边界已冻结');await refresh();}catch(e){notify(e.message,true);}});
 document.querySelectorAll('[data-close]').forEach(b=>b.onclick=async()=>{try{await lineage.close(b.dataset.close,{});notify('召回已结案');await refresh();}catch(e){notify(e.message,true);}});
 document.querySelectorAll('[data-reopen-recall]').forEach(b=>b.onclick=async()=>{
   const reason=prompt('重开原因','补录旧关系复核');if(!reason)return;
   try{await lineage.reopenRecall(b.dataset.reopenRecall,reason);notify('召回已重开');await refresh();}catch(e){notify(e.message,true);}
 });
 document.querySelectorAll('[data-release]').forEach(b=>b.onclick=async()=>{
   const voucher=prompt('更换凭证编号（每台整机必填）','RCV-2026-');if(!voucher)return;
   const note=prompt('凭证说明（可空）','更换密封件并复检')||'';
   try{await lineage.release(b.dataset.release,{voucher_no:voucher,voucher_note:note});notify('已登记凭证并解除隔离');await refresh();}catch(e){notify(e.message,true);}
 });
 document.querySelectorAll('[data-reisolate]').forEach(b=>b.onclick=async()=>{
   const reason=prompt('重新隔离原因','复核更换凭证不充分');if(!reason)return;
   try{await lineage.reopenIsolation(b.dataset.reisolate,reason);notify('该整机已重新隔离');await refresh();}catch(e){notify(e.message,true);}
 });
 document.querySelectorAll('[data-late]').forEach(b=>b.onclick=async()=>{
   const id=prompt('补录的装卸记录 ID（旧关系）');if(!id)return;
   try{const r=await lineage.lateRelation(b.dataset.late,Number(id));notify('补录后召回状态：'+(r.status==='open'?'已重开':'待处置'));await refresh();}catch(e){notify(e.message,true);}
 });
}

export async function recallPage(id){
 const [recalls,parts]=await Promise.all([lineage.recalls(),lineage.parts()]);
 workspace.recalls=recalls;workspace.parts=parts;
 const active=recalls.filter(r=>r.status!=='closed');
 const closed=recalls.filter(r=>r.status==='closed');
 const cards=recalls.map(r=>recallBlock(r,parts)).join('')||'<p class="empty">尚无召回。</p>';
 return heading('07 / RECALL DESK','召回处置与结案','隔离名单逐台核销：每台整机必须登记更换凭证才能解除隔离；待处理未清零拒绝结案；结案后补录旧关系自动重开。')
 +'<section class="overview-hero compact-hero"><div class="narrative"><p class="eyebrow">处置状态</p><h2>'+(active.length?active.length+' 个召回仍在处置，合计 '+recalls.reduce((n,r)=>n+r.pending,0)+' 台整机待核销。':'所有召回均已结案，名单清零。')+'</h2><p>已结案 '+closed.length+' 个 · 结案后可因补录旧关系重开。隔离整机不进入新的可靠度估计。</p><a href="#/lineage" class="text-link">回到装卸谱系 ↗</a></div><div class="numbers">'+[['召回',recalls.length],['进行中',active.length],['待处置',recalls.reduce((n,r)=>n+r.pending,0)],['已结案',closed.length]].map(([k,v])=>'<div><span>'+k+'</span><strong>'+v+'</strong></div>').join('')+'</div></section>'
 +'<section class="panel"><div class="section-title"><h2>召回明细</h2><span>按发起时间倒序</span></div><div class="recall-list">'+cards+'</div></section>';
}

export function bindRecall(refresh){bindLineage(refresh);}
