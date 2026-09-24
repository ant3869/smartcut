const state={jobs:[],selectedJobId:null,selectedMedia:'source',playhead:0,selection:{start:0,end:2},rotation:0,serverReady:false,editing:null};
const app=document.querySelector('#app'),$=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const apiFile=p=>'/api/file?path='+encodeURIComponent(String(p).replace(/\\/g,'/')),round=v=>Math.round(Number(v||0)*1000)/1000;
const basename=p=>String(p||'Unknown source').split(/[\\/]/).pop();
const seconds=v=>{const n=Math.max(0,Number(v||0)),m=Math.floor(n/60);return String(m).padStart(2,'0')+':'+(n%60).toFixed(1).padStart(4,'0')};
const esc=v=>String(v||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
async function api(path,options={}){const res=await fetch(path,{headers:{'Content-Type':'application/json'},...options});if(!res.ok){const data=await res.json().catch(()=>({}));throw new Error(data.detail||(res.status+' '+res.statusText))}return res.json()}
function toast(message,kind='info'){let region=$('.toast-region');if(!region){region=document.createElement('div');region.className='toast-region';document.body.append(region)}const n=document.createElement('div');n.className='toast '+kind;n.textContent=message;region.append(n);setTimeout(()=>n.remove(),4400)}
function selected(){return state.jobs.find(j=>j.id===state.selectedJobId)||state.jobs[0]||null}
function review(job){return job.review||{source_sha256:job.source_fingerprint?.sha256||job.source_sha256||'',timebase:'source',cut_intervals:[],keep_intervals:[]}}

function stopPlayback(){
  const v=$('#review-video');
  if(v&&!v.paused){
    v.pause();
    syncTransport();
  }
}

function preferredMedia(job){if(state.selectedMedia==='source'&&!job.source_available)return job.final_output?.path?'final':job.preview_output?.path?'preview':'source';return state.selectedMedia}
function media(job){const mode=preferredMedia(job);return mode==='final'?job.final_output?.path:mode==='preview'?job.preview_output?.path:job.source}
async function load({quiet=false}={}){try{const [health,jobs]=await Promise.all([api('/api/health'),api('/api/jobs')]);state.serverReady=Boolean(health.ok);state.jobs=jobs;if(!state.selectedJobId||!jobs.some(j=>j.id===state.selectedJobId))state.selectedJobId=jobs[0]?.id||null;const job=selected();if(job)state.selectedMedia=preferredMedia(job);clamp(job);render()}catch(error){state.serverReady=false;render();if(!quiet)toast(error.message,'danger')}}
function clamp(job){if(!job)return;const duration=Number(job.duration||1);state.playhead=Math.min(state.playhead,duration);state.selection.start=Math.max(0,Math.min(state.selection.start,duration));state.selection.end=Math.max(state.selection.start+.1,Math.min(state.selection.end,duration))}

function seekTo(time){
  const job=selected();
  if(!job)return;
  const d=Number(job.duration||1);
  state.playhead=Math.max(0,Math.min(d,round(time)));
  const v=$('#review-video');
  if(v)v.currentTime=state.playhead;
  syncTransport();
  $$('.playhead').forEach(n=>n.style.left=(state.playhead/d)*100+'%');
  const read=$('#time-readout');
  if(read)read.textContent=seconds(state.playhead)+' / '+seconds(d);
}

function editReviewInterval(job,kind,index){
  const intervals=kind==='cut'?review(job).cut_intervals||[]:review(job).keep_intervals||[];
  const interval=intervals[index];
  if(!interval)return;
  state.editing={kind,index};
  state.selection={start:round(interval.start),end:round(interval.end)};
  state.playhead=state.selection.start;
  const reason=$('#cut-reason');
  if(reason)reason.value=interval.reason||'editorial review';
  seekTo(state.playhead);
  updateSelectionInputs();
  renderSelectionOverlay();
  render();
}

function clearEditing(){state.editing=null}

function setInPoint(){
  const job=selected();
  if(!job)return;
  state.selection.start=round(state.playhead);
  if(state.selection.end<=state.selection.start){
    state.selection.end=round(Math.min(Number(job.duration),state.selection.start+1));
  }
  clamp(job);
  updateSelectionInputs();
  renderSelectionOverlay();
}

function setOutPoint(){
  const job=selected();
  if(!job)return;
  state.selection.end=round(state.playhead);
  if(state.selection.start>=state.selection.end){
    state.selection.start=round(Math.max(0,state.selection.end-1));
  }
  clamp(job);
  updateSelectionInputs();
  renderSelectionOverlay();
}

function updateSelectionInputs(){
  const s=$('#cut-start'),e=$('#cut-end'),st=$('.selection-time');
  if(s)s.value=state.selection.start;
  if(e)e.value=state.selection.end;
  if(st)st.innerHTML='<span>'+seconds(state.selection.start)+'</span><span>'+seconds(state.selection.end)+'</span>';
}

function renderSelectionOverlay(){
  const job=selected();
  if(!job)return;
  const d=Number(job.duration||1);
  const left=(state.selection.start/d)*100;
  const width=Math.max(0.25,((state.selection.end-state.selection.start)/d)*100);
  $$('.selection-span').forEach(el=>{
    el.style.left=left+'%';
    el.style.width=width+'%';
  });
}

function render(){
  const job=selected();
  app.innerHTML='<div class="cutroom"><aside class="sources"><div class="brand"><span class="brand-mark">A</span><span>Anna Cutroom</span></div><div class="source-heading"><span>Sources</span><span>'+state.jobs.length+'</span></div><div class="source-list">'+state.jobs.map(sourceRow).join('')+'</div><div class="source-footer"><span class="server-indicator '+(state.serverReady?'':'off')+'"></span>'+(state.serverReady?'local backend ready':'backend unavailable')+'</div></aside><section class="workspace"><header class="toolbar"><div class="toolbar-title"><strong>'+esc(job?basename(job.source):'No source selected')+'</strong><span>'+(job?(seconds(job.duration)+' · '+(job.metrics?.observation_count||0)+' Eye observations'):'Choose a completed plan')+'</span></div><div class="toolbar-actions"><span class="shortcuts-hint">[Space] Play &nbsp;|&nbsp; [I] In &nbsp;|&nbsp; [O] Out &nbsp;|&nbsp; [C] Cut &nbsp;|&nbsp; [K] Keep &nbsp;|&nbsp; [P] Protect &nbsp;|&nbsp; [←/→] Scrub</span><button class="button ghost" data-action="refresh">Refresh</button><button class="button primary" data-action="render" '+(job?'':'disabled')+'>Render approved cut</button></div></header><main id="review" class="review">'+(job?reviewView(job):'<div class="empty">No analyzed jobs yet.</div>')+'</main></section></div>';
  bind(job);
}

function sourceRow(job){
  return '<button class="source-row '+(job.id===state.selectedJobId?'active':'')+'" data-job="'+esc(job.id)+'"><span class="source-name">'+esc(basename(job.source))+'</span><span class="source-meta">'+seconds(job.duration)+' · '+job.status+'</span></button>';
}

function reviewView(job){
  const r=review(job),e=nearby(job),path=media(job),mode=preferredMedia(job),sourceMissing=!job.source_available;
  const editing=state.editing,editingLabel=editing?(editing.kind==='cut'?'Editing cut':'Editing keep/protect'):'New decision',editingReason=editing?((editing.kind==='cut'?r.cut_intervals:r.keep_intervals)[editing.index]?.reason||'editorial review'):'editorial review',cutLabel=editing?.kind==='cut'?'Update cut [C]':'Cut [C]',remove=editing?'<button class="button ghost remove-decision" data-action="remove-decision">Remove '+(editing.kind==='cut'?'cut':'keep/protect')+'</button>':'';
  return '<section class="player"><div class="video-wrap">'+(path?('<video id="review-video" src="'+apiFile(path)+'" preload="metadata" aria-label="Video review player"></video>'):'<div class="empty">That output does not exist yet.</div>')+'</div><div class="player-controls"><div class="media-switch">'+['source','final','preview'].map(m=>'<button data-media="'+m+'" class="'+(m===mode?'active':'')+'" '+((m==='source'?!job.source_available:m!=='source'&&!job[m+'_output'])?'disabled':'')+'>'+m+'</button>').join('')+'</div><span id="time-readout">'+seconds(state.playhead)+' / '+seconds(job.duration)+'</span></div>'+(sourceMissing?'<div class="source-missing">original source is gone — showing the final render. source-time edits are unavailable.</div>':'')+'</section><aside class="inspector"><div class="inspector-head"><h1>'+editingLabel+'</h1><p>'+esc(basename(job.source))+'</p></div><div class="inspector-section"><div class="selection-time"><span>'+seconds(state.selection.start)+'</span><span>'+seconds(state.selection.end)+'</span></div><div class="fields"><label class="field"><span class="field-label">Start [I]</span><input id="cut-start" type="number" min="0" max="'+job.duration+'" step="0.1" value="'+state.selection.start+'"></label><label class="field"><span class="field-label">End [O]</span><input id="cut-end" type="number" min="0" max="'+job.duration+'" step="0.1" value="'+state.selection.end+'"></label></div><div class="mark-buttons-row"><button class="button ghost small-btn" id="set-in-btn" type="button">Set In [I]</button><button class="button ghost small-btn" id="set-out-btn" type="button">Set Out [O]</button></div><label class="field reason"><span class="field-label">Reason</span><input id="cut-reason" value="'+esc(editingReason)+'" maxlength="120"></label></div><div class="inspector-section"><div class="decision-actions"><button class="button" data-decision="keep">Keep [K]</button><button class="button cut" data-decision="cut">'+cutLabel+'</button><button class="button protect" data-decision="protect">Protect [P]</button></div>'+remove+'</div><div class="inspector-section"><span class="field-label">Nearest Eye evidence</span><div class="evidence">'+(e.map(x=>'<div class="evidence-item"><span class="evidence-dot '+(x.keep?'':'cut')+'"></span><span><strong>'+seconds(x.timestamp)+'</strong> '+confidenceBadge(x.confidence==null?null:Number(x.confidence))+' · '+esc(x.keep?'kept':x.cull_reason||'cut')+'<br>'+esc(x.description||'No description')+'</span></div>').join('')||'<span class="evidence-item">No nearby evidence</span>')+'</div></div></aside><section class="timeline-panel">'+timeline(job,r)+'</section>'+reviewQueue(job)+'<section class="log"><div class="decision-log"><h2>Decision history</h2><div class="history">'+history(r)+'</div></div><div class="render-box"><h2>Ready when you are</h2><p>Your edits save as hash-bound source ranges. Re-plan uses them; render never touches the original.</p><button class="button primary" data-action="render">Render approved cut</button><button class="button ghost" data-action="export-otio" style="margin-top:8px">Export timeline (.otio)</button></div></section>';
}

function timeline(job,r){
  const d=Number(job.duration||1),pct=v=>Math.max(0,Math.min(100,(Number(v||0)/d)*100)),span=(x,c,kind='',index=-1)=>'<span class="span '+c+(kind?' review-span':'')+'" '+(kind?'data-review-kind="'+kind+'" data-review-index="'+index+'" role="button" tabindex="0" aria-label="Edit '+kind+' decision '+seconds(x.start)+' to '+seconds(x.end)+'"':'')+' style="left:'+pct(x.start)+'%;width:'+Math.max(.25,pct(x.end)-pct(x.start))+'%;height:16px" title="'+esc(x.reason||(x.reasons||[]).join(', '))+'">'+(kind==='cut'?'<i class="trim-handle trim-start" data-trim-edge="start" aria-label="Drag cut start"></i><i class="trim-handle trim-end" data-trim-edge="end" aria-label="Drag cut end"></i>':'')+'</span>',ticks=Array.from({length:11},(_,i)=>'<i class="tick" style="left:'+(i*10)+'%"><span>'+seconds(d*i/10)+'</span></i>').join(''),reject=job.observations.filter(x=>!x.keep),cuts=r.cut_intervals||[],keeps=r.keep_intervals||[],rail=(items,c,extra='',kind='')=>'<div class="timeline-rail" data-timeline>'+items.map((x,index)=>span(x,c,kind,index)).join('')+extra+'<span class="selection-span" style="left:'+pct(state.selection.start)+'%;width:'+Math.max(.25,pct(state.selection.end)-pct(state.selection.start))+'%;"></span><span class="playhead" style="left:'+pct(state.playhead)+'%"></span></div>';
  return '<div class="timeline-head"><h2>Timeline</h2><span>Click a red marked range to edit it. Click or drag empty timeline to scrub.</span></div><div class="timeline"><div class="ruler">'+ticks+'</div><div class="timeline-row"><div class="timeline-label">Plan</div>'+rail(job.clips,'keep')+'</div><div class="timeline-row"><div class="timeline-label">Your cuts</div>'+rail(cuts,'cut',keeps.map((x,index)=>span(x,'protect','keep',index)).join(''),'cut')+'</div><div class="timeline-row"><div class="timeline-label">Eye flags</div>'+rail([],'none',reject.map(x=>{
    const conf=x.confidence==null?1:Number(x.confidence);
    return '<span class="marker rejected'+(conf<0.6?' uncertain':'')+'" data-seek="'+x.timestamp+'" role="button" tabindex="0" aria-label="Seek to Eye flag at '+seconds(x.timestamp)+'" style="left:'+pct(x.timestamp)+'%" title="'+esc((x.cull_reason||'rejected')+' · confidence '+Math.round(conf*100)+'% · click to seek')+'"></span>';
  }).join(''))+'</div></div>';
}

function history(r){
  const rows=[...(r.cut_intervals||[]).map(x=>({...x,action:'cut'})),...(r.keep_intervals||[]).map(x=>({...x,action:x.reason==='protected'?'protect':'keep'}))].sort((a,b)=>a.start-b.start);
  return rows.length?rows.map(x=>'<div class="history-row"><span>'+seconds(x.start)+'–'+seconds(x.end)+'</span><span class="history-action '+x.action+'">'+x.action+'</span><span>'+esc(x.reason||'editorial review')+'</span></div>').join(''):'<div class="history-row"><span>—</span><span>—</span><span>No human decisions yet.</span></div>';
}

function nearby(job){
  const mid=(state.selection.start+state.selection.end)/2;
  return[...(job.observations||[])].sort((a,b)=>Math.abs(a.timestamp-mid)-Math.abs(b.timestamp-mid)).slice(0,2);
}

function parseConfidence(reasons){
  for(const r of reasons||[]){const m=/confidence:([0-9.]+)/.exec(String(r));if(m)return Number(m[1])}
  return null;
}

function confidenceBadge(conf){
  if(conf==null||Number.isNaN(conf))return '';
  const pct=Math.round(conf*100);
  return '<span class="confidence-badge '+(conf<0.6?'uncertain':'')+'" title="Model confidence">'+pct+'%</span>';
}

function queueItems(job){
  const items=[];
  (job.review_intervals||[]).forEach(x=>{
    const reasons=x.reasons||[];
    const label=(reasons.find(r=>String(r).startsWith('vision-review:'))||'uncertain verdict').replace('vision-review:','');
    items.push({kind:'review',start:Number(x.start),end:Number(x.end),label:'Uncertain cut: '+label,confidence:parseConfidence(reasons)});
  });
  (job.model_disagreements||[]).forEach(d=>{
    const half=(Number(d.sample_interval)||2)/2;
    items.push({kind:'disagreement',start:Math.max(0,Number(d.timestamp)-half),end:Number(d.timestamp)+half,
      label:'Model kept, heuristic flagged '+d.heuristic_suggests,confidence:d.confidence==null?null:Number(d.confidence),note:d.description});
  });
  return items.sort((a,b)=>a.start-b.start);
}

function reviewQueue(job){
  const items=queueItems(job);
  if(!items.length)return '';
  const rows=items.map((x,i)=>'<div class="queue-row"><button class="queue-main" data-queue-seek="'+i+'"><span class="queue-time">'+seconds(x.start)+'–'+seconds(x.end)+'</span><span class="queue-label">'+esc(x.label)+'</span>'+confidenceBadge(x.confidence)+'</button><span class="queue-actions"><button class="button small-btn" data-queue-decision="keep" data-queue-index="'+i+'">Keep</button><button class="button small-btn cut" data-queue-decision="cut" data-queue-index="'+i+'">Cut</button></span></div>').join('');
  return '<section class="review-queue"><div class="timeline-head"><h2>Needs your eyes</h2><span>'+items.length+' uncertain verdict'+(items.length===1?'':'s')+' — Keep or Cut to resolve</span></div><div class="queue-list">'+rows+'</div></section>';
}

async function resolveQueueItem(job,index,decision){
  const item=queueItems(job)[index];
  if(!item)return;
  state.selection={start:round(item.start),end:round(item.end)};
  state.playhead=state.selection.start;
  clearEditing();
  updateSelectionInputs();
  renderSelectionOverlay();
  seekTo(state.playhead);
  await saveDecision(job,decision,'review queue: '+item.label);
}

async function exportOtio(job){
  try{
    const res=await api('/api/jobs/'+encodeURIComponent(job.id)+'/export-otio',{method:'POST'});
    toast('Timeline exported — opens in Resolve or Premiere');
    const a=document.createElement('a');
    a.href=apiFile(res.path);
    a.download='timeline.otio';
    document.body.append(a);
    a.click();
    a.remove();
    await load({quiet:true});
  }catch(error){toast(error.message,'danger')}
}

let isDraggingSeek=false;
let isTrimmingReview=false;
function bind(job){
  if(!job)return;
  $$('[data-job]').forEach(b=>b.addEventListener('click',()=>{
    stopPlayback();
    state.selectedJobId=b.dataset.job;
    state.selectedMedia='source';
    state.playhead=0;
    state.selection={start:0,end:2};
    clearEditing();
    render();
  }));
  $('[data-action="refresh"]')?.addEventListener('click',()=>load());
  $$('[data-media]').forEach(b=>b.addEventListener('click',()=>{
    stopPlayback();
    state.selectedMedia=b.dataset.media;
    render();
  }));
  const video=$('#review-video');
  video?.addEventListener('timeupdate',()=>{
    state.playhead=video.currentTime;
    const read=$('#time-readout');
    if(read)read.textContent=seconds(state.playhead)+' / '+seconds(job.duration);
    $$('.playhead').forEach(n=>n.style.left=((state.playhead/job.duration)*100)+'%');
    const seek=$('#review-seek');
    if(seek&&!isDraggingSeek)seek.value=String(state.playhead);
  });

  let isDraggingTimeline=false;
  function scrubFromEvent(event,rail){
    const rect=rail.getBoundingClientRect();
    const ratio=Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width));
    seekTo(ratio*Number(job.duration));
  }

  $$('[data-timeline]').forEach(rail=>{
    rail.addEventListener('mousedown',e=>{
      if(e.target.closest('[data-review-kind],[data-seek]'))return;
      clearEditing();
      isDraggingTimeline=true;
      scrubFromEvent(e,rail);
      const onMove=ev=>{if(isDraggingTimeline)scrubFromEvent(ev,rail)};
      const onUp=()=>{
        isDraggingTimeline=false;
        window.removeEventListener('mousemove',onMove);
        window.removeEventListener('mouseup',onUp);
      };
      window.addEventListener('mousemove',onMove);
      window.addEventListener('mouseup',onUp);
    });
  });

  $$('[data-seek]').forEach(marker=>{
    const go=event=>{event.stopPropagation();seekTo(Number(marker.dataset.seek))};
    marker.addEventListener('click',go);
    marker.addEventListener('keydown',event=>{
      if(event.key!=='Enter'&&event.key!==' ')return;
      event.preventDefault();
      go(event);
    });
  });
  $$('[data-queue-seek]').forEach(b=>b.addEventListener('click',()=>{
    const item=queueItems(job)[Number(b.dataset.queueSeek)];
    if(!item)return;
    state.selection={start:round(item.start),end:round(item.end)};
    state.playhead=state.selection.start;
    clearEditing();
    updateSelectionInputs();
    renderSelectionOverlay();
    seekTo(state.playhead);
  }));
  $$('[data-queue-decision]').forEach(b=>b.addEventListener('click',()=>resolveQueueItem(job,Number(b.dataset.queueIndex),b.dataset.queueDecision)));
  $$('[data-action="export-otio"]').forEach(b=>b.addEventListener('click',()=>exportOtio(job)));
  $$('[data-review-kind]').forEach(span=>span.addEventListener('click',event=>{
    if(isTrimmingReview)return;
    event.stopPropagation();
    editReviewInterval(job,span.dataset.reviewKind,Number(span.dataset.reviewIndex));
  }));
  $$('[data-review-kind]').forEach(span=>span.addEventListener('keydown',event=>{
    if(event.key!=='Enter'&&event.key!==' ')return;
    event.preventDefault();
    editReviewInterval(job,span.dataset.reviewKind,Number(span.dataset.reviewIndex));
  }));
  $$('[data-trim-edge]').forEach(handle=>handle.addEventListener('mousedown',event=>{
    event.preventDefault();
    event.stopPropagation();
    const span=handle.closest('[data-review-kind]'),rail=handle.closest('[data-timeline]');
    const index=Number(span?.dataset.reviewIndex),interval=(review(job).cut_intervals||[])[index];
    if(!span||!rail||!interval)return;
    isTrimmingReview=true;
    state.editing={kind:'cut',index};
    state.selection={start:round(interval.start),end:round(interval.end)};
    const edge=handle.dataset.trimEdge,duration=Number(job.duration||1);
    const update=move=>{
      const rect=rail.getBoundingClientRect(),ratio=Math.max(0,Math.min(1,(move.clientX-rect.left)/rect.width)),time=round(ratio*duration);
      if(edge==='start')state.selection.start=Math.min(time,round(state.selection.end-.1));
      else state.selection.end=Math.max(time,round(state.selection.start+.1));
      const left=(state.selection.start/duration)*100,width=Math.max(.25,((state.selection.end-state.selection.start)/duration)*100);
      span.style.left=left+'%';
      span.style.width=width+'%';
      updateSelectionInputs();
      renderSelectionOverlay();
    };
    const finish=()=>{
      window.removeEventListener('mousemove',update);
      window.removeEventListener('mouseup',finish);
      saveDecision(job,'cut',interval.reason||'editorial review');
      setTimeout(()=>{isTrimmingReview=false},0);
    };
    window.addEventListener('mousemove',update);
    window.addEventListener('mouseup',finish);
  }));

  $('#set-in-btn')?.addEventListener('click',()=>setInPoint());
  $('#set-out-btn')?.addEventListener('click',()=>setOutPoint());
  $('#cut-start')?.addEventListener('change',event=>{state.selection.start=round(event.target.value);clamp(job);renderSelectionOverlay();updateSelectionInputs()});
  $('#cut-end')?.addEventListener('change',event=>{state.selection.end=round(event.target.value);clamp(job);renderSelectionOverlay();updateSelectionInputs()});
  $$('[data-decision]').forEach(b=>b.addEventListener('click',()=>saveDecision(job,b.dataset.decision)));
  $('[data-action="remove-decision"]')?.addEventListener('click',()=>removeEditingDecision(job));
  $$('[data-action="render"]').forEach(b=>b.addEventListener('click',()=>startRender(job)));
}

