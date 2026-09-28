import * as T from './timeline.mjs';
import * as A from './auto.mjs';
import * as Theme from './theme.mjs';
import {icon} from './icons.mjs';

const $ = (q, root=document) => root.querySelector(q);
const $$ = (q, root=document) => [...root.querySelectorAll(q)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const basename = p => String(p || '').split(/[\\/]/).pop();
const fileURL = p => '/api/file?path=' + encodeURIComponent(String(p).replace(/\\/g,'/'));
const round = n => Math.round(n*1000)/1000;
const tc = n => {const f=Math.round(Math.max(0,n||0)*(state.sequence?.fps||30)), fps=state.sequence?.fps||30; return [Math.floor(f/fps/3600),Math.floor(f/fps/60)%60,Math.floor(f/fps)%60,f%fps].map(v=>String(v).padStart(2,'0')).join(':');};
const short = n => `${Math.floor((n||0)/60)}:${String(Math.floor((n||0)%60)).padStart(2,'0')}`;
const kindOf = p => /\.(mp3|wav|m4a|aac|flac|ogg)$/i.test(p) ? 'audio' : /\.(png|jpe?g|webp|bmp)$/i.test(p) ? 'image' : 'video';
const state = {jobs:[],inbox:[],config:{},schema:{},job:null,source:null,sourceInfo:null,sequence:null,selected:null,
  project:null,projects:[],selection:new Set(),selectionRequest:0,
  bin:'all',view:'list',search:'',sort:'name',checked:new Set(),tab:'effects',focus:'source',sourceTime:0,time:0,
  in:0,out:2,zoom:1,snap:true,linked:true,tool:'select',dirty:false,editVersion:0,undo:[],redo:[],saving:null,
  gateway:'untested',tasks:[],taskCallbacks:new Map(),handled:new Set(),programMode:'sequence',playing:false,shuttle:0,
  error:null,retry:null,reviewEditing:null,loading:false,meta:new Map(),waves:new Map(),proposal:-1,
  prefs:{lanes:true,cc:true,hud:true,auto:{fromPlan:false,waste:true,silence:true,threshold:-40,minSilence:.6,pad:.12,gaps:true,scenes:false,highlights:true,highlightCount:5}}};
try{const saved=JSON.parse(localStorage.getItem('anna.cutroom.prefs')||'{}');state.prefs={...state.prefs,...saved,auto:{...state.prefs.auto,...saved.auto}};}catch{}
const savePrefs=()=>{try{localStorage.setItem('anna.cutroom.prefs',JSON.stringify(state.prefs));}catch{}};
let saveTimer, reverseTimer, previewTimer, previewToken=0, overlayKey='', clockLast=0, dialogFocus=null;
const kindIcons={video:'film',audio:'music',image:'image'};
const button=(action,label,cls='',extra='')=>`<button type="button" class="${cls}" data-action="${action}" ${extra}>${label}</button>`;
const withIcon=(name,label)=>`${icon(name)}<span>${label}</span>`;
const lightQuery=window.matchMedia('(prefers-color-scheme: light)');
state.themeMode=(()=>{try{return Theme.normalize(localStorage.getItem(Theme.THEME_KEY));}catch{return 'auto';}})();
const badge=(label,kind='')=>`<span class="badge ${kind}"><i></i>${esc(label)}</span>`;
const jobURL = suffix => '/api/jobs/'+encodeURIComponent(state.job.id)+'/'+suffix;
const projectURL = suffix => '/api/projects/'+encodeURIComponent(state.project.id)+(suffix?'/'+suffix:'');
const pathKey = path => String(path).replace(/\\/g,'/');

async function api(path, options={}) {
  const response=await fetch(path,{...options,headers:{...(options.body instanceof FormData?{}:{'Content-Type':'application/json'}),...options.headers}});
  const data=await response.json().catch(()=>({}));
  if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||response.statusText));
  return data;
}
const post=(path,body={})=>api(path,{method:'POST',body:JSON.stringify(body)});
function toast(message,kind='info') {
  const item=document.createElement('div');item.className='toast '+kind;
  item.innerHTML=`${icon({ok:'circle-check',error:'circle-x',warn:'triangle-alert'}[kind]||'info')}<div class="toast-body"></div><button type="button" class="toast-close" aria-label="Dismiss notification">${icon('x')}</button>`;
  item.querySelector('.toast-body').textContent=message;item.querySelector('.toast-close').addEventListener('click',()=>item.remove());
  $('#toasts').append(item);setTimeout(()=>item.remove(),6500);
}
// Auto follows the OS live; the head script in index.html already applied the first paint.
function applyThemeNow(){
  Theme.applyTheme(document.documentElement,state.themeMode,lightQuery.matches);
  const toggle=$('#theme-toggle'),title=`Theme: ${Theme.label(state.themeMode)} · click for ${Theme.label(Theme.nextMode(state.themeMode))}`;
  if(toggle){toggle.innerHTML=icon(Theme.iconFor(state.themeMode));toggle.title=title;toggle.setAttribute('aria-label',title);}
  $$('[data-theme-option]').forEach(item=>item.setAttribute('aria-checked',item.dataset.themeOption===state.themeMode));
  if($('#timeline-canvas'))renderTimeline();
}
function setTheme(mode){state.themeMode=Theme.normalize(mode);try{localStorage.setItem(Theme.THEME_KEY,state.themeMode);}catch{}applyThemeNow();}
function syncToggles(){
  for(const [action,on] of [['toggle-lanes',state.prefs.lanes],['toggle-cc',state.prefs.cc],['toggle-hud',state.prefs.hud]])
    $$(`[data-action="${action}"]`).forEach(b=>{if(b.closest('.menu-content'))b.setAttribute('aria-checked',on);else{b.classList.toggle('active',on);b.setAttribute('aria-pressed',on);}});
}
function fail(error,retry=null) {
  state.error=error.message||String(error); state.retry=retry;
  $('#error-banner').hidden=false;
  $('#error-message').textContent=state.error;
  $('#retry-error').hidden=!retry;
  toast(state.error,'error');
}
function clearError(){state.error=null;$('#error-banner').hidden=true;}
async function operation(label, fn, retry=fn) {
  try {clearError();return await fn();} catch(error){fail(error,retry);return null;}
}

