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