async function saveDecision(job,decision,reasonOverride=null){
  const r=review(job),reason=reasonOverride??($('#cut-reason')?.value.trim()||'editorial review');
  const interval={start:state.selection.start,end:state.selection.end,reason:decision==='protect'?'protected':reason};
  if(!(interval.end>interval.start))return toast('End needs to be after start.','danger');
  const kind=decision==='cut'?'cut':'keep';
  const property=kind==='cut'?'cut_intervals':'keep_intervals';
  const intervals=[...(r[property]||[])];
  if(state.editing?.kind===kind&&Number.isInteger(state.editing.index)&&intervals[state.editing.index]){
    intervals[state.editing.index]=interval;
    r[property]=intervals.filter((item,index)=>index===state.editing.index||round(item.start)!==round(interval.start)||round(item.end)!==round(interval.end));
  }else{
    r[property]=[...intervals.filter(item=>round(item.start)!==round(interval.start)||round(item.end)!==round(interval.end)),interval];
  }
  try{
    await api('/api/jobs/'+encodeURIComponent(job.id)+'/review',{method:'POST',body:JSON.stringify(r)});
    toast(state.editing?'decision updated':decision+' saved');
    clearEditing();
    await load({quiet:true});
  }catch(error){toast(error.message,'danger')}
}

