import {JobStatus,RecallStatus,IsolationStatus} from '../../constants/enums.js';
import {esc} from '../../utils/dom.js';
export const status=v=>'<span class="status '+esc(v)+'">'+esc(JobStatus[v]||v)+'</span>';
export const recallStatus=v=>'<span class="status '+esc(v)+'">'+esc(RecallStatus[v]||v)+'</span>';
export const isolationStatus=v=>'<span class="status '+esc(v)+'">'+esc(IsolationStatus[v]||v)+'</span>';