function shell(){
  $('#app').innerHTML=`
    <header class="app-header">
      <a class="brand" href="/" aria-label="SmartCut home"><img class="brand-mark" src="/favicon.png" alt="" width="32" height="32"><span class="brand-copy"><span class="brand-name">SmartCut</span><span class="brand-suite">Cutroom</span></span></a>
      <nav class="menus" aria-label="Application menu">
        ${menu('File',[['new-project','New project…'],['open-project','Open project…'],['import','Import media…'],['transcribe','Re-transcribe source (.srt)'],['folder','Open output folder']])}
        ${menu('Project',[['new-project','New project…'],['open-project','Open project…'],['settings','Pipeline settings…'],['refresh','Refresh media']])}
        ${menu('Sequence',[['save','Save sequence'],['undo','Undo'],['redo','Redo'],['rebuild','Load approved plan'],['reel','Best-of reel…']])}
        ${menu('Markers',[['in','Mark In · I'],['out','Mark Out · O'],['marker','Add sequence marker · M'],['auto-scenes','Markers at scene changes'],['auto-highlights','Markers at AI highlights'],['clear-markers','Clear all markers']])}
        ${menu('Auto',[['auto-edit','Auto-edit sequence…'],['auto-waste','Remove AI-flagged waste'],['auto-silence','Remove silences…'],['auto-gaps','Close all gaps'],['auto-fill','Fill frame'],['next-proposal','Next AI proposal · N']])}
        ${menu('Export',[['export-edl','EDL cut list'],['export-csv','CSV edit list'],['export-otio','OpenTimelineIO'],['export-srt','Captions for this edit (.srt)'],['export-mp4','Rendered MP4']])}
        ${menu('View',[...Theme.MODES.map(mode=>[`theme-${mode}`,`Theme · ${Theme.label(mode)}`,`role="menuitemradio" data-theme-option="${mode}" aria-checked="${mode===state.themeMode}"`]),['-'],['toggle-lanes','AI lanes on timeline','role="menuitemcheckbox"'],['toggle-cc','Live captions','role="menuitemcheckbox"'],['toggle-hud','AI verdict overlay','role="menuitemcheckbox"']])}
        ${menu('Help',[['shortcuts','Keyboard shortcuts · ?']])}
      </nav>
      <div class="header-status"><span id="model-label"></span><span id="gateway-status">${badge('Not tested')}</span>${button('connection',withIcon('plug','Test connection'),'quiet')}</div>
      ${button('theme-cycle',icon(Theme.iconFor(state.themeMode)),'icon-button','id="theme-toggle"')}
      ${button('settings',icon('settings'),'icon-button','aria-label="Pipeline settings" title="Pipeline settings"')}
    </header>
    <div id="error-banner" class="error-banner" role="alert" hidden>${icon('triangle-alert')}<span id="error-message"></span>${button('retry',withIcon('refresh-cw','Retry'),'danger','id="retry-error"')}${button('dismiss-error',icon('x'),'icon-button','aria-label="Dismiss error"')}</div>
    <div class="workspace-bar"><div class="page-title"><span class="page-icon" aria-hidden="true">${icon('clapperboard')}</span><div><span class="workspace-name">Editing</span><div class="page-heading"><h1 id="project-name">Untitled project</h1><span id="save-state"></span></div></div></div><div class="pipeline-actions">${button('import',withIcon('upload','Import'))}${button('analyze',withIcon('sparkles','Analyze'),'accent')}${button('auto-edit',withIcon('zap','Auto-edit'),'accent','title="Apply AI cuts, silence removal and markers as one undoable edit"')}${button('replan',withIcon('refresh-cw','Re-plan with my decisions'))}${button('preview',withIcon('film','Preview reel…'))}${button('render',withIcon('circle-check','Render approved cut'),'primary')}</div></div>
    <main class="workspace" id="review">
      <aside class="panel project-panel" aria-label="Project panel"><div class="project-switcher">${button('new-project',withIcon('plus','New project'),'primary')}${button('open-project',withIcon('folder-open','Open…'))}</div>
        <div class="panel-title"><h2>${icon('folder')}Project assets</h2><span id="asset-count"></span></div>
        <div class="asset-actions">${button('import',withIcon('upload','Import media'),'wide')}${button('add-existing','Add existing media…','quiet')}${button('remove-asset','Remove selected asset','quiet','title="Remove from this project; keep the original file"')}</div>
        <div class="bin-tree" role="group" aria-label="Asset filters">${[['all','All assets','layers'],['video','Video','film'],['audio','Audio','music'],['image','Images','image']].map(([key,label,name])=>`<button data-bin="${key}" class="${key==='all'?'active':''}">${icon(name)}<span>${label}</span><small id="count-${key}"></small></button>`).join('')}</div>
        <div class="project-tools"><div class="search-field">${icon('search')}<input id="media-search" type="search" aria-label="Search project" placeholder="Search media…"></div><div class="segmented">${button('list-view',icon('list'),'active','aria-label="List view" title="List view"')}${button('icon-view',icon('layout-grid'),'','aria-label="Icon view" title="Icon view"')}</div></div>
        <div class="media-table-head"><button data-sort="name">Name</button><button data-sort="duration">Duration</button></div>
        <div id="media-list" class="media-list" tabindex="0" aria-label="Project media"></div>
        <footer class="project-footer"><span id="media-footer">Drop media to import</span>${button('reel','Build reel','quiet')}</footer>
      </aside>
      <section class="monitors-panel" aria-label="Source and Program monitors">
        <section class="panel monitor" id="source-monitor" tabindex="0" aria-label="Source monitor"><div class="panel-title"><h2><span class="tab-indicator"></span>Source</h2><span id="source-name">No source selected</span></div><div class="monitor-stage" id="source-stage"></div>
          <div class="time-strip"><output id="source-time">00:00:00:00</output><span id="source-format">SOURCE</span><output id="source-duration">00:00:00:00</output></div>
          <input type="range" id="source-scrub" class="monitor-scrub" aria-label="Scrub source" min="0" max="1" step=".01" value="0">
          <div class="evidence-strip" id="source-evidence" hidden></div>
          <div class="transport">${button('source-start',icon('skip-back'),'icon-button','aria-label="Source start" title="Go to start"')}${button('source-back',icon('chevron-left'),'icon-button','aria-label="Source previous frame" title="Previous frame"')}${button('source-play',icon('play'),'play-button','aria-label="Play source" id="source-play"')}${button('source-next',icon('chevron-right'),'icon-button','aria-label="Source next frame" title="Next frame"')}<span class="transport-divider"></span>${button('in','I','icon-button mark-button','title="Mark In · I" aria-label="Mark In"')}${button('out','O','icon-button mark-button','title="Mark Out · O" aria-label="Mark Out"')}${button('rotate-source',icon('rotate-cw'),'icon-button','title="Rotate source monitor only" aria-label="Rotate source monitor"')}</div>
          <div class="source-edit"><span id="source-range">I — &nbsp; O —</span>${button('insert',withIcon('plus','Insert'),'quiet','title="Insert marked source on V1/A1"')}${button('overwrite',withIcon('layers','Overwrite'),'quiet','title="Overwrite marked source on V1/A1"')}</div>
        </section>
        <section class="panel monitor" id="program-monitor" tabindex="0" aria-label="Program monitor"><div class="panel-title"><h2><span class="tab-indicator"></span>Program</h2><select id="program-mode" aria-label="Program preview mode"><option value="sequence">Live sequence</option><option value="final">Rendered output</option><option value="preview">Preview reel</option></select></div><div class="monitor-stage" id="program-stage"></div>
          <div class="time-strip"><output id="program-time">00:00:00:00</output><span id="program-format">SEQUENCE</span><output id="program-duration">00:00:00:00</output></div>
          <input type="range" id="program-scrub" class="monitor-scrub" aria-label="Scrub program" min="0" max="1" step=".01" value="0">
          <div class="transport">${button('program-start',icon('skip-back'),'icon-button','aria-label="Program start" title="Go to start"')}${button('program-back',icon('chevron-left'),'icon-button','aria-label="Program previous frame" title="Previous frame"')}${button('program-play',icon('play'),'play-button','id="program-play" aria-label="Play program"')}${button('program-next',icon('chevron-right'),'icon-button','aria-label="Program next frame" title="Next frame"')}<span class="transport-divider"></span>${button('marker',icon('diamond'),'icon-button','aria-label="Add sequence marker" title="Add marker · M"')}${button('toggle-cc',icon('captions'),`icon-button ${state.prefs.cc?'active':''}`,`aria-pressed="${state.prefs.cc}" aria-label="Live captions" title="Live captions from the transcript"`)}${button('toggle-hud',icon('bot'),`icon-button ${state.prefs.hud?'active':''}`,`aria-pressed="${state.prefs.hud}" aria-label="AI verdict overlay" title="Show the AI verdict for the frame under the playhead"`)}${button('folder',icon('folder-open'),'icon-button','aria-label="Open output folder" title="Open output folder"')}</div>
          <div class="source-edit"><span id="program-note">Edits are previewed live</span><span class="keyboard-hint">J / K / L &nbsp; shuttle</span></div>
        </section>
      </section>
      <aside class="panel inspector-panel" aria-label="Inspector"><div class="inspector-tabs" role="tablist">${[['effects','Effects','sliders-horizontal'],['review','Review','list-checks'],['pipeline','Pipeline','workflow']].map(([key,label,name])=>`<button role="tab" aria-selected="${key==='effects'}" data-tab="${key}">${icon(name)}<span>${label}</span></button>`).join('')}</div><div id="inspector-content"></div></aside>
      <section class="panel timeline-panel" aria-label="Timeline"><div class="timeline-heading"><h2>${icon('clapperboard')}Sequence <span id="sequence-name">01</span></h2><div class="timeline-tools">${button('select-tool',icon('mouse-pointer-2'),'icon-button active','title="Selection tool · V" aria-label="Selection tool"')}${button('razor-tool',icon('scissors'),'icon-button','title="Razor tool" aria-label="Razor tool"')}${button('split',withIcon('scissors-line-dashed','Split'),'quiet','title="Split at playhead · C"')}${button('ripple',withIcon('arrow-left-to-line','Ripple'),'quiet','title="Ripple delete · Shift+Delete"')}<span class="transport-divider"></span>${button('snap',withIcon('magnet','Snap'),'quiet active','aria-pressed="true"')}${button('linked',withIcon('link','Linked'),'quiet active','aria-pressed="true"')}${button('toggle-lanes',withIcon('sparkles','AI lanes'),`quiet ${state.prefs.lanes?'active':''}`,`aria-pressed="${state.prefs.lanes}" title="Show AI scores, flags, scenes and transcript on the timeline"`)}${button('undo',icon('undo-2'),'icon-button','aria-label="Undo" title="Undo · Ctrl+Z"')}${button('redo',icon('redo-2'),'icon-button','aria-label="Redo" title="Redo · Ctrl+Y"')}<label class="zoom-control">${icon('zoom-out')}<input type="range" id="timeline-zoom" aria-label="Timeline zoom" min="1" max="60" step=".25" value="1">${icon('zoom-in')}</label>${button('save',withIcon('save','Save'),'quiet','title="Save sequence · Ctrl+S"')}</div></div>
        <div class="timeline-body"><div class="track-headers"><div class="ruler-head" id="timeline-time">00:00:00:00</div><div id="track-headers"></div></div><div class="timeline-scroll" id="timeline-scroll"><div id="timeline-canvas" tabindex="0" aria-label="Timeline tracks"></div></div></div>
        <footer class="timeline-footer"><span id="timeline-summary">Select a sequence to start editing</span><span>Space Play &nbsp; C Split &nbsp; I / O Mark &nbsp; N Next AI proposal &nbsp; ? Shortcuts</span></footer>
      </section>
    </main><footer class="statusbar"><span id="backend-status">Connecting…</span><button data-action="tasks" id="tasks-status">No active tasks</button><span>LOCAL WORKSPACE <span class="status-dot"></span></span></footer>
    <input type="file" id="file-input" multiple accept=".mp4,.mov,.mkv,.avi,.webm,.mp3,.wav,.m4a,.aac,.flac,.ogg,.png,.jpg,.jpeg,.webp,.bmp" hidden>
    <dialog id="dialog"></dialog><div id="context-menu" role="menu" hidden></div><div id="toasts" class="toast-region" role="status" aria-live="polite"></div>`;
  renderSource();renderProgram();renderTimeline();renderInspector();
}
function menu(label,items){return `<details class="menu"><summary>${label}</summary><div class="menu-content" role="menu">${items.map(([a,l,extra='role="menuitem"'])=>a==='-'?'<hr>':button(a,l,'',extra)).join('')}</div></details>`;}
function libraryAssets(){
  const map=new Map(state.inbox.map(f=>[f.path.replace(/\\/g,'/').toLowerCase(),{...f,kind:kindOf(f.path)}]));
  state.jobs.forEach(job=>{if(job.source){const key=job.source.replace(/\\/g,'/').toLowerCase();map.set(key,{...map.get(key),path:job.source,name:basename(job.source),kind:kindOf(job.source),duration:job.duration,job});}});
  return [...map.values()];
}
function assets(){return (state.project?.assets||[]).map(f=>({...f,kind:kindOf(f.path),job:state.jobs.find(j=>pathKey(j.source)===pathKey(f.path))}));}
function renderProject(){
  const all=assets();let rows=all.filter(f=>(state.bin==='all'||f.kind===state.bin)&&f.name.toLowerCase().includes(state.search.toLowerCase()));
  rows.sort((a,b)=>state.sort==='duration'?(b.duration||0)-(a.duration||0):a.name.localeCompare(b.name,undefined,{numeric:true}));
  $('#asset-count').textContent=all.length+' items';
  for(const bin of ['all','video','audio','image'])$('#count-'+bin).textContent=all.filter(f=>bin==='all'||f.kind===bin).length;
  $$('.bin-tree button').forEach(b=>b.classList.toggle('active',b.dataset.bin===state.bin));
  const list=$('#media-list');list.className='media-list '+(state.view==='icons'?'icon-view':'');
  list.innerHTML=rows.length?rows.map(f=>`<div class="media-row ${state.source===f.path?'selected':''}" data-source="${esc(f.path)}" draggable="true" role="button" tabindex="0" title="${esc(f.path)}">
    ${f.job?`<input type="checkbox" class="reel-check" data-job-check="${esc(f.job.id)}" aria-label="Select ${esc(f.name)} for reel" ${state.checked.has(f.job.id)?'checked':''}>`:'<span class="asset-spacer"></span>'}<span class="media-icon ${f.kind}">${icon(kindIcons[f.kind])}</span><span class="asset-info"><strong>${esc(f.name)}</strong><small>${f.job?badge(f.job.status,f.job.status==='rendered'?'ok':''):esc(f.kind.toUpperCase())} ${f.source_available===false?'<span class="offline">Offline</span>':''}</small></span><span class="asset-duration">${f.duration?short(f.duration):f.size?(f.size/1048576).toFixed(1)+' MB':'—'}</span></div>`).join(''):`<div class="empty-state"><span class="empty-glyph">${icon(state.search?'search':'folder-open')}</span><h3>${state.search?'No matching media':state.project?'Import your media':'Start a project'}</h3><p>${state.search?'Try another search.':state.project?'Add video, audio or images, then drag an asset onto the timeline.':'Create or open a project above. Its assets and timeline stay together.'}</p>${button(state.project?'import':'new-project',withIcon(state.project?'upload':'plus',state.project?'Import media':'New project'),'primary')}</div>`;
  $('#media-footer').textContent=state.checked.size?state.checked.size+' sources selected for reel':state.project?'Drag assets to the timeline · analysis optional':'Create or open a project to begin';
  $$('[data-action="list-view"],[data-action="icon-view"]').forEach(b=>b.classList.toggle('active',b.dataset.action===(state.view==='list'?'list-view':'icon-view')));
}
async function load({initial=false}={}){
  const [health,config,schema,jobs,inbox,projects]=await Promise.all(['/api/health','/api/config','/api/config/schema','/api/jobs','/api/inbox','/api/projects'].map(p=>api(p)));
  state.config=config;state.schema=schema;state.jobs=jobs;state.inbox=inbox;state.projects=projects;
  $('#backend-status').innerHTML=badge(health.ok?'Backend connected':'Backend offline',health.ok?'ok':'error');
  $('#model-label').textContent=config.vision_model;$('#model-label').title=config.lm_studio_url;
  if(state.job)state.job=jobs.find(j=>j.id===state.job.id)||state.job;
  if(state.project)state.project=projects.find(p=>p.id===state.project.id)||state.project;
  renderProject();renderInspector();renderSourceEvidence();
  if(initial){let id;try{id=localStorage.getItem('smartcut.project');}catch{}if(projects.some(p=>p.id===id))await openProject(id);else{renderSource();renderProgram();renderTimeline();updateTitles();}}
}
async function metadata(path){if(!state.meta.has(path))state.meta.set(path,await api('/api/media?path='+encodeURIComponent(path)));return state.meta.get(path);}
async function selectAsset(path){
  const request=++state.selectionRequest;pause();state.source=path;state.sourceInfo=null;state.sourceTime=0;state.in=0;state.out=0;state.reviewEditing=null;state.rotation=0;state.proposal=-1;
  state.job=state.jobs.find(j=>pathKey(j.source)===pathKey(path))||null;
  renderProject();renderSource();renderTimeline();renderInspector();updateTitles();renderSourceEvidence();
  if(state.job?.source_available===false||assets().find(a=>a.path===path)?.source_available===false)return toast('Original media is offline.','warn');
  const info=await metadata(path);if(request!==state.selectionRequest)return;
  state.sourceInfo=info;state.out=info.duration;
  syncTransport();renderInspector();updateTitles();
}
function newProjectDialog(){openDialog(`${dialogHead('PROJECT','New project')}<form id="new-project-form"><div class="dialog-body"><label class="stacked">Project name<input name="name" required maxlength="100" placeholder="My edit" autofocus></label><p>Create a project, import media, then drag assets onto the timeline. Analysis is optional.</p></div><footer class="dialog-footer">${button('close-dialog','Cancel')}<button type="submit" class="primary">Create project</button></footer></form>`,'new-project');}
async function openProjectDialog(){await load();openDialog(`${dialogHead('PROJECT','Open project')}<div class="dialog-body project-choices">${state.projects.map(p=>button('choose-project',`<strong>${esc(p.name)}</strong><small>${p.assets.length} assets · ${p.sequence.clips.length} timeline clips</small>`,'project-choice',`data-id="${p.id}"`)).join('')||'<p>No projects yet. Create your first project to start editing.</p>'}${state.jobs.some(j=>j.clips?.length&&j.source_available)?`<details><summary>Existing edits · open as a project</summary>${state.jobs.filter(j=>j.clips?.length&&j.source_available).map(j=>button('open-legacy',esc(basename(j.source)),'project-choice',`data-id="${esc(j.id)}"`)).join('')}</details>`:''}</div><footer class="dialog-footer">${button('new-project','New project','primary')}${button('close-dialog','Cancel')}</footer>`,'open-project');}
async function openProject(id){
  await saveSequence();const project=await api('/api/projects/'+encodeURIComponent(id));pause();clearError();
  state.selectionRequest++;state.project=project;state.sequence=project.sequence;state.fitDuration=Math.max(10,T.sequenceDuration(project.sequence));state.source=null;state.sourceInfo=null;state.job=null;state.selected=null;state.selection.clear();state.checked.clear();state.undo=[];state.redo=[];state.dirty=false;state.time=0;state.zoom=1;state.search='';state.bin='all';state.programMode='sequence';
  $('#media-search').value='';$('#timeline-zoom').value=1;$('#program-mode').value='sequence';$('#timeline-scroll').scrollLeft=0;
  try{localStorage.setItem('smartcut.project',id);}catch{}
  renderProject();renderSource();renderProgram();renderTimeline();renderInspector();renderSourceEvidence();updateTitles();
  if(project.assets[0])await selectAsset(project.assets[0].path);
}
async function createProject(name,job_id){await saveSequence();const payload={name,...(job_id?{job_id}:{})};await post('/api/projects?dry_run=true',payload);const project=await post('/api/projects',payload);closeDialog();await load();await openProject(project.id);}
async function addAssets(paths,projectId=state.project?.id){
  if(!projectId)throw new Error('Create or open a project first');
  const url='/api/projects/'+encodeURIComponent(projectId)+'/assets';await post(url+'?dry_run=true',{paths});const project=await post(url,{paths});
  if(state.project?.id===projectId){state.project=project;renderProject();updateTitles();}return project;
}
function existingMediaDialog(){if(!state.project)return newProjectDialog();const current=new Set(assets().map(a=>pathKey(a.path))),available=libraryAssets().filter(a=>!current.has(pathKey(a.path))&&a.job?.source_available!==false);openDialog(`${dialogHead('PROJECT ASSETS','Add existing media')}<form id="existing-media-form"><div class="dialog-body project-choices"><p>Reuse media already imported on this computer.</p>${available.map(a=>`<label class="project-choice"><input type="checkbox" name="paths" value="${esc(a.path)}">${esc(a.name)}</label>`).join('')||'<p>All available media is already in this project.</p>'}</div><footer class="dialog-footer">${button('close-dialog','Cancel')}<button type="submit" class="primary">Add to project</button></footer></form>`,'existing-media');}
async function removeAsset(){if(!state.source||!state.project)throw new Error('Select a project asset first');await saveSequence();state.project=await api(projectURL('assets')+'?path='+encodeURIComponent(state.source),{method:'DELETE'});state.selectionRequest++;state.source=null;state.sourceInfo=null;state.job=null;renderProject();renderSource();renderInspector();renderTimeline();renderSourceEvidence();updateTitles();toast('Removed from project · original file kept');}
function updateTitles(){
  $('#project-name').textContent=state.project?.name||'Create or open a project';
  $('#sequence-name').textContent=state.project?.name||'01';
  $('#save-state').innerHTML=state.saving?badge('Saving…'):state.dirty?badge('Unsaved','warn'):state.sequence?badge(state.sequence.revision?'Saved':'Draft',state.sequence.revision?'ok':''):'';
  $('#source-name').textContent=state.source?basename(state.source):'No source selected';
  $('#source-format').textContent=state.sourceInfo?`${state.sourceInfo.width} × ${state.sourceInfo.height}`:'SOURCE';
  $('#program-format').textContent=state.sequence?`${state.sequence.width} × ${state.sequence.height} / ${state.sequence.fps} FPS`:'SEQUENCE';
  syncTransport();
}
function emptyMonitor(title,message,action,label){return `<div class="empty-state"><span class="empty-glyph">${icon(action==='import'?'upload':action==='analyze'?'sparkles':action==='render'?'film':'clapperboard')}</span><h3>${title}</h3><p>${message}</p>${action?button(action,label,'quiet'):''}</div>`;}
function loadingMedia(element){
  const stage=element.closest('.monitor-stage');stage.classList.add('loading');
  const ready=()=>stage.classList.remove('loading');element.addEventListener('loadeddata',ready,{once:true});element.addEventListener('load',ready,{once:true});
  element.addEventListener('error',()=>{ready();stage.innerHTML=emptyMonitor('Media unavailable','Check the source path or retry loading.','retry-media','Retry');});
}
function renderSource(){
  const stage=$('#source-stage');
  if(!state.source){stage.innerHTML=emptyMonitor('Find your first frame','Select media from the Project panel.','import','Import media');return;}
  if(state.job?.source===state.source&&!state.job.source_available||assets().find(a=>a.path===state.source)?.source_available===false){stage.innerHTML=emptyMonitor('Original offline','This source file is no longer at its saved path.');return;}
  const kind=kindOf(state.source);
  stage.innerHTML=kind==='image'?`<img id="source-media" src="${fileURL(state.source)}" alt="${esc(basename(state.source))}">`:
    `${kind==='audio'?`<span class="audio-art">${icon('audio-waveform')}</span>`:''}<${kind==='audio'?'audio':'video'} id="source-media" src="${fileURL(state.source)}" preload="metadata" playsinline></${kind==='audio'?'audio':'video'}>`;
  const media=$('#source-media');loadingMedia(media);
  media.addEventListener('timeupdate',()=>{state.sourceTime=media.currentTime||0;if(state.stopAt!=null&&state.sourceTime>=state.stopAt){state.stopAt=null;media.pause();}syncTransport();if(state.tab==='review')renderEvidence();});
  if(kind!=='image')media.addEventListener('loadedmetadata',()=>{media.currentTime=state.sourceTime;syncTransport();});
  media.addEventListener('ended',()=>syncTransport());
  media.style.transform=`rotate(${state.rotation||0}deg)`;
  syncTransport();
}
function renderProgram(){
  const stage=$('#program-stage');
  if(state.programMode!=='sequence'){
    const output=state.programMode==='final'?state.project?.final_output:state.job?.preview_output;
    if(!output?.path){stage.innerHTML=emptyMonitor('No render yet','Render your approved sequence or build a preview reel.',state.programMode==='final'?'render':'preview',state.programMode==='final'?'Render approved cut':'Build preview');return;}
    stage.innerHTML=`<video id="output-media" src="${fileURL(output.path)}" preload="metadata" playsinline></video>`;
    const media=$('#output-media');loadingMedia(media);media.addEventListener('timeupdate',()=>{state.time=media.currentTime;syncTransport();});return;
  }
  if(!state.sequence){stage.innerHTML=emptyMonitor('Start a new edit','Create a project or open an existing one from the left panel.','new-project','New project');return;}
  if(!state.sequence.clips.length){stage.innerHTML=emptyMonitor('Your timeline is ready','Import media, then drag a project asset onto a track.',assets().length?'insert':'import',assets().length?'Insert source range':'Import media');return;}
  stage.innerHTML=`<div id="program-canvas" style="aspect-ratio:${state.sequence.width}/${state.sequence.height}">${state.sequence.clips.map(c=>{
    const tag=c.track.startsWith('A')?'audio':c.kind==='image'?'img':'video';
    return `<${tag} data-program-clip="${c.id}" src="${fileURL(c.source)}" ${tag==='img'?`alt="${esc(c.name)}"`:'preload="metadata" playsinline'} style="display:none" ${tag==='video'?'muted':''}>${tag==='img'?'':`</${tag}>`}`;
  }).join('')}</div><div class="program-overlay"><div id="ai-hud" class="ai-hud" hidden></div><p id="live-caption" class="live-caption" hidden></p></div>`;
  overlayKey='';syncProgram();syncTransport();
}
function syncProgram(){
  if(!state.sequence||state.programMode!=='sequence')return;
  const canvas=$('#program-canvas');if(!canvas)return;
  const factor=canvas.getBoundingClientRect().width/state.sequence.width;
  const muted=new Set(state.sequence.tracks.filter(t=>t.muted).map(t=>t.id));
  for(const clip of state.sequence.clips){
    const media=$(`[data-program-clip="${clip.id}"]`);if(!media)continue;
    const active=clip.enabled&&!muted.has(clip.track)&&state.time>=clip.start&&state.time<T.end(clip);
    const visual=clip.track.startsWith('V');media.style.display=active&&visual?'block':'none';
    if(visual){media.style.zIndex=clip.track==='V2'?2:1;media.style.opacity=clip.opacity;media.style.transform=`translate(${clip.x*factor}px,${clip.y*factor}px) rotate(${clip.rotation}deg) scale(${clip.scale})`;}
    if(media.tagName==='IMG')continue;
    media.muted=visual;media.volume=Math.min(1,clip.volume);media.playbackRate=clip.speed;
    const sourceTime=clip.source_start+(state.time-clip.start)*clip.speed;
    if(active&&Number.isFinite(media.duration)&&Math.abs(media.currentTime-sourceTime)>(state.playing?.2:.015))media.currentTime=Math.max(0,sourceTime);
    if(active&&state.playing&&media.paused)media.play().catch(()=>{});
    else if((!active||!state.playing)&&!media.paused)media.pause();
  }
}
function syncTransport(){
  const source=$('#source-media'),output=$('#output-media');
  const sd=state.sourceInfo?.duration||source?.duration||1,pd=state.programMode==='sequence'?(state.sequence?T.sequenceDuration(state.sequence):1):(output?.duration||1);
  $('#source-time').textContent=tc(state.sourceTime);$('#source-duration').textContent=tc(sd);
  $('#program-time').textContent=tc(state.time);$('#program-duration').textContent=tc(pd);
  $('#source-scrub').max=Number.isFinite(sd)?sd:1;$('#source-scrub').value=state.sourceTime;
  $('#program-scrub').max=Number.isFinite(pd)?pd:1;$('#program-scrub').value=state.time;
  setPlayIcon($('#source-play'),!!(source&&!source.paused&&source.tagName!=='IMG'),'source');
  setPlayIcon($('#program-play'),!!(state.playing||output&&!output.paused),'program');
  $('#source-range').textContent=`I ${tc(state.in)}  ·  O ${tc(state.out)}`;
  $('#timeline-time').textContent=tc(state.time);
  const playhead=$('#playhead'),x=state.time*pps();if(playhead)playhead.style.left=x+'px';
  const scroller=$('#timeline-scroll');if(state.playing&&scroller&&(x<scroller.scrollLeft||x>scroller.scrollLeft+scroller.clientWidth-40))scroller.scrollLeft=Math.max(0,x-60);
  const head=$('#evidence-head'),range=$('#evidence-range'),d=state.job?.duration;if(head&&d)head.style.left=(Math.min(1,state.sourceTime/d)*100)+'%';
  if(range&&d){const a=Math.min(state.in,state.out),b=Math.max(state.in,state.out);range.hidden=b-a<.02;range.style.left=(a/d*100)+'%';range.style.width=((b-a)/d*100)+'%';}
  renderOverlay();
}
// syncTransport runs every frame; only touch the button when the play state actually flips.
function setPlayIcon(button,playing,name){if(!button||button.dataset.playing===String(playing))return;button.dataset.playing=playing;button.innerHTML=icon(playing?'pause':'play');button.setAttribute('aria-label',`${playing?'Pause':'Play'} ${name}`);}
// Live captions and the AI verdict for whatever source frame is under the program playhead.
function renderOverlay(){
  const hud=$('#ai-hud'),caption=$('#live-caption');if(!hud||!caption)return;
  const job=state.job,hit=job&&state.sequence?A.sequenceToSource(state.sequence,state.time,job.source):null;
  const segment=state.prefs.cc&&hit?(job.transcript_segments||[]).find(s=>hit.time>=s.start&&hit.time<s.end):null;
  const obs=state.prefs.hud&&hit?(job.observations||[]).filter(o=>o.timestamp<=hit.time+.01).pop():null;
  const flag=obs&&[...aiProposals().map(r=>({...r,kind:'waste'})),...(job.review_intervals||[]).map(r=>({...r,kind:'review'}))].find(r=>hit.time>=r.start&&hit.time<r.end);
  const key=[segment?.start,obs?.timestamp,flag?.start,flag?.kind].join('|');if(key===overlayKey)return;overlayKey=key;
  caption.hidden=!segment;caption.textContent=segment?.text||'';
  hud.hidden=!obs;if(!obs)return;
  hud.className='ai-hud '+(flag?flag.kind:obs.keep===false?'waste':'keep');
  hud.innerHTML=`<b>AI ${esc(obs.score)}/10</b><span>${flag?.kind==='waste'?'Suggests cut':flag?'Needs your eyes':obs.keep===false?'Weak frame':'Keep'}</span>${flag?`<small>${esc((flag.reasons||[]).join(' · ').replace(/vision-(cull|review):/g,'').replace(/_/g,' '))}</small>`:''}`;
}
function seekSource(time){state.focus='source';state.sourceTime=Math.max(0,Math.min(state.sourceInfo?.duration||state.job?.duration||0,time));const m=$('#source-media');if(m&&m.tagName!=='IMG'&&Number.isFinite(m.duration))m.currentTime=state.sourceTime;syncTransport();renderEvidence();}
function seekProgram(time){state.focus='program';state.time=Math.max(0,Math.min(state.sequence?T.sequenceDuration(state.sequence):$('#output-media')?.duration||0,time));const m=$('#output-media');if(m&&Number.isFinite(m.duration))m.currentTime=state.time;syncProgram();syncTransport();}
function pause(){state.playing=false;state.shuttle=0;state.stopAt=null;clearInterval(reverseTimer);$$('video,audio').forEach(m=>m.pause());syncTransport();}
function play(focus=state.focus,rate=1){
  clearInterval(reverseTimer);state.focus=focus;
  if(focus==='source'){
    const m=$('#source-media');if(!m||m.tagName==='IMG')return;
    state.playing=false;$$('#program-stage video,#program-stage audio').forEach(v=>v.pause());m.playbackRate=rate;m.play().catch(e=>fail(e));
  }else if(state.programMode!=='sequence'){
    const m=$('#output-media');if(m){m.playbackRate=rate;m.play().catch(e=>fail(e));}
  }else{
    const m=$('#source-media');if(m?.pause)m.pause();if(!state.sequence)return;
    if(state.time>=T.sequenceDuration(state.sequence))state.time=0;
    state.playing=true;state.shuttle=rate;clockLast=performance.now();requestAnimationFrame(tick);
  }
  syncTransport();
}
function tick(now){if(!state.playing)return;state.time+=(now-clockLast)/1000*(state.shuttle||1);clockLast=now;if(state.time>=T.sequenceDuration(state.sequence)){state.time=T.sequenceDuration(state.sequence);pause();}syncProgram();syncTransport();if(state.playing)requestAnimationFrame(tick);}
function togglePlay(focus=state.focus){const m=focus==='source'?$('#source-media'):$('#output-media');if(state.playing||m&&!m.paused&&m.tagName!=='IMG')pause();else play(focus);}
function shuttle(direction){
  const rate=state.shuttle*direction>0?Math.min(4,Math.abs(state.shuttle)*2):1;pause();state.shuttle=direction*rate;
  if(direction>0)play(state.focus,rate);
  else reverseTimer=setInterval(()=>{const value=(state.focus==='source'?state.sourceTime:state.time)-.08*rate;if(state.focus==='source')seekSource(value);else seekProgram(value);if(value<=0)pause();},80);
}