async function removeEditingDecision(job){
  if(!state.editing)return;
  const r=review(job),property=state.editing.kind==='cut'?'cut_intervals':'keep_intervals',intervals=[...(r[property]||[])];
  if(!intervals[state.editing.index])return;
  intervals.splice(state.editing.index,1);
  r[property]=intervals;
  try{
    await api('/api/jobs/'+encodeURIComponent(job.id)+'/review',{method:'POST',body:JSON.stringify(r)});
    clearEditing();
    toast('decision removed');
    await load({quiet:true});
  }catch(error){toast(error.message,'danger')}
}

async function startRender(job){
  try{
    const task=await api('/api/jobs/'+encodeURIComponent(job.id)+'/render',{method:'POST',body:JSON.stringify({})});
    toast('Render started: '+task.label);
    pollTask(task.id);
  }catch(error){toast(error.message,'danger')}
}

async function pollTask(id){
  for(let i=0;i<120;i+=1){
    await new Promise(resolve=>setTimeout(resolve,1500));
    const task=await api('/api/tasks/'+id);
    if(task.status==='running')continue;
    toast(task.status==='succeeded'?'Render complete':(task.error||'Render failed'),task.status==='succeeded'?'info':'danger');
    return load({quiet:true});
  }
}

function applyReviewRotation(){
  const video=$('#review-video'),wrap=video?.parentElement;
  if(!video||!wrap)return;
  const turned=state.rotation%180!==0,rect=wrap.getBoundingClientRect(),scale=turned?Math.min(rect.width/rect.height,rect.height/rect.width):1;
  video.style.transform='rotate('+state.rotation+'deg) scale('+scale+')';
}

