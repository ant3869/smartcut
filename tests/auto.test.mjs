import test from 'node:test';
import assert from 'node:assert/strict';
import * as A from '../frontend/auto.mjs';
import * as T from '../frontend/timeline.mjs';

// Two linked V1/A1 pairs from source.mp4 (source 0-10 at 0s, 20-30 at 10s) plus a music bed on A2.
function sequence() {
  const pair = (link, start, source_start) => ['V1','A1'].map(track => ({id:link+track, track, source:'source.mp4', source_start, source_end:source_start+10, start, speed:1, enabled:true, link_id:link}));
  return {tracks:['V2','V1','A1','A2'].map(id => ({id, locked:false, muted:false})), markers:[{id:'m', time:15, label:'M'}],
    clips:[...pair('a', 0, 0), ...pair('b', 10, 20), {id:'music', track:'A2', source:'music.mp3', source_start:0, source_end:20, start:0, speed:1, enabled:true, link_id:null}]};
}
const spans = (s, track) => s.clips.filter(c => c.track === track).sort((a,b) => a.start-b.start).map(c => [c.start, c.source_start, c.source_end]);

test('range algebra merges, bridges and subtracts', () => {
  assert.deepEqual(A.union([{start:4,end:5},{start:0,end:2},{start:1,end:3}]), [{start:0,end:3},{start:4,end:5}]);
  assert.deepEqual(A.union([{start:0,end:1},{start:1.2,end:2}], .3), [{start:0,end:2}]);
  assert.deepEqual(A.subtract({start:0,end:10}, [{start:2,end:3},{start:8,end:12}]), [{start:0,end:2},{start:3,end:8}]);
  assert.equal(A.coverage({start:0,end:10}, [{start:0,end:4},{start:2,end:6}]), .6);
});

test('source ranges map once per moment through picture clips', () => {
  const s = sequence();
  assert.deepEqual(A.sourceToSequence(s, 'source.mp4', 5, 25).map(r => [r.start, r.end]), [[5,10],[10,15]]);
  assert.deepEqual(A.sourcePoint(s, 'source.mp4', 22), [12]);
  assert.deepEqual(A.sourcePoint(s, 'source.mp4', 10), []);
  const hit = A.sequenceToSource(s, 12.5, 'source.mp4');
  assert.equal(hit.time, 22.5); assert.equal(hit.clip.track, 'V1');
});

test('removing source ranges extracts across every track and keeps links paired', () => {
  const s = sequence();
  const report = A.removeSourceRanges(s, 'source.mp4', [{start:2,end:4}, {start:22,end:23}]);
  assert.deepEqual(report.ranges, [{start:2,end:4},{start:12,end:13}]);
  assert.equal(report.seconds, 3);
  assert.deepEqual(spans(s, 'V1'), [[0,0,2],[2,4,10],[8,20,22],[10,23,30]]);
  assert.deepEqual(spans(s, 'A1'), spans(s, 'V1'));
  assert.deepEqual(spans(s, 'A2'), [[0,0,2],[2,4,12],[10,13,20]]);
  for (const v of s.clips.filter(c => c.track === 'V1')) assert.equal(s.clips.filter(c => c.link_id === v.link_id).length, 2);
  assert.equal(s.markers[0].time, 12);
  assert.equal(T.sequenceDuration(s), 17);
});

test('tiny keeps between nearby cuts are absorbed instead of left as slivers', () => {
  const s = sequence();
  A.removeSourceRanges(s, 'source.mp4', [{start:2,end:4},{start:4.2,end:6}], {minKeep:.3});
  assert.deepEqual(spans(s, 'V1')[0], [0,0,2]);
  assert.deepEqual(spans(s, 'V1')[1], [2,6,10]);
});

test('extract refuses to move clips on locked tracks', () => {
  const s = sequence(); s.tracks.find(t => t.id === 'A2').locked = true;
  assert.throws(() => A.removeSourceRanges(s, 'source.mp4', [{start:2,end:4}]), /Unlock/);
});

test('close gaps pulls everything left without touching media ranges', () => {
  const s = sequence();
  s.clips = s.clips.filter(c => c.track !== 'A2');
  s.clips.filter(c => c.link_id === 'b').forEach(c => { c.start = 14; });
  s.clips.forEach(c => { c.start += 1; });
  assert.deepEqual(A.gaps(s), [{start:0,end:1},{start:11,end:15}]);
  const report = A.closeGaps(s);
  assert.equal(report.seconds, 5);
  assert.deepEqual(spans(s, 'V1'), [[0,0,10],[10,20,30]]);
});

test('silence detection pads speech and auto threshold sits between floor and speech', () => {
  const rate = 10, peaks = [...Array(10).fill(.5), ...Array(10).fill(.001), ...Array(10).fill(.5), ...Array(8).fill(.002)];
  assert.deepEqual(A.silentRanges(peaks, rate, {thresholdDb:-40, minSilence:.6, pad:.1}), [{start:1.1,end:1.9},{start:3.1,end:3.8}]);
  assert.deepEqual(A.silentRanges(peaks, rate, {thresholdDb:-40, minSilence:1.5}), []);
  const threshold = A.suggestThreshold(peaks);
  assert.ok(threshold > A.toDb(.002) && threshold < A.toDb(.5), String(threshold));
  assert.equal(A.suggestThreshold([]), -40);
  assert.equal(A.analyzeLevels(peaks).reliable, true);
  const noisy = A.analyzeLevels([...Array(50).fill(.08), ...Array(50).fill(.3)]);
  assert.equal(noisy.reliable, false); assert.ok(noisy.range < 18);
});