function pps(){return state.timelineScale||2;}
function renderTimeline(){
  const seq=state.sequence,seconds=Math.max(10,seq?T.sequenceDuration(seq):60);
  // Keep one scale for the rendered canvas and every pointer calculation.
  const scale=state.timelineScale=Math.max(2,(($('#timeline-scroll')?.clientWidth||800)-32)/(state.fitDuration||seconds))*state.zoom,width=Math.max($('#timeline-scroll').clientWidth-2,seconds*scale+60);
  const lanes=state.prefs.lanes&&!!seq&&!!state.job?.observations?.length;$('.timeline-body').classList.toggle('lanes',lanes);
  $('#track-headers').innerHTML=(lanes?'<div class="lane-header" title="AI frame scores, flagged spans and scene changes"><strong>AI</strong><span>score · flags</span></div><div class="lane-header" title="Transcript mapped through your edit"><strong>TX</strong><span>transcript</span></div>':'')+(seq?.tracks||['V2','V1','A1','A2'].map(id=>({id}))).map(t=>`<div class="track-header ${t.id[0]==='A'?'audio':''}"><strong>${t.id}</strong><span>${t.id==='V2'?'Overlay':t.id==='V1'?'Picture':t.id==='A1'?'Source audio':'Music / audio'}</span><button data-track-mute="${t.id}" aria-pressed="${!!t.muted}" aria-label="${t.id[0]==='A'?'Mute':'Hide'} ${t.id}" title="${t.id[0]==='A'?'Mute':'Hide'} ${t.id}" class="${t.muted?'active':''}">${icon(t.id[0]==='A'?(t.muted?'volume-x':'volume-2'):(t.muted?'eye-off':'eye'))}</button><button data-track-lock="${t.id}" aria-pressed="${!!t.locked}" aria-label="Lock ${t.id}" title="Lock ${t.id}" class="${t.locked?'active':''}">${icon(t.locked?'lock':'lock-open')}</button></div>`).join('');
  const step=[1,2,5,10,15,30,60,120,300].find(s=>s*scale>=65)||600;
  const ticks=Array.from({length:Math.ceil(seconds/step)+1},(_,i)=>`<span class="ruler-tick" style="left:${i*step*scale}px">${short(i*step)}</span>`).join('');
  $('#timeline-canvas').style.width=width+'px';
  $('#timeline-canvas').innerHTML=`<div class="ruler" data-scrub>${ticks}${(seq?.markers||[]).map(m=>`<button class="marker" data-marker="${esc(m.id)}" title="${esc(m.label)}" aria-label="Marker: ${esc(m.label)}" style="left:${m.time*scale}px"></button>`).join('')}</div>`+(lanes?aiLanes(seq,scale):'')+
    ['V2','V1','A1','A2'].map(track=>`<div class="track-row ${track[0]==='A'?'audio':''} ${T.locked(seq||{tracks:[]},track)?'locked':''}" data-track="${track}">${(seq?.clips||[]).filter(c=>c.track===track).map(c=>`<div class="timeline-clip ${c.track[0]==='A'?'audio':''} ${state.selection.has(c.id)?'selected':''} ${!c.enabled?'disabled':''}" data-clip="${c.id}" tabindex="0" role="button" aria-label="${esc(c.name)} on ${track}" style="left:${c.start*scale}px;width:${Math.max(4,T.duration(c)*scale)}px" title="${esc(c.name)} · ${tc(c.source_start)}–${tc(c.source_end)}">${c.track[0]==='A'?`<canvas class="clip-wave" data-wave-clip="${c.id}" aria-hidden="true"></canvas>`:filmstrip(c,scale)}<span class="trim-edge left" data-trim="start" title="Trim start"></span><span class="clip-label">${icon(c.track[0]==='A'?'music':c.kind==='image'?'image':'film')}${esc(c.name||basename(c.source))}</span><span class="clip-detail">${c.speed!==1?c.speed+'× · ':''}${tc(T.duration(c))}</span><span class="trim-edge right" data-trim="end" title="Trim end"></span></div>`).join('')}${!seq&&track==='V1'?'<span class="timeline-hint">Create or open a project to start editing</span>':''}</div>`).join('')+`<div id="playhead" style="left:${state.time*scale}px"><i></i></div>`;
  $('#timeline-summary').textContent=seq?`${seq.clips.filter(c=>c.track.startsWith('V')).length} video clips · ${tc(T.sequenceDuration(seq))} · ${seq.fps} fps`:'Select a sequence to start editing';
  $$('[data-action="undo"]').forEach(b=>b.disabled=!state.undo.length);$$('[data-action="redo"]').forEach(b=>b.disabled=!state.redo.length);
  paintSelection();drawWaves();
}
const isModelProposal=r=>!(r.reasons||[]).some(x=>String(x).startsWith('editor-review'));
// Every review save or reload replaces state.job, so the job object itself is a safe cache key.
let proposalCache={job:undefined,list:[]};
function aiProposals(){
  if(proposalCache.job!==state.job){const review=currentReview(),obs=state.job?.observations||[];proposalCache={job:state.job,list:(state.job?.waste_intervals||[]).filter(isModelProposal).map(r=>({...r,...A.proposalEvidence(r,obs),status:A.proposalStatus(r,review)}))};}
  return proposalCache.list;
}
const reasonText=r=>(r.reasons||[]).join(' · ').replace(/vision-(cull|review):/g,'').replace(/_/g,' ')||'model proposal';
// Source-time evidence mapped through the edit, so the AI's opinion follows the material wherever it now sits.
function aiLanes(seq,scale){
  const job=state.job,src=job.source,obs=job.observations||[],at=r=>`left:${r.start*scale}px;width:${Math.max(1,(r.end-r.start)*scale)}px`;
  const heat=obs.flatMap((o,i)=>A.sourceToSequence(seq,src,o.timestamp,obs[i+1]?.timestamp??Math.min(job.duration||o.timestamp+2,o.timestamp+2)).map(r=>`<i class="heat ${o.keep===false?'cut':''}" style="${at(r)};--h:${(Number(o.score)||0)/10}" title="${esc(`${short(o.timestamp)} · AI ${o.score}/10 · ${o.keep===false?'cut':'keep'} — ${String(o.description||'').slice(0,180)}`)}"></i>`)).join('');
  const flags=[...aiProposals().map(r=>({...r,kind:'waste'})),...(job.review_intervals||[]).map(r=>({...r,kind:'review'}))].flatMap(r=>A.sourceToSequence(seq,src,r.start,r.end).map(m=>`<i class="flag ${r.kind} ${r.status||''}" style="${at(m)}" ${r.kind==='waste'?`data-proposal-start="${r.start}"`:''} title="${esc(`${r.kind==='waste'?'AI suggests cutting':'Needs your eyes'} · ${reasonText(r)}`)}"></i>`)).join('');
  const scenes=(job.scene_boundaries||[]).flatMap(b=>A.sourcePoint(seq,src,b)).map(t=>`<i class="scene-tick" style="left:${t*scale}px" title="Scene change"></i>`).join('');
  const words=(job.transcript_segments||[]).flatMap(s=>A.sourceToSequence(seq,src,s.start,s.end).map(r=>`<span class="tx" style="${at(r)}" title="${esc(s.text)}">${(r.end-r.start)*scale>=30?esc(s.text):''}</span>`)).join('');
  return `<div class="lane-row ai-lane" data-lane="ai">${heat}${flags}${scenes}</div><div class="lane-row tx-lane" data-lane="tx">${words}</div>`;
}
function filmstrip(c,scale){
  const width=T.duration(c)*scale;if(width<40||!state.sequence)return '';
  const tile=Math.max(22,Math.round(37*state.sequence.width/state.sequence.height)),count=Math.min(12,Math.ceil(width/tile)),path=encodeURIComponent(String(c.source).replace(/\\/g,'/'));
  return `<span class="clip-thumbs" aria-hidden="true">${Array.from({length:count},(_,i)=>`<img loading="lazy" alt="" draggable="false" style="width:${tile}px" src="/api/thumbnail?path=${path}&t=${c.kind==='image'?0:Math.round((c.source_start+i*tile/scale*c.speed)*2)/2}&h=72" onerror="this.remove()">`).join('')}</span>`;
}
function peaksFor(path){if(!state.waves.has(path))state.waves.set(path,api('/api/waveform?path='+encodeURIComponent(path)).catch(()=>({rate:50,peaks:[]})));return state.waves.get(path);}
function drawWaves(){for(const canvas of $$('[data-wave-clip]')){const clip=state.sequence?.clips.find(c=>c.id===canvas.dataset.waveClip);if(clip)peaksFor(clip.source).then(w=>paintWave(canvas,clip,w));}}
function paintWave(canvas,clip,{peaks,rate}){
  if(!canvas.isConnected||!peaks?.length)return;
  const w=Math.min(4096,Math.round(canvas.clientWidth)),h=canvas.clientHeight,dpr=window.devicePixelRatio||1,span=clip.source_end-clip.source_start,gain=Math.min(clip.volume,2);if(!w||!h)return;
  canvas.width=w*dpr;canvas.height=h*dpr;const g=canvas.getContext('2d');g.scale(dpr,dpr);g.fillStyle=getComputedStyle(canvas).color;
  for(let x=0;x<w;x++){const a=Math.floor((clip.source_start+span*x/w)*rate),b=Math.max(a+1,Math.floor((clip.source_start+span*(x+1)/w)*rate));let p=0;for(let i=a;i<b&&i<peaks.length;i++)p=Math.max(p,peaks[i]);const bar=Math.min(1,p*gain)*(h-2);g.fillRect(x,(h-bar)/2,1,Math.max(.5,bar));}
  canvas.closest('.timeline-clip')?.classList.add('has-wave');
}
// Source-monitor minimap: every AI and human judgement for the whole source, clickable to seek.
function renderSourceEvidence(){
  const node=$('#source-evidence');if(!node)return;const job=state.job,d=job?.duration;
  if(!job||state.source!==job.source||!d||!job.observations?.length){node.hidden=true;node.innerHTML='';return;}
  const pos=(a,b)=>`left:${(Math.max(0,a)/d*100).toFixed(3)}%;width:${(Math.max(.05,Math.min(d,b)-Math.max(0,a))/d*100).toFixed(3)}%`,obs=job.observations,review=currentReview();
  const heat=obs.map((o,i)=>`<i class="heat ${o.keep===false?'cut':''}" style="${pos(o.timestamp,obs[i+1]?.timestamp??d)};--h:${(Number(o.score)||0)/10}"></i>`).join('');
  const spans=[...(job.clips||[]).map(r=>['kept',r,'Kept by plan']),...aiProposals().map(r=>['waste '+r.status,r,'AI cut · '+r.status+' · '+reasonText(r)]),...(job.review_intervals||[]).map(r=>['review',r,'Needs your eyes · '+reasonText(r)]),...(review.cut_intervals||[]).map(r=>['human-cut',r,'Your cut · '+(r.reason||'')]),...(review.keep_intervals||[]).map(r=>['human-keep',r,'Your keep · '+(r.reason||'')])];
  node.innerHTML=`<div class="strip heat-strip">${heat}</div><div class="strip flag-strip">${spans.map(([kind,r,label])=>`<i class="${kind}" style="${pos(r.start,r.end)}" title="${esc(`${short(r.start)}–${short(r.end)} · ${label}`)}"></i>`).join('')}</div><div class="strip word-strip">${(job.transcript_segments||[]).map(s=>`<i style="${pos(s.start,s.end)}" title="${esc(s.text)}"></i>`).join('')}</div>${(job.scene_boundaries||[]).map(b=>`<i class="scene-tick" style="left:${(b/d*100).toFixed(3)}%"></i>`).join('')}<i class="strip-range" id="evidence-range"></i><i class="strip-head" id="evidence-head"></i>`;
  node.hidden=false;syncTransport();
}
function edit(label,change){
  if(!state.sequence)throw new Error('Create or open a project first');
  const before=T.clone(state.sequence),next=T.clone(state.sequence);change(next);T.validate(next);
  state.undo.push(before);if(state.undo.length>75)state.undo.shift();state.redo=[];state.sequence=next;changed(label);
}
function changed(label){state.dirty=true;state.editVersion++;state.programMode='sequence';$('#program-mode').value='sequence';pause();renderTimeline();renderProgram();renderInspector();updateTitles();clearTimeout(saveTimer);saveTimer=setTimeout(()=>operation('Save sequence',()=>saveSequence()),900);if(label)toast(label);}
async function saveSequence(force=false){
  clearTimeout(saveTimer);
  if(force&&state.sequence?.revision===0)state.dirty=true;
  if(state.saving)return state.saving;
  if(!state.dirty||!state.sequence||!state.project)return;
  const id=state.project.id,url=projectURL('sequence');
  // One writer drains edits made during an in-flight save. Manual Save, autosave,
  // and project switching all await the same writer and cannot race revisions.
  state.saving=(async()=>{while(state.dirty&&state.project?.id===id){const version=state.editVersion,sequence=T.clone(state.sequence);const result=await api(url,{method:'PUT',body:JSON.stringify(sequence)});state.sequence.revision=result.sequence.revision;if(version===state.editVersion)state.dirty=false;}})();updateTitles();
  try{await state.saving;}finally{state.saving=null;updateTitles();}
}
function undo(redo=false){const from=redo?state.redo:state.undo,to=redo?state.undo:state.redo;if(!from.length)return;to.push(T.clone(state.sequence));const revision=state.sequence.revision;state.sequence=from.pop();state.sequence.revision=revision;changed(redo?'Redo':'Undo');}
async function addSource(overwrite=false){
  if(!state.source)throw new Error('Select source media first');const info=state.sourceInfo||await metadata(state.source),kind=kindOf(state.source);
  const start=Math.min(state.in,state.out),finish=Math.max(state.in,state.out);if(finish-start<.02)throw new Error('Mark an In and Out range first');
  const link=T.uid(),base={source:state.source,source_sha256:info.sha256,kind,source_start:start,source_end:finish,start:state.time,speed:1,opacity:1,scale:1,x:0,y:0,rotation:0,volume:1,enabled:true,link_id:link,name:basename(state.source)};
  const clips=kind==='audio'?[{...base,id:T.uid(),track:'A2'}]:[{...base,id:T.uid(),track:'V1'},...(kind==='video'&&info.has_audio?[{...base,id:T.uid(),track:'A1'}]:[])];
  edit(overwrite?'Source range overwritten':'Source range inserted',seq=>T.insert(seq,clips,state.time,overwrite));chooseClip(clips[0].id);
}
async function rebuildSequence(){
  if(!state.job)throw new Error('Select an analyzed source first');await saveSequence();const next=await api(jobURL('sequence?use_plan=true'));
  next.source_sha256=state.sequence.source_sha256;next.revision=state.sequence.revision;
  if(state.sequence)state.undo.push(T.clone(state.sequence));state.sequence=next;state.selected=null;state.selection.clear();state.time=0;changed('Approved plan loaded · Undo restores your previous sequence');
}
function commit(label,next){
  T.validate(next);next.revision=state.sequence.revision;next.source_sha256=state.sequence.source_sha256;state.undo.push(T.clone(state.sequence));if(state.undo.length>75)state.undo.shift();state.redo=[];
  state.sequence=next;if(!next.clips.some(c=>c.id===state.selected))state.selected=null;state.time=Math.min(state.time,T.sequenceDuration(next));changed(label);
}
const secs=n=>(n>=60?short(n)+' min':n.toFixed(1)+'s');
function needSequence(){if(!state.sequence)throw new Error('Create or open a project first');}
// One pass of the automatic editor over a copy of the sequence; nothing touches state until the caller commits.
async function runAuto(o){
  needSequence();
  if((o.fromPlan||o.waste||o.scenes||o.highlights)&&!state.job)throw new Error('Analyze the selected source to use AI edits');
  const job=state.job||{},src=state.source,keeps=currentReview().keep_intervals||[],protect=ranges=>ranges.flatMap(r=>A.subtract(r,keeps)),lines=[];
  const next=o.fromPlan?await api(jobURL('sequence?use_plan=true')):T.clone(state.sequence);
  if(o.fromPlan)lines.push('Rebuilt from the approved plan');
  if(o.waste){const r=A.removeSourceRanges(next,src,protect([...aiProposals().filter(p=>p.status!=='rejected'),...(currentReview().cut_intervals||[])]));if(r.ranges.length)lines.push(`AI waste & your cuts: ${r.ranges.length} range${r.ranges.length>1?'s':''} · −${secs(r.seconds)}`);}
  if(o.silence){let count=0,seconds=0;for(const path of [...new Set(next.clips.filter(c=>c.enabled&&c.track==='A1').map(c=>c.source))]){const w=await peaksFor(path);if(!w.peaks.length)continue;const silent=A.silentRanges(w.peaks,w.rate,{thresholdDb:o.threshold,minSilence:o.minSilence,pad:o.pad}),r=A.removeSourceRanges(next,path,path===src?protect(silent):silent);count+=r.ranges.length;seconds+=r.seconds;}if(count)lines.push(`Silences below ${o.threshold} dB: ${count} cut${count>1?'s':''} · −${secs(seconds)}`);}
  if(o.gaps){const r=A.closeGaps(next);if(r.ranges.length)lines.push(`Gaps closed: ${r.ranges.length} · −${secs(r.seconds)}`);}
  if(o.scenes){const m=A.sceneMarkers(next,src,job.scene_boundaries||[]);next.markers.push(...m);if(m.length)lines.push(`Scene markers: ${m.length}`);}
  if(o.highlights){const m=A.highlightMarkers(next,src,job.observations||[],{count:o.highlightCount}).filter(h=>!next.markers.some(x=>Math.abs(x.time-h.time)<.25));next.markers.push(...m);if(m.length)lines.push(`Highlight markers: ${m.length}`);}
  T.validate(next);return {next,lines,before:A.summary(state.sequence),after:A.summary(next)};
}
function autoOptions(form){const f=new FormData(form),n=k=>Number(f.get(k));return {fromPlan:f.has('fromPlan'),waste:f.has('waste'),silence:f.has('silence'),threshold:n('threshold'),minSilence:n('minSilence'),pad:n('pad'),gaps:f.has('gaps'),scenes:f.has('scenes'),highlights:f.has('highlights'),highlightCount:n('highlightCount')};}
function autoEditDialog(only=null){
  needSequence();const o={...state.prefs.auto,...(only?{fromPlan:false,waste:false,silence:false,gaps:false,scenes:false,highlights:false,[only]:true}:{})},job=state.job||{};
  const aiSteps=['fromPlan','waste','scenes','highlights'];if(!state.job)aiSteps.forEach(key=>o[key]=false);
  const step=(key,title,detail,extra='')=>`<label class="auto-step"><input type="checkbox" name="${key}" ${o[key]?'checked':''} ${!state.job&&aiSteps.includes(key)?'disabled':''}><span><strong>${title}</strong><small>${!state.job&&aiSteps.includes(key)?'Analyze the selected source to enable this step.':detail}</small>${extra}</span></label>`;
  openDialog(`${dialogHead('AUTO',only==='silence'?'Remove silences':'Auto-edit this sequence')}<form id="auto-form" data-only="${only||''}"><div class="dialog-body"><p>Everything below runs as <b>one undoable edit</b>. Your Keep and Protect decisions are never cut.</p><div class="auto-steps">
    ${step('fromPlan','Start from the approved plan','Rebuild from Brain’s plan before the steps below.')}
    ${step('waste','Remove AI-flagged waste',`Cuts ${aiProposals().filter(p=>p.status!=='rejected').length} model proposals and ${(currentReview().cut_intervals||[]).length} of your cut decisions wherever they still play.`)}
    ${step('silence','Remove silences','Cuts quiet spans on A1 audio, leaving a little air around speech.',`<span class="auto-fields"><label>Threshold <input name="threshold" type="number" min="-70" max="-10" step="1" value="${o.threshold}"> dB</label>${button('auto-threshold',withIcon('wand-sparkles','Auto'),'accent','title="Estimate from the noise floor"')}<label>Longer than <input name="minSilence" type="number" min=".2" max="10" step=".1" value="${o.minSilence}"> s</label><label>Padding <input name="pad" type="number" min="0" max="1" step=".02" value="${o.pad}"> s</label></span><small id="level-note" class="level-note">Measuring room tone…</small>`)}
    ${step('gaps','Close gaps','Pull everything left so nothing plays as black.')}
    ${step('scenes','Mark scene changes',`${(job.scene_boundaries||[]).length} detected boundaries in the source.`)}
    ${step('highlights','Mark top AI moments','Spaced markers on the best-scoring kept frames.',`<span class="auto-fields"><label>Up to <input name="highlightCount" type="number" min="1" max="20" value="${o.highlightCount}"> markers</label></span>`)}
  </div><output id="auto-preview" class="auto-preview" aria-live="polite">Calculating…</output></div><footer class="dialog-footer"><span>Undo restores the current sequence.</span>${button('close-dialog','Cancel')}<button class="primary" type="submit">Apply auto-edit</button></footer></form>`,'auto');
  updateAutoPreview();const audio=state.source||state.sequence.clips.find(c=>c.track==='A1')?.source;if(audio)peaksFor(audio).then(w=>levelNote(A.analyzeLevels(w.peaks||[])));else levelNote(A.analyzeLevels([]));
}
function levelNote(levels){
  const note=$('#level-note');if(!note)return levels;
  note.textContent=levels.floor===null?'No usable audio on this source.':levels.reliable?`Room tone ${levels.floor} dB · speech ${levels.speech} dB · Auto suggests ${levels.threshold} dB.`:`Background sound sits only ${levels.range} dB under speech (${levels.floor} vs ${levels.speech} dB); silence cuts may clip quiet words. Check the preview.`;
  note.className='level-note '+(levels.reliable?'':'warn');return levels;
}
function updateAutoPreview(){
  clearTimeout(previewTimer);previewTimer=setTimeout(async()=>{
    const form=$('#auto-form'),out=$('#auto-preview');if(!form||!out)return;const token=++previewToken;out.classList.add('busy');
    try{const {next,lines,before,after}=await runAuto(autoOptions(form));if(token!==previewToken)return;const saved=before.duration-after.duration,span=Math.max(.001,before.duration);
      const removed=A.removedMap(state.sequence,next),bar=`<div class="auto-diff" role="img" aria-label="${removed.length} removed stretches across the current sequence">${removed.map(r=>`<i style="left:${r.start/span*100}%;width:${Math.max(.15,(r.end-r.start)/span*100)}%" title="${tc(r.start)} – ${tc(r.end)}"></i>`).join('')}</div>`;
      out.innerHTML=`<div class="auto-totals"><span><small>Now</small>${tc(before.duration)}</span><span class="arrow">→</span><span><small>After</small>${tc(after.duration)}</span>${Math.abs(saved)>=.05?`<span class="${saved>0?'saving':''}"><small>${saved>0?'Tighter by':'Longer by'}</small>${secs(Math.abs(saved))}</span>`:''}</div>${removed.length?bar:''}${lines.length?`<ul>${lines.map(l=>`<li>${esc(l)}</li>`).join('')}</ul>`:'<p>Nothing to change with these options.</p>'}`;}
    catch(error){if(token===previewToken)out.innerHTML=`<p class="error-text">${esc(error.message)}</p>`;}
    finally{if(token===previewToken)out.classList.remove('busy');}
  },180);
}
async function applyAuto(form){
  const options=autoOptions(form),{threshold,minSilence,pad,highlightCount}=options;
  // A single-step entry point (Remove silences…) remembers its numbers but not its narrowed checkboxes.
  state.prefs.auto={...state.prefs.auto,...(form.dataset.only?{threshold,minSilence,pad,highlightCount}:options)};savePrefs();
  const {next,lines,before,after}=await runAuto(options);if(!lines.length)return toast('Nothing to change with these options','warn');
  closeDialog();commit(`Auto-edit · ${tc(before.duration)} → ${tc(after.duration)} · ${lines.length} step${lines.length>1?'s':''}`,next);
}
async function quickAuto(options){const {next,lines}=await runAuto({...Object.fromEntries(Object.keys(state.prefs.auto).map(k=>[k,false])),threshold:state.prefs.auto.threshold,minSilence:state.prefs.auto.minSilence,pad:state.prefs.auto.pad,highlightCount:state.prefs.auto.highlightCount,...options});if(!lines.length)return toast('Nothing to change','warn');commit(lines.join(' · '),next);}
async function fillFrame(fit=false){
  needSequence();const targets=(state.selected?T.linkedClips(state.sequence,state.selected,state.linked):state.sequence.clips).filter(c=>c.track[0]==='V');
  if(!targets.length)throw new Error('Select a video clip, or clear the selection to fill every video clip');
  const sizes=new Map();for(const c of targets)if(!sizes.has(c.source))sizes.set(c.source,await metadata(c.source));
  edit(`${fit?'Fit':'Fill'} frame · ${targets.length} clip${targets.length>1?'s':''}`,s=>{for(const t of targets){const c=s.clips.find(x=>x.id===t.id);if(T.locked(s,c.track))throw new Error('Unlock the track first');const m=sizes.get(c.source),turned=Math.abs(Math.round(c.rotation))%180===90;
    Object.assign(c,{x:0,y:0,scale:fit?1:Math.min(4,A.fillScale(turned?m.height:m.width,turned?m.width:m.height,s.width,s.height))});}});
}
async function decideProposals(list,verdict){
  if(!list.length)return toast('No pending AI proposals','warn');const review=T.clone(currentReview());
  for(const p of list)(verdict==='accept'?review.cut_intervals:review.keep_intervals).push({start:p.start,end:p.end,reason:`${verdict==='accept'?'Accepted':'Rejected'} AI cut: ${reasonText(p)}`});
  const result=await post(jobURL('review'),review);state.job=result.job;state.jobs=state.jobs.map(j=>j.id===result.job.id?result.job:j);
  renderInspector();renderSourceEvidence();renderTimeline();overlayKey='';renderOverlay();
  const many=list.length>1;toast(`${many?list.length+' proposals':'Proposal'} ${verdict==='accept'?(many?'accepted as cuts':'accepted as a cut'):'rejected and protected'} · Auto-edit or Re-plan applies ${many?'them':'it'}`,'ok');
}
function stepProposal(direction=1){
  if(!state.job)throw new Error('Select an analyzed source first');const all=aiProposals();if(!all.length)return toast('This source has no AI cut proposals','warn');
  const pending=all.map((p,i)=>[p,i]).filter(([p])=>p.status==='pending'),pool=pending.length?pending:all.map((p,i)=>[p,i]);
  const ordered=direction>0?pool:[...pool].reverse(),hit=ordered.find(([,i])=>direction>0?i>state.proposal:i<state.proposal)||ordered[0];
  focusProposal(hit[1]);if(!pending.length)toast('Every AI proposal has a decision','ok');
}
function focusProposal(index){
  const p=aiProposals()[index];if(!p)return;state.proposal=index;state.in=p.start;state.out=p.end;state.reviewEditing=null;state.tab='review';
  if(state.source===state.job.source)seekSource(p.start);const inSequence=state.sequence&&A.sourceToSequence(state.sequence,state.job.source,p.start,p.end)[0];if(inSequence)seekProgram(inSequence.start);
  renderInspector();$('.proposal.current')?.scrollIntoView({block:'nearest'});
}
function decideCurrent(verdict){const p=aiProposals()[state.proposal];if(!p)return stepProposal(1);return decideProposals([p],verdict).then(()=>{if(aiProposals().some(x=>x.status==='pending'))stepProposal(1);});}
// Play the current proposal (or In→Out) with a second of pre-roll, stopping just after the out point.
function audition(){
  if(!state.job||state.source!==state.job.source)throw new Error('Load the analyzed source in the Source monitor to audition');
  const p=aiProposals()[state.proposal],range=p||{start:Math.min(state.in,state.out),end:Math.max(state.in,state.out)};
  seekSource(Math.max(0,range.start-1));play('source');state.stopAt=range.end+.4;
}
function editPoint(direction){
  needSequence();const points=[...new Set([0,...state.sequence.clips.flatMap(c=>[c.start,T.end(c)]),...state.sequence.markers.map(m=>m.time)].map(t=>Math.round(t*1000)/1000))].sort((a,b)=>a-b);
  const t=direction>0?points.find(p=>p>state.time+.001):[...points].reverse().find(p=>p<state.time-.001);if(t!==undefined){pause();seekProgram(t);}
}
function setZoom(value,anchor=null){
  const scroller=$('#timeline-scroll'),old=pps(),offset=anchor??Math.max(0,state.time*old-scroller.scrollLeft),time=(scroller.scrollLeft+offset)/old;
  state.zoom=Math.max(1,Math.min(60,value));$('#timeline-zoom').value=state.zoom;renderTimeline();scroller.scrollLeft=Math.max(0,time*pps()-offset);
}
function markerDialog(id){
  const m=state.sequence?.markers.find(x=>x.id===id);if(!m)return;
  openDialog(`${dialogHead('MARKER','Edit marker')}<form id="marker-form" data-marker-form="${esc(id)}"><div class="dialog-body"><label class="stacked">Label<input name="label" maxlength="200" value="${esc(m.label)}" required></label><label class="stacked">Time (seconds)<input name="time" type="number" min="0" step=".01" value="${round(m.time)}" required></label></div><footer class="dialog-footer">${button('delete-marker','Delete marker','danger',`data-marker-id="${esc(id)}"`)}${button('close-dialog','Cancel')}<button class="primary" type="submit">Save marker</button></footer></form>`,'marker');
}
function exportCaptions(){
  needSequence();if(!state.job)throw new Error('Transcribe the selected source first');const captions=A.editCaptions(state.sequence,state.job.source,state.job.transcript_segments||[]);if(!captions.length)throw new Error('No transcribed speech plays in this sequence');
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([A.toSRT(captions)],{type:'application/x-subrip'}));a.download=basename(state.job.source).replace(/\.[^.]+$/,'')+'_edit.srt';
  document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),2000);toast(`${captions.length} captions exported, timed to this edit`,'ok');
}
function shortcutsDialog(){
  const keys=[['Space','Play / pause'],['J K L','Shuttle reverse, stop, forward'],['← / →','Previous / next frame'],['↑ / ↓','Previous / next edit point or marker'],['I / O','Mark In / Out'],['P','Audition the AI proposal or In→Out'],['C','Split at playhead'],['V','Selection tool'],['M','Add marker (double-click a marker to edit)'],['Drag empty track space','Box-select clips across tracks'],['Shift / Ctrl + click','Add or remove a clip from selection'],['Escape','Cancel drag or box selection'],['Delete','Lift selected clips'],['Shift + Delete','Ripple delete'],['= / − / \\','Zoom in / out / fit (Ctrl + wheel zooms at the cursor)'],['Ctrl + Z / Y','Undo / redo'],['Ctrl + S','Save sequence'],['N / Shift + N','Next / previous AI proposal'],['A','Accept proposal as a cut'],['X','Reject proposal and protect it'],['?','This list']];
  openDialog(`${dialogHead('HELP','Keyboard shortcuts')}<div class="dialog-body"><dl class="shortcut-list">${keys.map(([k,v])=>`<dt><kbd>${esc(k)}</kbd></dt><dd>${esc(v)}</dd>`).join('')}</dl></div><footer class="dialog-footer">${button('close-dialog','Done','primary')}</footer>`,'help');
}