function syncTransport(){
  const video=$('#review-video'),play=$('[data-action="playback"]'),seek=$('#review-seek'),time=$('#transport-time');
  if(!video)return;
  if(play)play.textContent=video.paused?'play':'pause';
  if(seek&&!isDraggingSeek)seek.value=String(video.currentTime||0);
  if(time)time.textContent=seconds(video.currentTime)+' / '+seconds(video.duration||selected()?.duration);
}

function ensureTransportControls(){
  const video=$('#review-video'),player=$('.player');
  if(!video||!player)return;
  video.controls=false;
  let transport=$('.review-transport',player);
  if(!transport){
    transport=document.createElement('div');
    transport.className='review-transport';
    transport.innerHTML='<button class="transport-button" type="button" data-action="playback">play</button><input id="review-seek" type="range" min="0" step="0.01" aria-label="Playback position"><span id="transport-time"></span><button class="transport-button" type="button" data-action="mute">mute</button>';
    player.insertBefore(transport,$('.player-controls',player));
    video.addEventListener('timeupdate',syncTransport);
    video.addEventListener('play',syncTransport);
    video.addEventListener('pause',syncTransport);
    video.addEventListener('loadedmetadata',()=>{ensureTransportControls();applyReviewRotation()});
  }
  const seek=$('#review-seek');
  if(seek){
    seek.max=String(video.duration||selected()?.duration||1);
    if(!seek.dataset.bound){
      seek.dataset.bound='true';
      seek.addEventListener('mousedown',()=>{isDraggingSeek=true});
      seek.addEventListener('input',e=>{seekTo(Number(e.target.value))});
      seek.addEventListener('change',e=>{isDraggingSeek=false;seekTo(Number(e.target.value))});
    }
  }
  syncTransport();
}

