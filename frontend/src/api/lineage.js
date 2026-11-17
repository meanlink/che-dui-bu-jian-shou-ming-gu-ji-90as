import {api} from './client.js';

function key(){return (crypto.randomUUID?crypto.randomUUID():String(Date.now())+Math.random().toString(16).slice(2));}
function post(path,body){return api(path,{method:'POST',body:JSON.stringify(body),headers:{'Idempotency-Key':key()}});}

export const lineage={
 parts:kind=>api('/lineage/parts'+(kind?'?kind='+kind:'')),
 createPart:data=>post('/lineage/parts',data),
 installs:params=>api('/lineage/installs'+(params?'?'+new URLSearchParams(params):'')),
 install:data=>post('/lineage/installs',data),
 remove:(id,data)=>post('/lineage/installs/'+id+'/remove',data),
 tree:id=>api('/lineage/installs/'+id+'/tree'),
 summary:()=>api('/lineage/summary'),
 recalls:()=>api('/lineage/recalls'),
 recall:id=>api('/lineage/recalls/'+id),
 openRecall:data=>post('/lineage/recalls',data),
 trace:id=>post('/lineage/recalls/'+id+'/trace',{}),
 freeze:id=>post('/lineage/recalls/'+id+'/freeze',{}),
 lateRelation:(id,install_id)=>post('/lineage/recalls/'+id+'/late-relation',{install_id}),
 close:(id,data)=>post('/lineage/recalls/'+id+'/close',data||{}),
 reopenRecall:(id,reason)=>post('/lineage/recalls/'+id+'/reopen',{reason}),
 isolation:params=>api('/lineage/isolation'+(params?'?'+new URLSearchParams(params):'')),
 release:(id,data)=>post('/lineage/isolation/'+id+'/release',data),
 reopenIsolation:(id,reason)=>post('/lineage/isolation/'+id+'/reopen',{reason}),
};