function renderInspector(){
  $$('[data-tab]').forEach(t=>t.setAttribute('aria-selected',t.dataset.tab===state.tab));
  const body=$('#inspector-content');
  if(state.tab==='effects'){
    if(state.selection.size>1){body.innerHTML=`<div class="inspector-section"><h3>${state.selection.size} clips selected</h3><p>Drag to move together. Shift-click adds or removes clips.</p>${button('delete','Delete selected','wide')}${button('duplicate','Duplicate selected','wide')}${button('enable','Toggle enabled','wide')}</div>`;return;}
    const clip=state.sequence?.clips.find(c=>c.id===state.selected);
    if(!clip){body.innerHTML=`<div class="inspector-intro"><span class="eyebrow">EFFECT CONTROLS</span><h3>Every frame, in place.</h3><p>Select a timeline clip to adjust its properties.</p></div><div class="inspector-section"><h3>Sequence</h3><dl><dt>Resolution</dt><dd>${state.sequence?state.sequence.width+' × '+state.sequence.height:'—'}</dd><dt>Frame rate</dt><dd>${state.sequence?.fps||'—'} fps</dd><dt>Tracks</dt><dd>2 video · 2 audio</dd></dl>${button('sequence-settings','Sequence settings','wide')}</div><div class="inspector-section"><h3>Editing guide</h3><p>Drag edges to trim. Drag clips between matching tracks. Right-click a clip for more actions.</p><p>Linked editing keeps picture and sound together.</p></div>`;return;}
    const audio=clip.track.startsWith('A');
    body.innerHTML=`<div class="clip-inspector-title"><span class="media-icon ${audio?'audio':'video'}">${icon(audio?'music':'film')}</span><div><strong>${esc(clip.name)}</strong><small>${clip.track} · ${tc(T.duration(clip))}</small></div></div><div class="inspector-section"><h3>Clip properties</h3><dl><dt>Source In</dt><dd>${tc(clip.source_start)}</dd><dt>Source Out</dt><dd>${tc(clip.source_end)}</dd><dt>Sequence start</dt><dd>${tc(clip.start)}</dd></dl><label class="toggle-row">Enabled<input type="checkbox" data-effect="enabled" ${clip.enabled?'checked':''}></label></div><details class="inspector-section" open><summary>Time &amp; audio</summary>${effect('speed','Speed',clip.speed,.25,4,.05,'×')}${effect('volume','Audio gain',clip.volume,0,4,.05,'×')}<p class="field-note">Gain above 1× is applied on render; live preview is capped at 1×.</p></details><details class="inspector-section" ${!audio?'open':''}><summary>Motion &amp; opacity</summary>${effect('opacity','Opacity',clip.opacity,0,1,.01)}${effect('scale','Scale',clip.scale,.1,4,.01,'×')}${audio?'':`<div class="effect-auto">${button('auto-fit','Fit frame','quiet')}${button('auto-fill','Fill frame','quiet','title="Scale so the picture covers the whole sequence frame"')}</div>`}${effect('x','Position X',clip.x,-4096,4096,1,'px')}${effect('y','Position Y',clip.y,-4096,4096,1,'px')}${effect('rotation','Rotation',clip.rotation,-360,360,1,'°')}</details><div class="inspector-section">${button('reset-effects','Reset effects','wide')}</div>`;
  }else if(state.tab==='review')renderReview();
  else body.innerHTML=`<div class="inspector-intro"><span class="eyebrow">PIPELINE</span><h3>From source to story.</h3><p>Control each stage from Pipeline Settings.</p></div>${['Connection','Ear','Eye','Temporal & multi-pass','Brain','Preview','Reel','Voice','Blade'].map((group,i)=>`<details class="inspector-section" ${i<2?'open':''}><summary>${esc(group)} <small>${Object.values(state.schema).filter(f=>f.group===group).length} controls</small></summary><p>${stageSummary(group)}</p><button class="wide" data-settings-group="${esc(group)}">Configure ${esc(group)}</button></details>`).join('')}<div class="inspector-section"><h3>Background tasks</h3><div id="task-list">${taskList()}</div></div>`;
}
function effect(key,label,value,min,max,step,suffix=''){return `<label class="effect-control"><span>${label}<small>${suffix}</small></span><input type="number" data-effect="${key}" value="${round(value)}" min="${min}" max="${max}" step="${step}" aria-label="${label}"></label><input type="range" class="effect-slider" data-effect="${key}" value="${value}" min="${min}" max="${max}" step="${step}" aria-label="${label} slider">`;}
function stageSummary(group){const c=state.config;return esc(({Connection:c.vision_model,Ear:`${c.whisper_model} · ${c.whisper_device} · ${c.whisper_compute_type}`,Eye:`Every ${c.frame_interval_seconds}s · ${c.vision_max_width}px · batches of ${c.vision_batch_size}`,'Temporal & multi-pass':c.multi_pass_apply_cuts?'Targeted cuts enabled':'Review before applying targeted cuts',Brain:`Minimum surviving segment ${c.full_edit_min_segment_seconds}s`,Preview:`${c.preview_min_clip_seconds}–${c.preview_max_clip_seconds}s clips`,Reel:`${c.reel_target_seconds}s target · multiple sources`,Voice:c.performer||'No performer configured',Blade:`CRF ${c.blade_crf} · ${c.blade_preset} · ${c.blade_output_fps}fps`})[group]||'');}
function currentReview(){return state.job?.review||{source_sha256:state.job?.source_sha256||state.job?.source_fingerprint?.sha256||'',timebase:'source',cut_intervals:[],keep_intervals:[]};}
function renderReview(){
  const job=state.job,review=currentReview();
  $('#inspector-content').innerHTML=!job?'<div class="empty-state"><h3>No review yet</h3><p>Analyze a source to inspect its evidence.</p></div>':`${aiSummary()}<div class="inspector-section"><h3>Source-time decisions</h3><p>Mark a source range with I / O. Keep and Protect override model cuts; explicit human cuts take precedence.</p><div class="range-fields"><label>In (s)<input id="review-in" type="number" min="0" step=".01" value="${round(state.in)}"></label><label>Out (s)<input id="review-out" type="number" min="0" step=".01" value="${round(state.out)}"></label></div><input id="review-reason" placeholder="Reason for this decision" aria-label="Review reason" value="${esc(state.reviewEditing?.reason||'')}"><div class="review-buttons">${button('keep','Keep','ok')}${button('cut','Cut','danger')}${button('protect','Protect')}</div>${state.reviewEditing?button('remove-review','Remove selected decision','wide danger'):''}${button('replan','Re-plan with my decisions','wide accent')}</div>${proposalSection()}<details class="inspector-section" open><summary>Needs your eyes <small>${job.review_intervals?.length||0}</small></summary>${(job.review_intervals||[]).map((r,i)=>`<button class="evidence-item" data-queue="${i}"><span>${tc(r.start)} → ${tc(r.end)}</span><small>${esc((r.reasons||[]).join(' · '))}</small></button>`).join('')||'<p>No uncertain intervals.</p>'}</details><details class="inspector-section" open><summary>Your decisions</summary>${['cut','keep'].flatMap(kind=>(review[kind+'_intervals']||[]).map((r,i)=>`<button class="decision-row ${kind}" data-review-kind="${kind}" data-review-index="${i}"><i></i><span>${tc(r.start)} – ${tc(r.end)}<small>${esc(r.reason||kind)}</small></span></button>`)).join('')||'<p>No decisions saved yet.</p>'}</details><details class="inspector-section" open><summary>Nearby evidence</summary><div id="nearby-evidence"></div></details><details class="inspector-section"><summary>Transcript <small>${job.transcript_segments?.length||0}</small></summary>${(job.transcript_segments||[]).map(s=>`<button class="transcript-line" data-source-time="${s.start}"><time>${short(s.start)}</time>${esc(s.text)}</button>`).join('')||'<p>No transcript available.</p>'}${button('transcribe','Re-transcribe (.srt)','wide')}</details><details class="inspector-section"><summary>Caption</summary><p>${esc(job.caption||'No caption generated. Configure a performer and persona in Voice settings.')}</p></details>`;
  renderEvidence();
}
function aiSummary(){
  const job=state.job,obs=job?.observations||[];if(!obs.length)return '';
  const avg=obs.reduce((s,o)=>s+(Number(o.score)||0),0)/obs.length,flagged=obs.filter(o=>o.keep===false).length,proposals=aiProposals(),pending=proposals.filter(p=>p.status==='pending').length;
  const counts=Array.from({length:10},(_,i)=>obs.filter(o=>Math.min(10,Math.max(1,Math.round(Number(o.score)||1)))===i+1).length),peak=Math.max(1,...counts);
  const stat=(value,label)=>`<span><b>${value}</b><small>${label}</small></span>`;
  return `<div class="inspector-section ai-summary"><h3>AI read of this source</h3><div class="ai-stats">${stat(obs.length,'frames judged')}${stat(avg.toFixed(1),'average score')}${stat(Math.round(flagged/obs.length*100)+'%','frames flagged')}${stat(`${proposals.length} · ${Math.round(A.total(proposals))}s`,'cut proposals')}${stat(job.review_intervals?.length||0,'need your eyes')}${stat(job.scene_boundaries?.length||0,'scene changes')}</div>
    <div class="score-histogram" role="img" aria-label="Frame score distribution">${counts.map((n,i)=>`<i style="height:${Math.max(n?8:2,n/peak*100)}%;--h:${(i+1)/10}" title="${n} frame${n===1?'':'s'} scored ${i+1}/10"></i>`).join('')}</div><div class="histogram-axis"><span>1</span><span>score</span><span>10</span></div>
    ${pending?button('next-proposal',`Review ${pending} pending proposal${pending>1?'s':''} · N`,'wide accent'):''}</div>`;
}
function proposalSection(){
  const all=aiProposals(),pending=all.filter(p=>p.status==='pending');if(!all.length)return '';
  const done=all.length-pending.length,seconds=A.total(pending),strong=pending.filter(p=>p.strength==='strong');
  const chip=p=>`<span class="strength ${p.strength}" title="${p.frames?`${p.frames} frame${p.frames>1?'s':''} inside, mean score ${p.meanScore}/10`:'No sampled frame inside; transcript or merge evidence'}">${p.strength==='strong'?'Strong':p.strength==='weak'?'Weak · scored '+p.meanScore:'Likely'}</span>`;
  return `<details class="inspector-section proposals" open><summary>AI cut proposals <small>${pending.length} pending · ${done}/${all.length} decided</small></summary><div class="proposal-meter" role="img" aria-label="${done} of ${all.length} proposals decided"><i style="width:${all.length?done/all.length*100:0}%"></i></div><p>Accept to make a model cut authoritative; reject to protect the span. <kbd>N</kbd> next · <kbd>A</kbd> accept · <kbd>X</kbd> reject.</p>${all.map((p,i)=>`<div class="proposal ${p.status} ${i===state.proposal?'current':''}"><button class="proposal-seek" data-proposal="${i}"><span>${tc(p.start)} → ${tc(p.end)} ${chip(p)}</span><small>${esc(reasonText(p))}</small></button>${p.status==='pending'?`${button('accept-proposal',icon('check'),'icon-button ok',`data-index="${i}" aria-label="Accept proposal ${i+1} as a cut" title="Accept · A"`)}${button('reject-proposal',icon('x'),'icon-button',`data-index="${i}" aria-label="Reject proposal ${i+1}" title="Reject and protect · X"`)}`:`<span class="proposal-state">${p.status}</span>`}</div>`).join('')}${pending.length?`<div class="review-buttons">${strong.length&&strong.length<pending.length?button('accept-strong',`Accept ${strong.length} strong`,'danger','title="Proposals whose own frames scored 3/10 or lower"'):''}${button('accept-all',`Accept all ${pending.length} · −${secs(seconds)}`,'danger')}${button('reject-all','Reject all','ok')}</div>`:''}</details>`;
}
function renderEvidence(){const node=$('#nearby-evidence');if(!node)return;const items=(state.job?.observations||[]).filter(o=>Math.abs(o.timestamp-state.sourceTime)<4);node.innerHTML=items.map(o=>`<div class="evidence-card">${badge(o.keep?'Keep':'Cut',o.keep?'ok':'warn')}<time>${short(o.timestamp)}</time><p>${esc(o.description)}</p><small>Score ${o.score}/10 · confidence ${Math.round((o.confidence??1)*100)}%</small></div>`).join('')||'<p>No observations near this frame.</p>';}
async function saveReview(kind){
  if(!state.job||state.source!==state.job.source)throw new Error('Select the sequence’s original source for source-time review');
  const start=Number($('#review-in').value),finish=Number($('#review-out').value);if(!(start>=0&&finish>start&&finish<=state.job.duration))throw new Error('Choose an ordered range within the source duration');
  const review=T.clone(currentReview());if(state.reviewEditing)review[state.reviewEditing.kind+'_intervals'].splice(state.reviewEditing.index,1);
  const field=kind==='cut'?'cut_intervals':'keep_intervals',reason=$('#review-reason').value.trim()||(kind==='protect'?'Protected content':kind==='keep'?'Keep content':'Editor cut');
  review[field].push({start,end:finish,reason});
  const result=await post(jobURL('review'),review);state.job=result.job;state.reviewEditing=null;renderInspector();renderSourceEvidence();renderTimeline();toast('Review decision saved · Re-plan to apply it');
}