function ensureRotateControl(){
  ensureTransportControls();
  const controls=$('.media-switch');
  if(!controls||controls.querySelector('[data-rotate-view]'))return;
  const button=document.createElement('button');
  button.className='rotate-button';
  button.dataset.rotateView='true';
  button.type='button';
  button.title='Rotate the review view 90 degrees';
  button.textContent='rotate '+state.rotation+'°';
  controls.append(button);
  applyReviewRotation();
}

new MutationObserver(ensureRotateControl).observe(app,{childList:true});

document.addEventListener('click',event=>{
  const rotate=event.target.closest('[data-rotate-view]');
  if(rotate){
    state.rotation=(state.rotation+90)%360;
    rotate.textContent='rotate '+state.rotation+'°';
    applyReviewRotation();
    return;
  }
  const action=event.target.closest('[data-action]')?.dataset.action,video=$('#review-video');
  if(!video)return;
  if(action==='playback'){
    video.paused?video.play():video.pause();
    syncTransport();
  }
  if(action==='mute'){
    video.muted=!video.muted;
    event.target.textContent=video.muted?'unmute':'mute';
  }
});

document.addEventListener('keydown',event=>{
  const active=document.activeElement;
  if(active&&(active.tagName==='INPUT'||active.tagName==='TEXTAREA'||active.isContentEditable))return;
  const job=selected();
  if(!job)return;
  const video=$('#review-video');
  const key=event.key.toLowerCase();
  if(event.code==='Space'||key===' '){
    event.preventDefault();
    if(video){
      video.paused?video.play():video.pause();
      syncTransport();
    }
  }else if(key==='i'){
    event.preventDefault();
    setInPoint();
    toast('Mark In: '+seconds(state.selection.start));
  }else if(key==='o'){
    event.preventDefault();
    setOutPoint();
    toast('Mark Out: '+seconds(state.selection.end));
  }else if(key==='c'){
    event.preventDefault();
    saveDecision(job,'cut');
  }else if(key==='k'){
    event.preventDefault();
    saveDecision(job,'keep');
  }else if(key==='p'){
    event.preventDefault();
    saveDecision(job,'protect');
  }else if(key==='arrowleft'){
    event.preventDefault();
    const step=event.shiftKey?1.0:0.1;
    seekTo(state.playhead-step);
  }else if(key==='arrowright'){
    event.preventDefault();
    const step=event.shiftKey?1.0:0.1;
    seekTo(state.playhead+step);
  }else if(key==='j'){
    event.preventDefault();
    seekTo(state.playhead-1.0);
  }else if(key==='l'){
    event.preventDefault();
    seekTo(state.playhead+1.0);
  }
});

window.addEventListener('resize',applyReviewRotation);
load();
