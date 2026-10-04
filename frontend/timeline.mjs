// Pure timeline commands shared by the browser and Node's built-in test runner.
export const clone = value => JSON.parse(JSON.stringify(value));
export const duration = clip => (clip.source_end - clip.source_start) / clip.speed;
export const end = clip => clip.start + duration(clip);
export const sequenceDuration = sequence => Math.max(0, ...sequence.clips.map(end));
// Zoom 1 fits the full sequence; smaller values give room beyond the edit.
export const pixelsPerSecond = (viewportWidth, seconds, zoom=1) =>
  Math.max(1, viewportWidth-64)/Math.max(10, seconds)*zoom;
export const uid = () => crypto.randomUUID().slice(0, 12);
export const locked = (sequence, track) => sequence.tracks.find(t => t.id === track)?.locked;
export const visualTracks = sequence => sequence.tracks.map(t=>t.id).filter(id=>id.startsWith('V')).sort((a,b)=>Number(b.slice(1))-Number(a.slice(1)));
export function visualGeometry(sourceWidth,sourceHeight,canvasWidth,canvasHeight,fit='fit'){
  const ratio=fit==='original'?1:fit==='fill'?Math.max(canvasWidth/sourceWidth,canvasHeight/sourceHeight):fit==='stretch'?0:Math.min(canvasWidth/sourceWidth,canvasHeight/sourceHeight);
  const mediaWidth=fit==='stretch'?canvasWidth:sourceWidth*ratio,mediaHeight=fit==='stretch'?canvasHeight:sourceHeight*ratio;
  return {mediaWidth,mediaHeight,boxWidth:fit==='fit'?mediaWidth:canvasWidth,boxHeight:fit==='fit'?mediaHeight:canvasHeight};
}
export function placementTracks(kind,hasAudio,track){
  if(kind==='audio'&&track[0]!=='A'||kind==='image'&&track[0]!=='V')throw new Error('Drop images on video tracks and audio on audio tracks');
  return [track,...(kind==='video'&&hasAudio&&track==='V1'?['A1']:[])];
}
export const ANIMATED = ['x','y','scale','rotation','opacity'];
export function clipValue(clip, property, time) {
  const frames=clip.keyframes?.[property]||[];
  if(!frames.length)return clip[property];
  if(time<=frames[0].time)return frames[0].value;
  for(let i=1;i<frames.length;i++)if(time<frames[i].time){
    const left=frames[i-1],right=frames[i],p=(time-left.time)/(right.time-left.time);
    const eased=left.interpolation==='hold'?0:left.interpolation==='ease-in'?p*p:
      left.interpolation==='ease-out'?1-(1-p)**2:left.interpolation==='ease-in-out'?p*p*(3-2*p):p;
    return left.value+(right.value-left.value)*eased;
  }
  return frames.at(-1).value;
}
export function setKeyframe(clip,property,time,value,interpolation) {
  if(!ANIMATED.includes(property))throw new Error('This property cannot be animated');
  if(time<0||time>duration(clip)+.001)throw new Error('Keyframe is outside the clip');
  clip.keyframes??={};const frames=clip.keyframes[property]??=[];
  const existing=frames.find(k=>Math.abs(k.time-time)<.0001);
  if(existing)Object.assign(existing,{value,...(interpolation?{interpolation}:{})});else frames.push({time,value,interpolation:interpolation||'linear'});
  frames.sort((a,b)=>a.time-b.time);
  return clip;
}
export function deleteKeyframe(clip,property,time){
  if(clip.keyframes?.[property])clip.keyframes[property]=clip.keyframes[property].filter(k=>Math.abs(k.time-time)>.0001);
  return clip;
}
export function moveKeyframe(clip,property,from,to){
  const frame=clip.keyframes?.[property]?.find(k=>Math.abs(k.time-from)<.0001);
  if(!frame)throw new Error('Keyframe not found');
  if(to<0||to>duration(clip)+.001)throw new Error('Keyframe is outside the clip');
  if(clip.keyframes[property].some(k=>k!==frame&&Math.abs(k.time-to)<.0001))throw new Error('Keyframe already exists there');
  frame.time=to;clip.keyframes[property].sort((a,b)=>a.time-b.time);return clip;
}
function shiftKeyframes(clip,offset){
  for(const frames of Object.values(clip.keyframes||{}))frames.forEach(k=>k.time-=offset);
}
// An edit built from an earlier snapshot (e.g. a drag begun before an autosave finished) must save
// against the newest server revision, or the server rejects it as a change from another window.
export function keepSaveState(next, current) {
  next.revision = current.revision;
  next.source_sha256 = current.source_sha256;
  return next;
}
export function linkedClips(sequence, id, linked = true) {
  const ids = new Set(Array.isArray(id) ? id : [id]);
  const selected = sequence.clips.filter(c => ids.has(c.id));
  if (!selected.length) throw new Error('Select a clip first');
  const links = new Set(linked ? selected.map(c=>c.link_id).filter(Boolean) : []);
  return sequence.clips.filter(c => ids.has(c.id) || links.has(c.link_id));
}
function editable(sequence, clips) {
  if (clips.some(c => locked(sequence, c.track))) throw new Error('Unlock the affected track first');
}
export function validate(sequence) {
  if(!Number.isFinite(sequence.fps??30)||!Number.isFinite(sequence.width??1280)||!Number.isFinite(sequence.height??720))throw new Error('Sequence dimensions and frame rate must be finite');
  const tracks=new Set(sequence.tracks.map(t=>t.id));
  if(sequence.clips.some(c=>!tracks.has(c.track)))throw new Error('Clip track is missing');
  for (const track of sequence.tracks) {
    const clips = sequence.clips.filter(c => c.enabled && c.track === track.id).sort((a,b) => a.start-b.start);
    for (let i=0;i<clips.length;i++) {
      if (duration(clips[i]) < .02 || clips[i].source_start < 0 || clips[i].start < 0) throw new Error('Clip is too short or outside the timeline');
      for(const frames of Object.values(clips[i].keyframes||{}))if(frames.some((k,n)=>!Number.isFinite(k.time)||n>0&&frames[n-1].time>=k.time))throw new Error('Keyframes must be ordered');
      if (i && end(clips[i-1]) > clips[i].start + .001) throw new Error('Clips overlap. Use another track or Overwrite.');
    }
  }
  return sequence;
}
export function split(sequence, id, time, linked = true) {
  const targets = linkedClips(sequence, id, linked).filter(c => c.start + .02 < time && end(c) - .02 > time);
  if (!targets.length) throw new Error('Place the playhead inside a clip to split');
  editable(sequence, targets);
  const rightLink = uid();
  for (const clip of targets) {
    const boundary = clip.source_start + (time-clip.start)*clip.speed;
    const right=clone(clip);shiftKeyframes(right,time-clip.start);
    sequence.clips.push({...right, id:uid(), start:time, source_start:boundary, link_id:linked ? rightLink : null});
    clip.source_end = boundary;
    if (!linked) clip.link_id = null;
  }
  return validate(sequence);
}
export function pruneTransitions(sequence, ids) {
  sequence.transitions = (sequence.transitions || []).filter(t =>
    (!t.outgoing_id || !ids.has(t.outgoing_id)) && (!t.incoming_id || !ids.has(t.incoming_id)));
  return sequence;
}
export function remove(sequence, id, ripple = false, linked = true) {
  const targets = linkedClips(sequence, id, linked);
  editable(sequence, targets);
  if (Array.isArray(id)) {
    if (!ripple) {const ids=new Set(targets.map(c=>c.id));sequence.clips=sequence.clips.filter(c=>!ids.has(c.id));pruneTransitions(sequence,ids);return validate(sequence);}
    const spans=targets.map(c=>[c.start,end(c)]).sort((a,b)=>a[0]-b[0]),merged=[];
    for(const [a,b] of spans){const last=merged.at(-1);if(last&&a<=last[1]+.001)last[1]=Math.max(last[1],b);else merged.push([a,b]);}
    const ids=new Set(targets.map(c=>c.id));sequence.clips=sequence.clips.filter(c=>!ids.has(c.id));pruneTransitions(sequence,ids);
    for(const [a,b] of merged.reverse()){
      if(sequence.clips.some(c=>c.start<b-.001&&end(c)>a+.001))throw new Error('Another clip crosses this ripple range. Split it or use Delete.');
      editable(sequence,sequence.clips.filter(c=>c.start>=b-.001));
      sequence.clips.filter(c=>c.start>=b-.001).forEach(c=>c.start-=b-a);
      sequence.markers.forEach(m=>{m.time=m.time>=b?m.time-(b-a):m.time>a?a:m.time;});
    }
    return validate(sequence);
  }
  const clip = sequence.clips.find(c => c.id === id), start = clip.start, finish = end(clip), delta = duration(clip);
  const ids = new Set(targets.map(c => c.id));
  if (ripple) {
    const survivors = sequence.clips.filter(c => !ids.has(c.id));
    // A crossing clip must be split explicitly; never silently truncate another track.
    if (survivors.some(c => c.start < finish-.001 && end(c) > start+.001)) throw new Error('Another clip crosses this ripple range. Split it or use Delete.');
    editable(sequence, survivors.filter(c => c.start >= finish-.001));
    survivors.filter(c => c.start >= finish-.001).forEach(c => { c.start = Math.max(0,c.start-delta); });
    sequence.markers = sequence.markers.map(m => ({...m,time:m.time >= finish ? m.time-delta : m.time > start ? start : m.time}));
  }
  sequence.clips = sequence.clips.filter(c => !ids.has(c.id));
  pruneTransitions(sequence, ids);
  return validate(sequence);
}
export function addTrack(sequence, kind) {
  const video = kind !== 'audio';
  const prefix = video ? 'V' : 'A';
  const top = Math.max(2, ...sequence.tracks.map(t => t.id).filter(id => id[0] === prefix).map(id => Number(id.slice(1))));
  if (top >= 8) throw new Error('Maximum eight ' + (video ? 'video' : 'audio') + ' tracks');
  sequence.tracks.push({id: prefix + (top + 1), muted: false, locked: false});
  return validate(sequence);
}
export function removeTrack(sequence, id) {
  const track = sequence.tracks.find(t => t.id === id);
  if (!track) throw new Error('Track not found');
  if (track.locked) throw new Error('Unlock the track first');
  if (['V1', 'V2', 'A1', 'A2'].includes(id)) throw new Error(id + ' is required and cannot be deleted');
  const top = Math.max(...sequence.tracks.map(t => t.id).filter(x => x[0] === id[0]).map(x => Number(x.slice(1))));
  if (Number(id.slice(1)) !== top) throw new Error('Delete track ' + id[0] + top + ' first to keep tracks contiguous');
  sequence.tracks = sequence.tracks.filter(t => t.id !== id);
  sequence.clips = sequence.clips.filter(c => c.track !== id);
  return validate(sequence);
}
export function unlink(sequence, id, linked = true) {
  const targets = linkedClips(sequence, id, linked);
  targets.forEach(c => { c.link_id = null; });
  return validate(sequence);
}
export function duplicate(sequence, id, linked = true) {
  const targets = linkedClips(sequence,id,linked);
  editable(sequence,targets);
  const links = new Map(), finish = Math.max(...sequence.clips.map(end));
  const base = Math.min(...targets.map(c => c.start));
  targets.forEach(c => {
    if(linked&&c.link_id&&!links.has(c.link_id))links.set(c.link_id,uid());
    sequence.clips.push({...clone(c),id:uid(),link_id:linked ? links.get(c.link_id)||null : null,start:finish+c.start-base});
  });
  return validate(sequence);
}
export function trim(sequence, id, edge, time, linked = true) {
  const targets = linkedClips(sequence,id,linked);
  editable(sequence,targets);
  for (const clip of targets) {
    if (edge === 'start') {
      const delta = time-clip.start;
      shiftKeyframes(clip,delta);
      clip.source_start += delta*clip.speed;
      clip.start = time;
    } else clip.source_end = clip.source_start + (time-clip.start)*clip.speed;
  }
  return validate(sequence);
}
export function move(sequence, id, time, track, linked = true) {
  const targets = linkedClips(sequence,id,linked), clip = targets.find(c=>c.id===id);
  const delta=time-clip.start;
  editable(sequence,targets);
  if (locked(sequence,track)) throw new Error('Unlock the destination track first');
  if (track[0] !== clip.track[0]) throw new Error('Move video between V tracks and audio between A tracks');
  for (const c of targets) { c.start += delta; if(c.id===id)c.track=track; }
  return validate(sequence);
}
export function moveSelection(sequence, ids, anchorId, time, track, linked=true) {
  const targets=linkedClips(sequence,ids,linked),anchor=targets.find(c=>c.id===anchorId);
  if(!anchor)throw new Error('Select a clip first');
  editable(sequence,targets);
  if(track[0]!==anchor.track[0])throw new Error('Move video between V tracks and audio between A tracks');
  const delta=time-anchor.start,trackDelta=Number(track[1])-Number(anchor.track[1]);
  for(const c of targets){
    // Linked audio keeps its track when moving a video clip between picture tracks.
    const destination=c.track[0]===anchor.track[0]?c.track[0]+(Number(c.track[1])+trackDelta):c.track;
    if(!sequence.tracks.some(t=>t.id===destination)||locked(sequence,destination))throw new Error('Selection does not fit on the destination tracks');
    c.start+=delta;c.track=destination;
  }
  return validate(sequence);
}
export function dropPlacement(sequence,time,duration,pixelsPerSecond,enabled=true,extra=[]) {
  time=Math.max(0,time);if(!enabled)return {start:time,snap:null};
  const points=[0,...extra,...sequence.markers.map(m=>m.time),...sequence.clips.flatMap(c=>[c.start,end(c)])];
  let best={start:time,snap:null},distance=8/pixelsPerSecond;
  for(const edge of [0,duration])for(const point of points){
    const delta=Math.abs(point-time-edge),start=point-edge;
    if(start>=0&&delta<=distance&&(delta<distance||best.snap===null)){best={start,snap:point};distance=delta;}
  }
  return best;
}
export function intersects(a,b){return a.left<=b.right&&a.right>=b.left&&a.top<=b.bottom&&a.bottom>=b.top;}
export function insert(sequence, clips, time, overwrite = false) {
  const span = Math.max(...clips.map(duration));
  const tracks = new Set(clips.map(c=>c.track));
  if ([...tracks].some(t=>locked(sequence,t))) throw new Error('Unlock the destination track first');
  for (const track of sequence.tracks.filter(t=>tracks.has(t.id))) {
    for (const c of [...sequence.clips.filter(c=>c.track===track.id)]) {
      if (c.start < time && end(c) > time) split(sequence,c.id,time,false);
    }
    if (overwrite) {
      for (const c of [...sequence.clips.filter(c=>c.track===track.id)]) {
        if (c.start < time+span && end(c) > time+span) split(sequence,c.id,time+span,false);
      }
      sequence.clips=sequence.clips.filter(c=>c.track!==track.id || c.start < time-.001 || c.start >= time+span-.001);
    } else sequence.clips.filter(c=>c.track===track.id && c.start>=time-.001).forEach(c=>{c.start+=span;});
  }
  clips.forEach(c=>sequence.clips.push({...c,start:time}));
  return validate(sequence);
}
export function snap(sequence, time, pixelsPerSecond, excludeIds=[], enabled=true, extra=[]) {
  if (!enabled) return Math.max(0,time);
  const candidates=[0,...extra,...sequence.markers.map(m=>m.time),...sequence.clips.filter(c=>!excludeIds.includes(c.id)).flatMap(c=>[c.start,end(c)])];
  const near=candidates.reduce((a,b)=>Math.abs(b-time)<Math.abs(a-time)?b:a, candidates[0]);
  return Math.max(0,Math.abs(near-time)*pixelsPerSecond<=8 ? near : time);
}