function openDialog(content,kind){pause();const dialog=$('#dialog');if(dialog.open)dialog.close();dialogFocus=document.activeElement;dialog.innerHTML=content;dialog.dataset.kind=kind;dialog.showModal();dialog.querySelector('input,button,select')?.focus();}
function closeDialog(){const d=$('#dialog');d.close();dialogFocus?.focus();}
function dialogHead(eyebrow,title){return `<header class="dialog-header"><div><span class="eyebrow">${eyebrow}</span><h2>${title}</h2></div>${button('close-dialog',icon('x'),'icon-button','aria-label="Close dialog"')}</header>`;}
const labels={lm_studio_url:'Gateway base URL',vision_model:'Vision model',vision_api_key:'API key',caption_model:'Caption model override',whisper_compute_type:'Compute type',multi_pass_apply_cuts:'Apply targeted cuts',auto_render:'Auto-render in watch mode',preview_target_seconds:'Fixed preview target (optional)'};
const labelFor=key=>labels[key]||key.replace(/_/g,' ').replace(/^./,c=>c.toUpperCase());
function settingControl(key,spec){
  const value=state.config[key]??spec.default,label=labelFor(key),id='setting-'+key;
  let control;
  if(spec.type==='boolean')control=`<input type="checkbox" id="${id}" name="${key}" ${value?'checked':''}>`;
  else if(['number','optional_number'].includes(spec.type))control=`<div class="number-setting">${spec.type==='number'?`<input type="range" data-setting-range="${key}" aria-label="${esc(label)} slider" min="${spec.min}" max="${spec.max}" step="${spec.step}" value="${value}">`:''}<input type="number" id="${id}" name="${key}" min="${spec.min}" max="${spec.max}" step="${spec.step}" value="${value??''}" ${spec.type==='number'?'required':''}></div>`;
  else if(['textarea','list','json'].includes(spec.type))control=`<textarea id="${id}" name="${key}" rows="${spec.type==='json'?5:3}">${esc(spec.type==='json'?JSON.stringify(value,null,2):spec.type==='list'?(value||[]).join('\n'):value)}</textarea>`;
  else if(spec.options&&spec.type!=='model')control=`<select id="${id}" name="${key}">${spec.options.map(o=>`<option ${o===value?'selected':''}>${esc(o)}</option>`).join('')}</select>`;
  else control=`<input id="${id}" name="${key}" type="${spec.type==='password'?'password':'text'}" value="${spec.type==='password'?'':esc(value)}" ${spec.type==='model'?`list="models-${key}"`:''} ${spec.type==='password'?'autocomplete="new-password" placeholder="Leave blank to keep current key"':''}>${spec.type==='model'?`<datalist id="models-${key}">${(spec.options||[state.config.vision_model]).map(o=>`<option value="${esc(o)}"></option>`).join('')}</datalist>`:''}`;
  return `<div class="setting-field ${spec.type==='boolean'?'toggle-row':''}" data-key="${key}"><label for="${id}">${esc(label)}</label>${control}<small>${spec.type==='password'?`Key source: ${esc(state.config.api_key_source)}. Empty keeps the current credential; clear to use NEXUS_LLM_API_KEY. <button type="button" data-action="clear-key" class="inline">Clear saved key</button>`:spec.type==='list'?'One term per line':spec.type==='json'?'JSON object: performer name → persona instructions':spec.type==='model'?'Choose a model or enter its exact gateway ID':esc(key)}</small></div>`;
}
function openSettings(group='Connection'){
  const groups=[...new Set(Object.values(state.schema).map(s=>s.group))];
  openDialog(`${dialogHead('PROJECT','Pipeline settings')}<form id="settings-form"><div class="settings-layout"><nav class="settings-nav" aria-label="Settings sections">${groups.map(g=>`<a href="#settings-${g.replace(/\W/g,'')}" data-settings-nav="${esc(g)}">${esc(g)}</a>`).join('')}</nav><div class="settings-content">${groups.map(g=>`<details id="settings-${g.replace(/\W/g,'')}" data-settings-section="${esc(g)}" ${g===group?'open':''}><summary>${esc(g)}<small>${Object.values(state.schema).filter(s=>s.group===g).length} settings</small></summary><div class="settings-fields">${Object.entries(state.schema).filter(([,s])=>s.group===g).map(([k,s])=>settingControl(k,s)).join('')}</div></details>`).join('')}</div></div><footer class="dialog-footer"><span>Changes apply to the next task.</span>${button('settings-test','Test connection')}${button('close-dialog','Cancel')}<button class="primary" type="submit">Save settings</button></footer></form>`,'settings');
}
function settingsValues(){
  const values={};for(const [key,spec] of Object.entries(state.schema)){const input=$(`[name="${key}"]`,$('#settings-form'));if(!input)continue;
    if(spec.type==='password'){if(input.dataset.clear)values[key]=null;else if(input.value)values[key]=input.value;continue;}
    if(spec.type==='boolean')values[key]=input.checked;
    else if(['number','optional_number'].includes(spec.type))values[key]=input.value===''?null:Number(input.value);
    else if(spec.type==='json')values[key]=JSON.parse(input.value||'{}');
    else if(spec.type==='list')values[key]=input.value.split('\n').map(v=>v.trim()).filter(Boolean);
    else values[key]=input.value===''&&spec.default===null?null:input.value;
  }return values;
}
function analyzeDialog(){
  if(!state.project)return newProjectDialog();if(!assets().some(f=>f.kind==='video'))return toast('Import a video into this project first','warn');
  openDialog(`${dialogHead('PIPELINE','Analyze source')}<form id="analyze-form"><div class="dialog-body"><label class="stacked">Source<select name="source" required>${assets().filter(f=>f.kind==='video'&&f.job?.source_available!==false).map(f=>`<option value="${esc(f.path)}" ${state.source===f.path?'selected':''}>${esc(f.name)}</option>`).join('')}</select></label><h3>Stages to run</h3><div class="stage-options">${[['ear','Ear','Transcribe speech locally'],['eye','Eye','Judge frames and scene boundaries'],['voice','Voice','Write a persona caption']].map(([id,name,description])=>`<label><input type="checkbox" name="stages" value="${id}" checked><span><strong>${name}</strong><small>${description}</small></span></label>`).join('')}</div><p class="field-note">Unchecked Ear/Eye stages reuse saved evidence when available. Voice is skipped. No render runs during analysis.</p><label class="toggle-row">Refresh selected stages from scratch<input type="checkbox" name="refresh"></label></div><footer class="dialog-footer">${button('settings','Pipeline settings')}${button('close-dialog','Cancel')}<button type="submit" class="primary">Start analysis</button></footer></form>`,'analyze');
}
function previewDialog(reel=false){
  openDialog(`${dialogHead('ASSEMBLY',reel?'Build a best-of reel':'Preview reel')}<form id="preview-form" data-reel="${reel}"><div class="dialog-body"><p>${reel?'Select analyzed sources. Brain ranks moments across them.':'Build a short trailer from the strongest moments in this source.'}</p><label class="stacked">Target duration (seconds)<input name="target" type="number" min="1" max="3600" value="${reel?state.config.reel_target_seconds:state.config.preview_target_seconds||state.config.preview_max_target_seconds||30}" required></label>${reel?`<div class="reel-sources">${assets().map(a=>a.job).filter(j=>j?.source_available&&j.clips?.length).map(j=>`<label><input type="checkbox" name="jobs" value="${esc(j.id)}" ${state.checked.has(j.id)?'checked':''}><span>${esc(basename(j.source))}</span><small>${short(j.duration)}</small></label>`).join('')}</div>`:''}</div><footer class="dialog-footer">${button('close-dialog','Cancel')}<button class="primary" type="submit">${reel?'Build reel':'Build preview'}</button></footer></form>`,'preview');
}
async function startTask(path,payload,callback){const task=await post(path,payload);if(!task.id)throw new Error('Server did not return a task');if(callback)state.taskCallbacks.set(task.id,callback);state.tasks.unshift(task);toast(task.label+' · queued');renderTasks();}
function taskList(){return state.tasks.slice(0,10).map(t=>`<div class="task-card"><div><strong>${esc(t.label)}</strong>${badge(t.status,t.status==='failed'?'error':t.status==='succeeded'?'ok':'')}</div><small>${esc(t.error||t.stage||t.status)}</small>${['running','queued'].includes(t.status)?`<progress max="100" value="${t.progress||0}" aria-label="${esc(t.label)} stage progress"></progress>`:''}</div>`).join('')||'<p>No background tasks.</p>';}
function renderTasks(){const active=state.tasks.filter(t=>['running','queued'].includes(t.status));$('#tasks-status').textContent=active.length?`${active.length} active · ${active[0].stage||'Starting'} ${active[0].progress||0}%`:'No active tasks';
  if(active.length)document.title=`${active[0].progress||0}% · ${active[0].label} — SmartCut`;else if(!document.title.startsWith('✓'))document.title='SmartCut';if($('#task-list'))$('#task-list').innerHTML=taskList();}
