import test from 'node:test';
import assert from 'node:assert/strict';
import * as T from '../frontend/timeline.mjs';
function sequence(){return {tracks:['V2','V1','A1','A2'].map(id=>({id,locked:false,muted:false})),markers:[{id:'m',time:8}],clips:['V1','A1'].map((track,i)=>({id:'c'+i,track,source:'fixture.mp4',source_start:2,source_end:12,start:0,speed:1,enabled:true,link_id:'pair'}))};}
test('linked split retains media time and both tracks',()=>{const s=sequence();T.split(s,'c0',4);assert.equal(s.clips.length,4);assert.equal(s.clips[0].source_end,6);assert.equal(s.clips[2].source_start,6);assert.equal(s.clips[2].link_id,s.clips[3].link_id);});
test('ripple shifts downstream clips and markers equally',()=>{const s=sequence();T.split(s,'c0',4);T.remove(s,'c0',true);assert.equal(s.clips.length,2);assert.equal(s.clips[0].start,0);assert.equal(s.markers[0].time,4);assert.equal(T.sequenceDuration(s),6);});
test('locked linked audio prevents destructive edit',()=>{const s=sequence();s.tracks.find(t=>t.id==='A1').locked=true;assert.throws(()=>T.split(s,'c0',4),/Unlock/);assert.equal(s.clips.length,2);});
test('trim respects speed and snap supports markers and edges',()=>{const s=sequence();T.trim(s,'c0','start',2);assert.equal(s.clips[0].source_start,4);assert.equal(s.clips[1].start,2);assert.equal(T.snap(s,7.95,50),8);assert.equal(T.snap(s,7.95,50,[],false),7.95);});
test('overwrite splits existing source ranges without changing duration',()=>{const s=sequence();const insert={...s.clips[0],id:'insert',link_id:null,source_start:0,source_end:2};T.insert(s,[insert],4,true);const clips=s.clips.filter(c=>c.track==='V1').sort((a,b)=>a.start-b.start);assert.deepEqual(clips.map(c=>[c.start,c.source_start,c.source_end]),[[0,2,6],[4,0,2],[6,8,12]]);});
test('move to another video track keeps linked audio timing',()=>{const s=sequence();T.move(s,'c0',3,'V2');assert.equal(s.clips[0].track,'V2');assert.equal(s.clips[1].start,3);assert.throws(()=>T.move(s,'c0',3,'A2'),/video/);});
test('snap also honours extra points such as scene changes',()=>{const s=sequence();assert.equal(T.snap(s,5.03,100,[],true,[5]),5);assert.equal(T.snap(s,5.3,100,[],true,[5]),5.3);});
test('drop preview snaps either boundary within eight screen pixels at each zoom',()=>{
  const s=sequence();
  for(const scale of [2,20,80,300]){
    const p=T.dropPlacement(s,10+4/scale,12.4,scale,true);
    assert.equal(p.start,10);assert.equal(p.snap,10);
    const clips=[{...s.clips[0],id:'d',track:'V2',source_start:0,source_end:12.4}];
    T.insert(s,clips,p.start);assert.equal(s.clips.at(-1).start,p.start);assert.equal(T.duration(s.clips.at(-1))*scale,12.4*scale);s.clips.pop();
  }
  assert.deepEqual(T.dropPlacement(s,5.03,2,100,true,[7]),{start:5,snap:7});
  assert.deepEqual(T.dropPlacement(s,5.03,2,100,false,[7]),{start:5.03,snap:null});
  assert.deepEqual(T.dropPlacement(s,0,2,100,true),{start:0,snap:0});
});
test('zoom fits a long sequence and can pull back far beyond fit',()=>{
  const viewport=800, seconds=1800;
  const fit=T.pixelsPerSecond(viewport,seconds);
  assert.ok(seconds*fit+60<=viewport);
  assert.ok(seconds*T.pixelsPerSecond(viewport,seconds,1/32)<25);
  assert.equal(T.pixelsPerSecond(viewport,seconds,64),fit*64);
});
test('marquee intersection includes partial hits on any track and excludes separated boxes',()=>{
  const box={left:10,right:20,top:10,bottom:80};
  assert.ok(T.intersects(box,{left:0,right:11,top:30,bottom:50}));
  assert.ok(T.intersects(box,{left:19,right:30,top:70,bottom:90}));
  assert.ok(!T.intersects(box,{left:21,right:30,top:30,bottom:50}));
});
test('group moves and deletion preserve links and track locks',()=>{
  const s=sequence();s.clips.push({...s.clips[0],id:'overlay',track:'V2',link_id:null,start:1});
  T.moveSelection(s,['c0','overlay'],'c0',3,'V1');assert.deepEqual(s.clips.map(c=>c.start),[3,3,4]);
  assert.throws(()=>T.moveSelection(T.clone(s),['c0','overlay'],'c0',3,'V2'),/destination/);
  const locked=T.clone(s);locked.tracks.find(t=>t.id==='A1').locked=true;assert.throws(()=>T.remove(locked,['c0','overlay']),/Unlock/);
  T.remove(s,['c0','overlay']);assert.equal(s.clips.length,0);
});
test('group ripple removes the union of selected spans once across tracks',()=>{
  const s=sequence();T.split(s,'c0',4);const right=s.clips.filter(c=>c.start===4).map(c=>c.id);
  T.remove(s,right,true);assert.equal(s.clips.length,2);assert.equal(T.sequenceDuration(s),4);assert.equal(s.markers[0].time,4);
});
test('duplicating a group preserves independent link pairs',()=>{
  const s=sequence();s.clips.push({...s.clips[0],id:'overlay',track:'V2',link_id:null});
  T.duplicate(s,['c0','overlay']);const copies=s.clips.slice(3);
  assert.equal(copies.length,3);assert.equal(copies[0].link_id,copies[1].link_id);
  assert.notEqual(copies[0].link_id,'pair');assert.equal(copies[2].link_id,null);
});
