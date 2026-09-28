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
  for (const track of sequence.tracks) {
    const clips = sequence.clips.filter(c => c.enabled && c.track === track.id).sort((a,b) => a.start-b.start);
    for (let i=0;i<clips.length;i++) {
      if (duration(clips[i]) < .02 || clips[i].source_start < 0 || clips[i].start < 0) throw new Error('Clip is too short or outside the timeline');
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
    sequence.clips.push({...clip, id:uid(), start:time, source_start:boundary, link_id:linked ? rightLink : null});
    clip.source_end = boundary;
    if (!linked) clip.link_id = null;
  }
  return validate(sequence);
}
export function remove(sequence, id, ripple = false, linked = true) {
  const targets = linkedClips(sequence, id, linked);
  editable(sequence, targets);
  if (Array.isArray(id)) {
    if (!ripple) {const ids=new Set(targets.map(c=>c.id));sequence.clips=sequence.clips.filter(c=>!ids.has(c.id));return validate(sequence);}
    const spans=targets.map(c=>[c.start,end(c)]).sort((a,b)=>a[0]-b[0]),merged=[];
    for(const [a,b] of spans){const last=merged.at(-1);if(last&&a<=last[1]+.001)last[1]=Math.max(last[1],b);else merged.push([a,b]);}
    const ids=new Set(targets.map(c=>c.id));sequence.clips=sequence.clips.filter(c=>!ids.has(c.id));
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
  return validate(sequence);
}
export function duplicate(sequence, id, linked = true) {
  const targets = linkedClips(sequence,id,linked);
  editable(sequence,targets);
  const links = new Map(), finish = Math.max(...sequence.clips.map(end));
  const base = Math.min(...targets.map(c => c.start));
  targets.forEach(c => {
    if(linked&&c.link_id&&!links.has(c.link_id))links.set(c.link_id,uid());
    sequence.clips.push({...c,id:uid(),link_id:linked ? links.get(c.link_id)||null : null,start:finish+c.start-base});
  });
  return validate(sequence);
}
export function trim(sequence, id, edge, time, linked = true) {
  const targets = linkedClips(sequence,id,linked);
  editable(sequence,targets);
  for (const clip of targets) {
    if (edge === 'start') {
      const delta = time-clip.start;
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