async function pollTasks(){
  try{
    state.tasks=await api('/api/tasks');renderTasks();
    for(const task of state.tasks){
      if(!['succeeded','failed'].includes(task.status)||state.handled.has(task.id))continue;
      state.handled.add(task.id);const callback=state.taskCallbacks.get(task.id);state.taskCallbacks.delete(task.id);
      if(task.status==='failed'){if(task.label==='Test gateway connection'){state.gateway='failed';renderGateway();}fail(new Error(task.label+': '+task.error),callback?.retry||null);}
      else {toast(task.label+' complete','ok');if(document.hidden)document.title=`✓ ${task.label} — SmartCut`;if(callback)await callback(task.result);}
    }
  }catch(error){$('#tasks-status').textContent='Task polling unavailable · retrying';}
}
function renderGateway(){const names={untested:'Not tested',testing:'Testing…',ok:'Connected',failed:'Connection failed'};$('#gateway-status').innerHTML=badge(names[state.gateway],state.gateway==='ok'?'ok':state.gateway==='failed'?'error':'');}
async function testConnection(values){state.gateway='testing';renderGateway();await startTask('/api/connection/test',values?{values}:undefined,()=>{state.gateway='ok';renderGateway();});}
function download(path){const a=document.createElement('a');a.href=fileURL(path);a.download=basename(path);document.body.append(a);a.click();a.remove();}
async function exportFile(format){
  needSequence();if(format==='srt')return exportCaptions();await saveSequence();
  if(format==='mp4'){await renderCut();return;}
  const result=await post(projectURL('export'),{format});download(result.path);toast(format.toUpperCase()+' exported','ok');
  if(result.warnings?.length)openDialog(`${dialogHead('EXPORT','Export notes')}<div class="dialog-body">${result.warnings.map(w=>`<p>${esc(w)}</p>`).join('')}</div><footer class="dialog-footer">${button('close-dialog','Done','primary')}</footer>`,'export');
}
async function renderCut(){needSequence();await saveSequence(true);const id=state.project.id;await startTask(projectURL('render'),{},async()=>{await load();if(state.project?.id!==id)return;state.programMode='final';$('#program-mode').value='final';state.time=0;renderProgram();});}
async function replan(){if(!state.job)throw new Error('Select an analyzed source first');await saveSequence();const id=state.project?.id;await startTask(jobURL('replan'),{},async()=>{await load();if(state.project?.id===id)toast('Plan updated · use Auto-edit or Load approved plan to apply it to the timeline');});}
async function transcribe(){if(!state.source)throw new Error('Select a video or audio source first');await startTask('/api/actions/transcribe',{source:state.source,refresh:true},result=>{download(result.path);return load();});}
async function importFiles(files){if(!files.length)return;if(!state.project){newProjectDialog();throw new Error('Create or open a project before importing media');}const id=state.project.id,form=new FormData();[...files].forEach(f=>form.append('files',f));toast('Importing '+files.length+' files…');const saved=await api('/api/inbox/upload',{method:'POST',body:form});await addAssets(saved.map(f=>f.path),id);await load();toast('Media imported · drag an asset onto the timeline','ok');
  if(state.project?.id===id&&saved[0])await selectAsset(saved[0].path);}
function sequenceSettings(){if(!state.sequence)throw new Error('Select a sequence first');openDialog(`${dialogHead('SEQUENCE','Sequence settings')}<form id="sequence-form"><div class="dialog-body"><div class="preset-row" role="group" aria-label="Format presets">${[['match','Match source'],['1080x1920','9:16 Vertical'],['1920x1080','16:9'],['1080x1080','1:1'],['1080x1350','4:5']].map(([value,label])=>button('format-preset',label,'quiet',`data-preset="${value}"`)).join('')}</div><label class="stacked">Width<input name="width" type="number" min="64" max="4096" step="2" value="${state.sequence.width}" required></label><label class="stacked">Height<input name="height" type="number" min="64" max="4096" step="2" value="${state.sequence.height}" required></label><label class="stacked">Frame rate<input name="fps" type="number" min="1" max="120" value="${state.sequence.fps}" required></label><p>Manual sequences use exact cuts. Transition settings apply to generated previews and reels.</p></div><footer class="dialog-footer">${button('close-dialog','Cancel')}<button type="submit" class="primary">Apply</button></footer></form>`,'sequence');}