test('scene and highlight markers land only inside kept material', () => {
  const s = sequence();
  const scenes = A.sceneMarkers(s, 'source.mp4', [5, 15, 25, 10]);
  assert.deepEqual(scenes.map(m => m.time), [5]);
  assert.ok(!scenes.some(m => m.time === 15), 'existing marker at 15 is not duplicated');
  const obs = [{timestamp:3, score:9, keep:true, description:'Big laugh. Then more'}, {timestamp:4, score:8, keep:true}, {timestamp:15, score:10, keep:true}, {timestamp:27, score:8, keep:true}, {timestamp:28, score:9, keep:false}];
  const marks = A.highlightMarkers(s, 'source.mp4', obs, {count:5, minScore:8, spacing:3});
  assert.deepEqual(marks.map(m => m.time), [3, 17]);
  assert.equal(marks[0].label, '★ 9/10 Big laugh');
});

test('proposal status follows human coverage and fill scale covers the frame', () => {
  const review = {cut_intervals:[{start:0,end:3}], keep_intervals:[{start:10,end:20}]};
  assert.equal(A.proposalStatus({start:1,end:4}, review), 'accepted');
  assert.equal(A.proposalStatus({start:12,end:13}, review), 'rejected');
  assert.equal(A.proposalStatus({start:5,end:6}, review), 'pending');
  assert.equal(A.fillScale(1920, 1080, 1080, 1920), 3.16);
  assert.equal(A.fillScale(1080, 1920, 1080, 1920), 1);
});

test('proposal evidence separates strong cuts from self-contradictory ones', () => {
  const obs = [{timestamp:7, score:1}, {timestamp:8, score:2}, {timestamp:223, score:8}, {timestamp:100, score:5}];
  assert.deepEqual(A.proposalEvidence({start:7,end:9}, obs), {frames:2, meanScore:1.5, strength:'strong'});
  assert.equal(A.proposalEvidence({start:223,end:225}, obs).strength, 'weak');
  assert.equal(A.proposalEvidence({start:99,end:101}, obs).strength, 'likely');
  assert.deepEqual(A.proposalEvidence({start:50,end:52}, obs), {frames:0, meanScore:null, strength:'likely'});
});

test('captions are re-timed through the edit and serialize as SRT', () => {
  const s = sequence();
  const captions = A.editCaptions(s, 'source.mp4', [{start:1,end:3,text:' Hello '}, {start:9.9,end:21,text:'split'}, {start:12,end:14,text:'gone'}, {start:5,end:5.1,text:'blip'}]);
  assert.deepEqual(captions, [{start:1,end:3,text:'Hello'}, {start:9.9,end:10,text:'split'}, {start:10,end:11,text:'split'}].filter(c => c.end-c.start >= .2));
  assert.equal(A.toSRT([{start:61.5,end:3723.042,text:'Hi'}]), '1\n00:01:01,500 --> 01:02:03,042\nHi\n');
});

test('removed map reports what the edit took out, in the original timeline', () => {
  const before = sequence(), after = T.clone(before);
  A.removeSourceRanges(after, 'source.mp4', [{start:2,end:4}, {start:22,end:23}]);
  assert.deepEqual(A.removedMap(before, after), [{start:2,end:4},{start:12,end:13}]);
  assert.deepEqual(A.removedMap(before, T.clone(before)), []);
});

test('each frame verdict covers the time around its sample, matching how cuts are proposed', () => {
  const obs = [{timestamp:0}, {timestamp:2}, {timestamp:4}, {timestamp:6}];
  assert.deepEqual(A.sampleSpans(obs, 7).map(r => [r.start, r.end]), [[0,1],[1,3],[3,5],[5,7]]);
  // A frame at 70s rejected by Eye becomes the 69-71s cut; its heat span must be the same.
  assert.deepEqual(A.sampleSpans([{timestamp:68}, {timestamp:70}, {timestamp:72}], 227.5)[1], {start:69, end:71, observation:{timestamp:70}});
  assert.deepEqual(A.sampleSpans([{timestamp:4}], 10).map(r => [r.start, r.end]), [[3,5]]);
  assert.deepEqual(A.sampleSpans([], 10), []);
});

test('the verdict at a moment is the nearest sampled frame, not the last one before it', () => {
  const obs = [{timestamp:68, keep:true}, {timestamp:70, keep:false}, {timestamp:72, keep:true}];
  assert.equal(A.sampleAt(obs, 69.2).timestamp, 70);
  assert.equal(A.sampleAt(obs, 70.9).timestamp, 70);
  assert.equal(A.sampleAt(obs, 71.1).timestamp, 72);
  assert.equal(A.sampleAt(obs, 0).timestamp, 68);
  assert.equal(A.sampleAt([], 5), null);
});