const actions={
  import:()=>state.project?$('#file-input').click():newProjectDialog(),'new-project':newProjectDialog,'open-project':openProjectDialog,
  'choose-project':async t=>{await openProject(t.dataset.id);closeDialog();},'open-legacy':t=>createProject(basename(state.jobs.find(j=>j.id===t.dataset.id).source).replace(/\.[^.]+$/,''),t.dataset.id),'add-existing':existingMediaDialog,'remove-asset':removeAsset,
  settings:()=>openSettings(),connection:()=>testConnection(),refresh:async()=>{await load();toast('Project refreshed');},
  analyze:analyzeDialog,preview:()=>previewDialog(),reel:()=>previewDialog(true),render:renderCut,replan,transcribe,rebuild:rebuildSequence,
  folder:async()=>{await post('/api/open-folder?path='+encodeURIComponent(state.project?.final_output?.path||state.config.output_dir));toast('Output folder opened');},
  'close-dialog':closeDialog,'dismiss-error':clearError,retry:()=>state.retry?.(),'retry-media':()=>{renderSource();renderProgram();},
  'settings-test':()=>testConnection(settingsValues()),'clear-key':()=>{const key=$('[name="vision_api_key"]');key.value='';key.dataset.clear='true';key.placeholder='Saved key will be cleared on Save';},
  'list-view':()=>{state.view='list';renderProject();},'icon-view':()=>{state.view='icons';renderProject();},
  save:async()=>{await saveSequence(true);toast('Sequence saved','ok');},undo:()=>undo(),redo:()=>undo(true),
  'select-tool':()=>setTool('select'),'razor-tool':()=>setTool('razor'),
  split:()=>edit('Clip split',s=>T.split(s,state.selected,state.time,state.linked)),
  delete:()=>edit('Clips removed',s=>T.remove(s,selectedIds(),false,state.linked)),
  ripple:()=>edit('Gaps closed',s=>T.remove(s,selectedIds(),true,state.linked)),
  duplicate:()=>edit('Clip duplicated at sequence end',s=>T.duplicate(s,selectedIds(),state.linked)),
  enable:()=>edit('Clip enabled state changed',s=>{const clips=T.linkedClips(s,selectedIds(),state.linked);if(clips.some(c=>T.locked(s,c.track)))throw new Error('Unlock the track first');const enabled=!clips[0].enabled;clips.forEach(c=>c.enabled=enabled);}),
  snap:()=>{state.snap=!state.snap;$$('[data-action="snap"]').forEach(b=>{b.classList.toggle('active',state.snap);b.setAttribute('aria-pressed',state.snap);});},
  linked:()=>{state.linked=!state.linked;$$('[data-action="linked"]').forEach(b=>{b.classList.toggle('active',state.linked);b.setAttribute('aria-pressed',state.linked);});},
  in:()=>{state.in=state.sourceTime;syncTransport();if(state.tab==='review')renderReview();},out:()=>{state.out=state.sourceTime;syncTransport();if(state.tab==='review')renderReview();},
  marker:()=>edit('Marker added',s=>s.markers.push({id:T.uid(),time:state.time,label:'Marker '+(s.markers.length+1)})),
  insert:()=>addSource(false),overwrite:()=>addSource(true),keep:()=>saveReview('keep'),cut:()=>saveReview('cut'),protect:()=>saveReview('protect'),
  'remove-review':async()=>{const r=T.clone(currentReview()),e=state.reviewEditing;if(!e)return;r[e.kind+'_intervals'].splice(e.index,1);state.job=(await post(jobURL('review'),r)).job;state.reviewEditing=null;renderReview();toast('Decision removed');},
  'source-play':()=>togglePlay('source'),'program-play':()=>togglePlay('program'),
  'source-start':()=>seekSource(0),'program-start':()=>seekProgram(0),
  'source-back':()=>seekSource(state.sourceTime-1/30),'source-next':()=>seekSource(state.sourceTime+1/30),
  'program-back':()=>seekProgram(state.time-1/(state.sequence?.fps||30)),'program-next':()=>seekProgram(state.time+1/(state.sequence?.fps||30)),
  'rotate-source':()=>{state.rotation=((state.rotation||0)+90)%360;const m=$('#source-media');if(m)m.style.transform=`rotate(${state.rotation}deg)`;},
  tasks:()=>{state.tab='pipeline';renderInspector();$('#task-list')?.scrollIntoView({block:'nearest'});},
  'sequence-settings':sequenceSettings,
  'auto-edit':()=>autoEditDialog(),'auto-silence':()=>autoEditDialog('silence'),'auto-waste':()=>quickAuto({waste:true}),'auto-gaps':()=>quickAuto({gaps:true}),
  'auto-scenes':()=>quickAuto({scenes:true}),'auto-highlights':()=>quickAuto({highlights:true}),'auto-fill':()=>fillFrame(),'auto-fit':()=>fillFrame(true),
  'auto-threshold':async()=>{const source=state.source||state.sequence.clips.find(c=>c.track==='A1')?.source;if(!source)throw new Error('Import audio first');const levels=levelNote(A.analyzeLevels((await peaksFor(source)).peaks||[]));$('[name="threshold"]').value=levels.threshold;updateAutoPreview();},
  'next-proposal':()=>stepProposal(1),'accept-proposal':t=>{focusProposal(Number(t.dataset.index));return decideCurrent('accept');},'reject-proposal':t=>{focusProposal(Number(t.dataset.index));return decideCurrent('reject');},
  'accept-all':()=>decideProposals(aiProposals().filter(p=>p.status==='pending'),'accept'),'accept-strong':()=>decideProposals(aiProposals().filter(p=>p.status==='pending'&&p.strength==='strong'),'accept'),'reject-all':()=>decideProposals(aiProposals().filter(p=>p.status==='pending'),'reject'),
  'toggle-lanes':()=>{state.prefs.lanes=!state.prefs.lanes;savePrefs();syncToggles();renderTimeline();},
  'toggle-cc':()=>{state.prefs.cc=!state.prefs.cc;savePrefs();syncToggles();overlayKey='';renderOverlay();},
  'toggle-hud':()=>{state.prefs.hud=!state.prefs.hud;savePrefs();syncToggles();overlayKey='';renderOverlay();},
  'theme-cycle':()=>setTheme(Theme.nextMode(state.themeMode)),'theme-auto':()=>setTheme('auto'),'theme-light':()=>setTheme('light'),'theme-dark':()=>setTheme('dark'),
  'format-preset':async t=>{const form=$('#sequence-form');let [w,h]=t.dataset.preset.split('x').map(Number);if(t.dataset.preset==='match'){if(!state.source)throw new Error('Select a project asset first');const m=await metadata(state.source);[w,h]=[m.width,m.height];}form.width.value=Math.round(w/2)*2;form.height.value=Math.round(h/2)*2;},
  audition,'prev-edit':()=>editPoint(-1),'next-edit':()=>editPoint(1),'frame-back':()=>actions[state.focus==='source'?'source-back':'program-back'](),'frame-next':()=>actions[state.focus==='source'?'source-next':'program-next'](),
  'zoom-fit':()=>{state.fitDuration=Math.max(10,T.sequenceDuration(state.sequence||{clips:[]}));setZoom(1,0);$('#timeline-scroll').scrollLeft=0;},'zoom-in':()=>setZoom(state.zoom*1.5),'zoom-out':()=>setZoom(state.zoom/1.5),
  'delete-marker':t=>{const id=t.dataset.markerId;closeDialog();edit('Marker deleted',s=>{s.markers=s.markers.filter(m=>m.id!==id);});},
  'clear-markers':()=>{needSequence();if(!state.sequence.markers.length)return toast('No markers to clear','warn');edit(`${state.sequence.markers.length} markers cleared`,s=>{s.markers=[];});},
  shortcuts:shortcutsDialog,'prev-proposal':()=>stepProposal(-1),'accept-current':()=>decideCurrent('accept'),'reject-current':()=>decideCurrent('reject'),
  'reset-effects':()=>edit('Effects reset',s=>{const clips=T.linkedClips(s,selectedIds(),state.linked);if(clips.some(c=>T.locked(s,c.track)))throw new Error('Unlock the track first');clips.forEach(c=>Object.assign(c,{speed:1,opacity:1,scale:1,x:0,y:0,rotation:0,volume:1}));}),
};
function setTool(tool){state.tool=tool;$$('[data-action$="-tool"]').forEach(b=>b.classList.toggle('active',b.dataset.action===tool+'-tool'));$('#timeline-canvas').classList.toggle('razor',tool==='razor');}
function selectedIds(){return state.selection.size?[...state.selection]:state.selected?[state.selected]:[];}
function paintSelection(){
  const valid=new Set(state.sequence?.clips.map(c=>c.id)||[]);state.selection=new Set([...state.selection].filter(id=>valid.has(id)));
  if(!state.selection.has(state.selected))state.selected=[...state.selection][0]||null;
  $$('[data-clip]').forEach(c=>{const selected=state.selection.has(c.dataset.clip);c.classList.toggle('selected',selected);c.setAttribute('aria-pressed',selected);});
}
function chooseClip(id,add=false){if(add){if(state.selection.has(id))state.selection.delete(id);else state.selection.add(id);}else state.selection=new Set([id]);state.selected=state.selection.has(id)?id:[...state.selection][0]||null;state.focus='program';paintSelection();renderInspector();}
function timelineAt(event){const rect=$('#timeline-canvas').getBoundingClientRect();return Math.max(0,(event.clientX-rect.left)/pps());}
function mediaClips(path,info,track,start=0,finish=info.duration){
  const kind=kindOf(path),link=T.uid(),base={source:path,source_sha256:info.sha256,kind,source_start:start,source_end:finish,start:0,speed:1,opacity:1,scale:1,x:0,y:0,rotation:0,volume:1,enabled:true,link_id:link,name:basename(path)};
  if((kind==='audio')!==(track[0]==='A'))throw new Error('Drop video and images on V tracks, audio on A tracks');
  return [{...base,id:T.uid(),track},...(kind==='video'&&info.has_audio?[{...base,id:T.uid(),track:track==='V1'?'A1':'A2'}]:[])];
}
let mediaDrag=null,dragFrame=0;
function clearDropPreview(){
  cancelAnimationFrame(dragFrame);dragFrame=0;$$('.drop-preview,.drop-snap').forEach(n=>n.remove());
  if(mediaDrag){mediaDrag.preview=null;mediaDrag.pointer=null;if(mediaDrag.width)$('#timeline-canvas').style.width=mediaDrag.width;}
}
function updateDropPreview(event){
  const drag=mediaDrag,canvas=$('#timeline-canvas');if(!drag||!state.sequence||drag.project!==state.project?.id)return;
  drag.pointer={clientX:event.clientX,clientY:event.clientY};
  const row=document.elementFromPoint(event.clientX,event.clientY)?.closest('.track-row');
  if(!row||!canvas.contains(row)){clearDropPreview();return;}
  const info=drag.info;if(!info?.duration)return;
  const scale=pps(),track=row.dataset.track,position=T.dropPlacement(state.sequence,timelineAt(event),info.duration,scale,state.snap,[state.time]);
  let clips=[],error=null;try{clips=mediaClips(drag.path,info,track);T.insert(T.clone(state.sequence),clips,position.start);}catch(err){error=err;}
  drag.preview={...position,track,error,scale,duration:info.duration};
  $$('.drop-preview,.drop-snap').forEach(n=>n.remove());
  const tracks=clips.length?clips.map(c=>c.track):[track];
  canvas.style.width=Math.max(parseFloat(drag.width)||0,(position.start+info.duration)*scale+60)+'px';
  for(const id of tracks){const target=$(`[data-track="${id}"]`,canvas),ghost=document.createElement('div');ghost.className='drop-preview'+(error?' invalid':'');ghost.style.left=position.start*scale+'px';ghost.style.width=info.duration*scale+'px';ghost.dataset.start=position.start;ghost.dataset.duration=info.duration;ghost.textContent=error?error.message:`${basename(drag.path)} · ${tc(position.start)} → ${tc(position.start+info.duration)}`;target.append(ghost);}
  if(position.snap!==null){const line=document.createElement('div');line.className='drop-snap';line.style.left=position.snap*scale+'px';canvas.append(line);}
  if(event.dataTransfer)event.dataTransfer.dropEffect=error?'none':'copy';
  if(!dragFrame){const scroll=()=>{dragFrame=0;if(!mediaDrag?.pointer)return;const p=mediaDrag.pointer,s=$('#timeline-scroll'),r=s.getBoundingClientRect();const dx=p.clientX>r.right-30?12:p.clientX<r.left+30?-12:0;if(dx){s.scrollLeft+=dx;updateDropPreview(p);}if(mediaDrag?.pointer&&!dragFrame)dragFrame=requestAnimationFrame(scroll);};dragFrame=requestAnimationFrame(scroll);}
}
function bindMediaDrag(){
  document.addEventListener('dragstart',e=>{const row=e.target.closest('[data-source]');if(!row||!state.project)return;const path=row.dataset.source,asset=assets().find(a=>a.path===path);if(asset?.source_available===false){e.preventDefault();return;}
    e.dataTransfer.setData('text/anna-source',path);e.dataTransfer.effectAllowed='copy';
    const drag=mediaDrag={path,project:state.project.id,info:state.meta.get(path)||asset,width:$('#timeline-canvas').style.width,pointer:null};
    drag.ready=metadata(path).then(info=>{drag.info=info;if(mediaDrag===drag&&drag.pointer)updateDropPreview(drag.pointer);return info;});drag.ready.catch(error=>{if(mediaDrag===drag){clearDropPreview();fail(error);}});
  });
  document.addEventListener('dragend',()=>{clearDropPreview();mediaDrag=null;});
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){clearDropPreview();mediaDrag=null;}});
  const panel=$('.project-panel'),canvas=$('#timeline-canvas');
  for(const target of [panel,canvas]){
    target.addEventListener('dragover',e=>{if(!state.project)return;e.preventDefault();if(target===canvas&&mediaDrag)updateDropPreview(e);else if([...e.dataTransfer.types].includes('Files'))panel.classList.add('drop-active');});
    target.addEventListener('dragleave',e=>{if(target.contains(e.relatedTarget))return;target.classList.remove('drop-active');if(target===canvas)clearDropPreview();});
    target.addEventListener('drop',e=>{e.preventDefault();panel.classList.remove('drop-active');if(target===canvas&&mediaDrag)updateDropPreview(e);const drag=mediaDrag,placement=drag?.preview;clearDropPreview();mediaDrag=null;
      operation('Drop media',async()=>{if(e.dataTransfer.files.length)return importFiles(e.dataTransfer.files);if(target!==canvas||!drag||!placement)return;if(placement.error)throw placement.error;const info=await drag.ready;if(state.project?.id!==drag.project)throw new Error('Project changed during the drag');if(Math.abs(info.duration-placement.duration)>.001)throw new Error('Media duration changed. Drag the asset again.');const clips=mediaClips(drag.path,info,placement.track);edit('Media added to timeline',seq=>T.insert(seq,clips,placement.start));state.time=placement.start;chooseClip(clips[0].id);syncTransport();});
    });
  }
}
function bind(){
  document.addEventListener('click',event=>{
    const target=event.target.closest('button,[data-source],a[data-settings-nav]');
    if(!event.target.closest('#context-menu'))$('#context-menu').hidden=true;
    if(target?.dataset.action){const action=target.dataset.action;$('#context-menu').hidden=true;$$('.menu[open]').forEach(m=>m.removeAttribute('open'));operation(action,()=>action.startsWith('export-')?exportFile(action.slice(7)):actions[action]?.(target));return;}
    if(target?.dataset.proposal!==undefined){focusProposal(Number(target.dataset.proposal));return;}
    if(event.target.matches('[data-job-check]')){const id=event.target.dataset.jobCheck;if(event.target.checked)state.checked.add(id);else state.checked.delete(id);renderProject();return;}
    if(target?.dataset.source)operation('Load source',()=>selectAsset(target.dataset.source));
    if(target?.dataset.bin){state.bin=target.dataset.bin;renderProject();}
    if(target?.dataset.sort){state.sort=target.dataset.sort;renderProject();}
    if(target?.dataset.tab){state.tab=target.dataset.tab;renderInspector();}
    if(target?.dataset.settingsGroup)openSettings(target.dataset.settingsGroup);
    if(target?.dataset.settingsNav){event.preventDefault();const section=$(`[data-settings-section="${target.dataset.settingsNav}"]`);section.open=true;section.scrollIntoView({block:'start'});}
    if(target?.dataset.trackMute)operation('Track mute',()=>edit('Track visibility / mute changed',s=>{const t=s.tracks.find(t=>t.id===target.dataset.trackMute);if(t.locked)throw new Error('Unlock the track first');t.muted=!t.muted;}));
    if(target?.dataset.trackLock)operation('Track lock',()=>edit('Track lock changed',s=>{const t=s.tracks.find(t=>t.id===target.dataset.trackLock);t.locked=!t.locked;}));
    if(target?.dataset.marker){const m=state.sequence.markers.find(m=>m.id===target.dataset.marker);if(m)seekProgram(m.time);}
    if(target?.dataset.sourceTime)seekSource(Number(target.dataset.sourceTime));
    if(target?.dataset.queue!==undefined){const r=state.job.review_intervals[Number(target.dataset.queue)];state.in=r.start;state.out=r.end;state.reviewEditing=null;seekSource(r.start);renderReview();}
    if(target?.dataset.reviewKind){const kind=target.dataset.reviewKind,index=Number(target.dataset.reviewIndex),r=currentReview()[kind+'_intervals'][index];state.reviewEditing={kind,index,reason:r.reason};state.in=r.start;state.out=r.end;seekSource(r.start);renderReview();}
  });
  $('#media-search').addEventListener('input',e=>{state.search=e.target.value;renderProject();});
  $('#file-input').addEventListener('change',e=>operation('Import',async()=>{await importFiles(e.target.files);e.target.value='';}));
  $('#source-scrub').addEventListener('input',e=>seekSource(Number(e.target.value)));
  $('#source-evidence').addEventListener('pointerdown',e=>{if(e.button!==0||!state.job?.duration)return;const strip=e.currentTarget,seek=event=>{const r=strip.getBoundingClientRect();seekSource((event.clientX-r.left)/r.width*state.job.duration);};pause();seek(e);strip.setPointerCapture(e.pointerId);strip.onpointermove=seek;strip.onpointerup=()=>{strip.onpointermove=null;};});
  $('#program-scrub').addEventListener('input',e=>seekProgram(Number(e.target.value)));
  $('#timeline-zoom').addEventListener('input',e=>{state.zoom=Number(e.target.value);renderTimeline();});
  $('#timeline-scroll').addEventListener('scroll',e=>{$('.track-headers').scrollTop=e.target.scrollTop;},{passive:true});
  $('#timeline-scroll').addEventListener('wheel',e=>{if(!(e.ctrlKey||e.altKey))return;e.preventDefault();setZoom(state.zoom*(e.deltaY<0?1.25:.8),e.clientX-e.currentTarget.getBoundingClientRect().left);},{passive:false});
  $('#timeline-canvas').addEventListener('dblclick',e=>{const marker=e.target.closest('[data-marker]');if(marker)markerDialog(marker.dataset.marker);});
  $('#program-mode').addEventListener('change',e=>{pause();state.programMode=e.target.value;state.time=0;renderProgram();syncTransport();});
  $('#source-monitor').addEventListener('pointerdown',()=>state.focus='source');$('#program-monitor').addEventListener('pointerdown',()=>state.focus='program');
  document.addEventListener('change',e=>{
    if(e.target.dataset.effect){const key=e.target.dataset.effect,value=key==='enabled'?e.target.checked:Number(e.target.value);operation('Change effect',()=>edit('Effect updated',s=>{const targets=T.linkedClips(s,state.selected,key==='speed'&&state.linked);if(targets.some(c=>T.locked(s,c.track)))throw new Error('Unlock the track first');targets.forEach(c=>{c[key]=value;});}));}
    if(e.target.id==='review-in'){state.in=Number(e.target.value);syncTransport();}if(e.target.id==='review-out'){state.out=Number(e.target.value);syncTransport();}
  });
  document.addEventListener('input',e=>{if(e.target.closest('#auto-form'))updateAutoPreview();if(e.target.dataset.settingRange){const input=$(`[name="${e.target.dataset.settingRange}"]`);input.value=e.target.value;}if(e.target.name&&state.schema[e.target.name]){const r=$(`[data-setting-range="${e.target.name}"]`);if(r)r.value=e.target.value;}});
  document.addEventListener('submit',e=>{e.preventDefault();operation('Submit',async()=>{
    if(e.target.id==='new-project-form')await createProject(String(new FormData(e.target).get('name')).trim());
    if(e.target.id==='existing-media-form'){const paths=new FormData(e.target).getAll('paths');if(!paths.length)throw new Error('Select media to add');await addAssets(paths);closeDialog();await selectAsset(paths[0]);}
    if(e.target.id==='settings-form'){const values=settingsValues();await api('/api/config',{method:'PUT',body:JSON.stringify({values,revision:state.config.revision,dry_run:true})});const result=await api('/api/config',{method:'PUT',body:JSON.stringify({values,revision:state.config.revision})});state.config={...state.config,...result.config};state.gateway='untested';renderGateway();$('#model-label').textContent=state.config.vision_model;closeDialog();renderInspector();toast('Pipeline settings saved','ok');}
    if(e.target.id==='analyze-form'){const form=new FormData(e.target),source=form.get('source'),stages=form.getAll('stages'),id=state.project?.id;if(!source||!stages.length)throw new Error('Choose a source and at least one stage');closeDialog();await startTask('/api/actions/analyze',{source,stages,refresh:form.has('refresh')},async()=>{await load();if(state.project?.id===id&&state.source===source)await selectAsset(source);});}
    if(e.target.id==='preview-form'){const form=new FormData(e.target),target_seconds=Number(form.get('target')),reel=e.target.dataset.reel==='true',job_ids=form.getAll('jobs');if(reel&&job_ids.length<2)throw new Error('Select at least two sources');if(!reel&&!state.job)throw new Error('Select an analyzed source');closeDialog();await saveSequence();await startTask(reel?'/api/actions/reel':jobURL('preview'),reel?{job_ids,target_seconds}:{target_seconds},async result=>{await load();if(reel){download(result.final_output.path);}else{state.programMode='preview';$('#program-mode').value='preview';state.time=0;renderProgram();}});}
    if(e.target.id==='sequence-form'){const f=new FormData(e.target),reshaped=Number(f.get('width'))/Number(f.get('height'))!==state.sequence.width/state.sequence.height;edit('Sequence format updated',s=>{s.width=Number(f.get('width'));s.height=Number(f.get('height'));s.fps=Number(f.get('fps'));});closeDialog();if(reshaped&&state.sequence.clips.some(c=>c.track[0]==='V'))toast('New aspect ratio · Auto ▸ Fill frame covers it without letterboxing','warn');}
    if(e.target.id==='auto-form')await applyAuto(e.target);
    if(e.target.id==='marker-form'){const f=new FormData(e.target),id=e.target.dataset.markerForm;closeDialog();edit('Marker updated',s=>{const m=s.markers.find(x=>x.id===id);if(!m)throw new Error('Marker no longer exists');m.label=String(f.get('label')).trim().slice(0,200)||'Marker';m.time=Math.max(0,Number(f.get('time'))||0);});}
  });});
  document.addEventListener('keydown',e=>{
    if(e.target.closest('input,textarea,select,[contenteditable="true"]')||$('#dialog').open)return;
    const key=e.key.toLowerCase();
    if((e.ctrlKey||e.metaKey)&&['z','y','s'].includes(key)){e.preventDefault();operation('Keyboard edit',()=>key==='s'?actions.save():undo(key==='y'||e.shiftKey));return;}
    if(e.ctrlKey||e.metaKey||e.altKey)return;
    const map={' ':'play',j:'reverse',k:'pause',l:'forward',c:'split',v:'select-tool',i:'in',o:'out',m:'marker',delete:e.shiftKey?'ripple':'delete',backspace:'delete',n:e.shiftKey?'prev-proposal':'next-proposal',a:'accept-current',x:'reject-current','?':'shortcuts',p:'audition',arrowup:'prev-edit',arrowdown:'next-edit',arrowleft:'frame-back',arrowright:'frame-next','\\':'zoom-fit','=':'zoom-in','+':'zoom-in','-':'zoom-out'};
    if(!map[key]){if(e.key==='Enter'&&e.target.dataset.source)operation('Select source',()=>selectAsset(e.target.dataset.source));return;}
    e.preventDefault();operation('Keyboard edit',()=>{if(map[key]==='play')togglePlay();else if(map[key]==='reverse')shuttle(-1);else if(map[key]==='forward')shuttle(1);else if(map[key]==='pause')pause();else return actions[map[key]]();});
  });
  $('#timeline-canvas').addEventListener('pointerdown',timelinePointer);
  $('#timeline-canvas').addEventListener('contextmenu',e=>{const el=e.target.closest('[data-clip]');if(!el)return;e.preventDefault();if(!state.selection.has(el.dataset.clip))chooseClip(el.dataset.clip);const menu=$('#context-menu');menu.innerHTML=[['split','Split at playhead'],['delete','Delete'],['ripple','Ripple delete'],['duplicate','Duplicate'],['enable','Toggle enabled']].map(([a,l])=>button(a,l,'','role="menuitem"')).join('');menu.style.left=Math.min(e.clientX,innerWidth-210)+'px';menu.style.top=Math.min(e.clientY,innerHeight-220)+'px';menu.hidden=false;});
  bindMediaDrag();
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&document.title.startsWith('✓'))document.title='SmartCut';});
  // Closing the desktop window stops the server, so an unsaved edit or a running render both deserve a warning.
  window.addEventListener('resize',()=>{renderTimeline();syncProgram();});window.addEventListener('beforeunload',e=>{if(state.dirty||state.tasks.some(t=>['running','queued'].includes(t.status))){e.preventDefault();e.returnValue='';}});
}
function timelinePointer(e){
  if(e.button!==0||!state.sequence||e.target.closest('button,input,select,textarea,a,[contenteditable="true"]'))return;
  const el=e.target.closest('[data-clip]'),flag=e.target.closest('[data-proposal-start]');
  if(flag){const index=aiProposals().findIndex(p=>p.start===Number(flag.dataset.proposalStart));if(index>=0){pause();focusProposal(index);return;}}
  if(!el){
    if(e.target.closest('.track-row')&&state.tool==='select'){startMarquee(e);return;}
    pause();const update=event=>seekProgram(timelineAt(event));update(e);const done=()=>{window.removeEventListener('pointermove',update);window.removeEventListener('pointerup',done);window.removeEventListener('pointercancel',done);};window.addEventListener('pointermove',update);window.addEventListener('pointerup',done);window.addEventListener('pointercancel',done);return;
  }
  e.preventDefault();$('#timeline-canvas').focus({preventScroll:true});const id=el.dataset.clip;
  if(e.shiftKey||e.ctrlKey||e.metaKey){chooseClip(id,true);return;}
  if(!state.selection.has(id))chooseClip(id);else{state.selected=id;renderInspector();}
  if(state.tool==='razor'){seekProgram(timelineAt(e));operation('Split',()=>actions.split());return;}
  const before=T.clone(state.sequence),original=before.clips.find(c=>c.id===id),edge=e.target.dataset.trim,startX=e.clientX,startY=e.clientY,scale=pps();
  const scenes=state.job?(state.job.scene_boundaries||[]).flatMap(b=>A.sourcePoint(before,state.job.source,b)):[];
  if(T.locked(before,original.track))return;
  pause();let changedPointer=false,candidate=null,error=null;
  const ids=edge?[id]:selectedIds(),targets=T.linkedClips(before,ids,state.linked).map(c=>c.id);
  const update=event=>{if(Math.hypot(event.clientX-startX,event.clientY-startY)<3&&!changedPointer)return;changedPointer=true;const delta=(event.clientX-startX)/scale;candidate=T.clone(before);const pointerTrack=document.elementFromPoint(event.clientX,event.clientY)?.closest('[data-track]')?.dataset.track||original.track;
    try{const value=edge==='start'?original.start+delta:edge==='end'?T.end(original)+delta:original.start+delta;const snapped=T.snap(candidate,value,scale,targets,state.snap,scenes);if(edge)T.trim(candidate,id,edge,snapped,state.linked);else T.moveSelection(candidate,ids,id,snapped,pointerTrack,state.linked);error=null;for(const c of candidate.clips.filter(c=>targets.includes(c.id))){const node=$(`[data-clip="${c.id}"]`);node.style.left=(c.start*scale)+'px';node.style.width=Math.max(4,T.duration(c)*scale)+'px';$(`[data-track="${c.track}"]`).append(node);}el.classList.remove('invalid');}catch(err){error=err;el.classList.add('invalid');}};
  const done=event=>{window.removeEventListener('pointermove',update);window.removeEventListener('pointerup',done);window.removeEventListener('pointercancel',done);window.removeEventListener('keydown',cancel);if(event?.type==='pointercancel'||event?.key==='Escape'){renderTimeline();return;}if(!changedPointer){chooseClip(id);return;}if(error){renderTimeline();fail(error);return;}state.undo.push(before);state.redo=[];state.sequence=candidate;changed(edge?'Clip trimmed':'Clips moved');};
  const cancel=event=>{if(event.key==='Escape')done(event);};
  window.addEventListener('pointermove',update);window.addEventListener('pointerup',done);window.addEventListener('pointercancel',done);window.addEventListener('keydown',cancel);
}

function startMarquee(e){
  e.preventDefault();pause();state.focus='program';const canvas=$('#timeline-canvas'),scroller=$('#timeline-scroll');canvas.focus({preventScroll:true});
  const point=event=>{const r=canvas.getBoundingClientRect();return {x:Math.max(0,event.clientX-r.left),y:Math.max(0,event.clientY-r.top)};};
  const origin=point(e),before=new Set(state.selection),add=e.shiftKey||e.ctrlKey||e.metaKey;let active=false,box=null,last=e,frame=0;
  const update=event=>{last=event;const p=point(event);if(!active&&Math.hypot(p.x-origin.x,p.y-origin.y)<3)return;active=true;
    if(!box){box=document.createElement('div');box.className='timeline-marquee';box.setAttribute('aria-hidden','true');canvas.append(box);}
    const bounds={left:Math.min(origin.x,p.x),right:Math.max(origin.x,p.x),top:Math.min(origin.y,p.y),bottom:Math.max(origin.y,p.y)},r=canvas.getBoundingClientRect();
    Object.assign(box.style,{left:bounds.left+'px',top:bounds.top+'px',width:(bounds.right-bounds.left)+'px',height:(bounds.bottom-bounds.top)+'px'});
    const ids=add?new Set(before):new Set();for(const node of $$('[data-clip]',canvas)){const b=node.getBoundingClientRect();if(T.intersects(bounds,{left:b.left-r.left,right:b.right-r.left,top:b.top-r.top,bottom:b.bottom-r.top}))ids.add(node.dataset.clip);}
    state.selection=ids;paintSelection();renderInspector();
  };
  const scroll=()=>{if(active){const r=scroller.getBoundingClientRect(),left=scroller.scrollLeft,top=scroller.scrollTop;scroller.scrollLeft+=last.clientX>r.right-24?10:last.clientX<r.left+24?-10:0;scroller.scrollTop+=last.clientY>r.bottom-20?8:last.clientY<r.top+20?-8:0;if(left!==scroller.scrollLeft||top!==scroller.scrollTop)update(last);}frame=requestAnimationFrame(scroll);};
  const done=event=>{cancelAnimationFrame(frame);box?.remove();window.removeEventListener('pointermove',update);window.removeEventListener('pointerup',done);window.removeEventListener('pointercancel',done);window.removeEventListener('keydown',cancel);
    if(event.type==='pointercancel'||event.key==='Escape')state.selection=before;else if(!active&&!add)state.selection.clear();paintSelection();renderInspector();if(!active&&event.type==='pointerup')seekProgram(timelineAt(event));
  };
  const cancel=event=>{if(event.key==='Escape')done(event);};
  window.addEventListener('pointermove',update);window.addEventListener('pointerup',done);window.addEventListener('pointercancel',done);window.addEventListener('keydown',cancel);frame=requestAnimationFrame(scroll);
}

shell();bind();applyThemeNow();syncToggles();
lightQuery.addEventListener('change',()=>{if(state.themeMode==='auto')applyThemeNow();});
operation('Load project',()=>load({initial:true}));pollTasks();setInterval(pollTasks,2000);
