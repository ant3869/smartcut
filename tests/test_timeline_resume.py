"""Partial-resume integrity: exact filenames, content-bound verdicts, no stale reuse."""
import json

import pytest

from pipeline.brain import PipelineBrain
from pipeline.util import PipelineError
from test_timeline_engine import (
    _brain,
    _clip,
    _interruptible_wire,
    _video,
)


def test_interruption_writes_exact_partial_names(tmp_path):
    import numpy as np
    src = _video(tmp_path / "media" / "s.mp4", 20)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _interruptible_wire(brain, {str(src): [frame]}, fail_on_call=2)
    project = tmp_path / "vault" / "projects" / "r1"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 20.0, str(src))]}
    with pytest.raises(PipelineError, match="simulated interruption"):
        brain.analyze_timeline(project, seq, {"mode": "entire"})
    partial = project / "timeline_analysis.partial.json"
    assert partial.is_file()
    # The resume record is one file: observations bound to identity in a
    # single atomic write. No legacy sidecar, no double suffix.
    assert not (project / "timeline_analysis.partial.key.json").exists()
    assert not (project / "timeline_analysis.partial.partial.json").exists()
    assert not (project / "timeline_analysis.json").exists()
    record = json.loads(partial.read_text())
    assert record["timeline_identity"]["sequence_revision"] == 1
    assert len(record["observations"]) == 4  # first batch kept, bound to identity


def test_resume_reuses_matching_and_rejects_reordered_footage(tmp_path):
    import numpy as np
    a = _video(tmp_path / "media" / "a.mp4", 12, color=(0, 0, 240))
    b = _video(tmp_path / "media" / "b.mp4", 12, color=(0, 240, 0))
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    wired = _interruptible_wire(brain, {str(a): [frame], str(b): [frame]},
                                fail_on_call=99)
    project = tmp_path / "vault" / "projects" / "r2"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 6.0, str(a)),
                                    _clip("b", 6.0, 0.0, 6.0, str(b))]}
    (project / "sequence.json").write_text(json.dumps(seq))
    # Interrupt after the first batch: fail on call 2, then rerun clean.
    wired_fail = _interruptible_wire(brain, {str(a): [frame], str(b): [frame]},
                                     fail_on_call=2)
    with pytest.raises(PipelineError, match="simulated interruption"):
        brain.analyze_timeline(project, seq, {"mode": "entire"})
    judged_partial = json.loads(
        (project / "timeline_analysis.partial.json").read_text())
    assert len(judged_partial["observations"]) == 4  # first batch kept
    # Same timeline instants, swapped footage: nothing may be reused.
    swapped = {"revision": 1, "clips": [_clip("b2", 0.0, 0.0, 6.0, str(b)),
                                        _clip("a2", 6.0, 0.0, 6.0, str(a))]}
    (project / "sequence.json").write_text(json.dumps(swapped))
    clean = _interruptible_wire(brain, {str(a): [frame], str(b): [frame]},
                                fail_on_call=99)
    result = brain.analyze_timeline(project, swapped, {"mode": "entire"})
    assert clean["calls"] == 2  # both batches re-judged, none resumed
    by_tl = {o["timestamp"]: o for o in result["observations"]}
    assert by_tl[0.0]["description"].startswith("ok-")
    prov = {p["timeline"]: p["source"] for p in result["provenance"]}
    assert prov[0.0] == str(b) and prov[6.0] == str(a)


def test_changed_trim_rejudges_only_changed_instants(tmp_path):
    import numpy as np
    src = _video(tmp_path / "media" / "s.mp4", 30)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _interruptible_wire(brain, {str(src): [frame]}, fail_on_call=2)
    project = tmp_path / "vault" / "projects" / "r3"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 20.0, str(src))]}
    (project / "sequence.json").write_text(json.dumps(seq))
    with pytest.raises(PipelineError, match="simulated interruption"):
        brain.analyze_timeline(project, seq, {"mode": "entire"})
    # Trim the tail: judged instants 0-6 keep identical footage, the rest is fresh.
    trimmed = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 16.0, str(src))]}
    (project / "sequence.json").write_text(json.dumps(trimmed))
    clean = _interruptible_wire(brain, {str(src): [frame]}, fail_on_call=99)
    result = brain.analyze_timeline(project, trimmed, {"mode": "entire"})
    asked = clean["asked"]
    assert all(t not in asked for t in (0.0, 2.0, 4.0, 6.0))  # resumed, not re-judged
    assert 8.0 in asked and 16.0 in asked
    assert len(result["observations"]) == 9  # 0..16 step 2
    assert {o["timestamp"] for o in result["observations"]} == {
        round(t, 3) for t in (0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0)}


def test_revision_bump_forces_fresh_run(tmp_path):
    import numpy as np
    src = _video(tmp_path / "media" / "s.mp4", 12)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _interruptible_wire(brain, {str(src): [frame]}, fail_on_call=2)
    project = tmp_path / "vault" / "projects" / "r4"
    project.mkdir(parents=True)
    seq = {"revision": 5, "clips": [_clip("a", 0.0, 0.0, 12.0, str(src))]}
    (project / "sequence.json").write_text(json.dumps(seq))
    with pytest.raises(PipelineError, match="simulated interruption"):
        brain.analyze_timeline(project, seq, {"mode": "entire"})
    seq6 = {"revision": 6, "clips": [_clip("a", 0.0, 0.0, 12.0, str(src))]}
    (project / "sequence.json").write_text(json.dumps(seq6))
    clean = _interruptible_wire(brain, {str(src): [frame]}, fail_on_call=99)
    result = brain.analyze_timeline(project, seq6, {"mode": "entire"})
    assert clean["calls"] == 2  # sidecar revision mismatch: full fresh run
    assert result["sequence_revision"] == 6


def test_crash_before_first_flush_cannot_trust_rewritten_keys(tmp_path):
    """A crash after content changed but before any new verdict flushed must
    never leave the new identity keys trusting the old partial.

    Resume files sync partial-first, keys-second precisely so this torn
    state cannot arise from a genuine crash; a run that wrote fresh keys
    without flushing fresh verdicts would let swapped footage inherit stale
    judgments at identical timeline instants.
    """
    import numpy as np
    a = _video(tmp_path / "media" / "a.mp4", 12, color=(0, 0, 240))
    b = _video(tmp_path / "media" / "b.mp4", 12, color=(0, 240, 0))
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    project = tmp_path / "vault" / "projects" / "r5"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 6.0, str(a)),
                                    _clip("b", 6.0, 0.0, 6.0, str(b))]}
    (project / "sequence.json").write_text(json.dumps(seq))
    _interruptible_wire(brain, {str(a): [frame], str(b): [frame]},
                        fail_on_call=2)
    with pytest.raises(PipelineError, match="simulated interruption"):
        brain.analyze_timeline(project, seq, {"mode": "entire"})
    # Same timeline instants, swapped footage — then crash before the first
    # new verdict flushes (fail on the very first ask).
    swapped = {"revision": 1, "clips": [_clip("b2", 0.0, 0.0, 6.0, str(b)),
                                        _clip("a2", 6.0, 0.0, 6.0, str(a))]}
    (project / "sequence.json").write_text(json.dumps(swapped))
    _interruptible_wire(brain, {str(a): [frame], str(b): [frame]},
                        fail_on_call=1)
    with pytest.raises(PipelineError, match="simulated interruption"):
        brain.analyze_timeline(project, swapped, {"mode": "entire"})
    # Full fresh judging: nothing from the old footage may be reused.
    clean = _interruptible_wire(brain, {str(a): [frame], str(b): [frame]},
                                fail_on_call=99)
    result = brain.analyze_timeline(project, swapped, {"mode": "entire"})
    assert clean["calls"] == 2  # both batches re-judged, none resumed
    assert 0.0 in clean["asked"] and 6.0 in clean["asked"]
    assert len(result["observations"]) == 8
    prov = {p["timeline"]: p["source"] for p in result["provenance"]
            if p["timeline"] in (0.0, 6.0)}
    assert prov[0.0] == str(b)


def test_corrupt_partial_is_treated_as_miss_not_crash(tmp_path):
    """A torn/truncated partial write must degrade to a full re-judge,
    never crash the resume nor trust garbage."""
    import numpy as np
    src = _video(tmp_path / "media" / "s.mp4", 12)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    project = tmp_path / "vault" / "projects" / "r6"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 12.0, str(src))]}
    (project / "sequence.json").write_text(json.dumps(seq))
    _interruptible_wire(brain, {str(src): [frame]}, fail_on_call=2)
    with pytest.raises(PipelineError, match="simulated interruption"):
        brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert (project / "timeline_analysis.partial.json").is_file()
    (project / "timeline_analysis.partial.json").write_text("{truncated")
    clean = _interruptible_wire(brain, {str(src): [frame]}, fail_on_call=99)
    result = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert result["judged_count"] == 7  # 0..12 step 2: full re-judge
    assert len(clean["asked"]) == 7
    assert (project / "timeline_analysis.json").is_file()
    assert not (project / "timeline_analysis.partial.json").exists()


def test_identity_stripped_partial_is_never_trusted(tmp_path):
    """Simulates the torn write between verdict flush and identity sync: a
    partial record carrying observations but no timeline_identity must
    degrade to a full re-judge, never a resume."""
    import numpy as np
    src = _video(tmp_path / "media" / "s.mp4", 12)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    project = tmp_path / "vault" / "projects" / "r7"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 12.0, str(src))]}
    (project / "sequence.json").write_text(json.dumps(seq))
    _interruptible_wire(brain, {str(src): [frame]}, fail_on_call=2)
    with pytest.raises(PipelineError, match="simulated interruption"):
        brain.analyze_timeline(project, seq, {"mode": "entire"})
    partial = project / "timeline_analysis.partial.json"
    record = json.loads(partial.read_text())
    assert len(record["observations"]) == 4
    del record["timeline_identity"]  # torn write: verdicts without identity
    partial.write_text(json.dumps(record))
    clean = _interruptible_wire(brain, {str(src): [frame]}, fail_on_call=99)
    result = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert sum(clean["asked"].count(t) for t in (0.0, 2.0, 4.0, 6.0)) == 4
    assert result["judged_count"] == 7


def test_return_to_previous_content_rejudges_safely(tmp_path):
    """A -> B -> A: the B run overwrites the single resume record, so coming
    back to A finds no usable identity and re-judges everything. Slower,
    but no B verdict can ever be assigned to A footage."""
    import numpy as np
    a = _video(tmp_path / "media" / "a.mp4", 12, color=(0, 0, 240))
    b = _video(tmp_path / "media" / "b.mp4", 12, color=(0, 240, 0))
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    project = tmp_path / "vault" / "projects" / "r8"
    project.mkdir(parents=True)
    seq_a = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 12.0, str(a))]}
    seq_b = {"revision": 1, "clips": [_clip("b", 0.0, 0.0, 12.0, str(b))]}
    (project / "sequence.json").write_text(json.dumps(seq_a))
    _interruptible_wire(brain, {str(a): [frame], str(b): [frame]}, fail_on_call=2)
    with pytest.raises(PipelineError, match="simulated interruption"):
        brain.analyze_timeline(project, seq_a, {"mode": "entire"})
    (project / "sequence.json").write_text(json.dumps(seq_b))
    _interruptible_wire(brain, {str(a): [frame], str(b): [frame]}, fail_on_call=2)
    with pytest.raises(PipelineError, match="simulated interruption"):
        brain.analyze_timeline(project, seq_b, {"mode": "entire"})
    # Back to A: the record now describes B, so nothing is reused.
    (project / "sequence.json").write_text(json.dumps(seq_a))
    clean = _interruptible_wire(brain, {str(a): [frame], str(b): [frame]},
                                fail_on_call=99)
    result = brain.analyze_timeline(project, seq_a, {"mode": "entire"})
    assert clean["calls"] == 2  # all 7 A instants re-judged, none reused
    assert len(result["observations"]) == 7
    prov = {p["timeline"]: p["source"] for p in result["provenance"]}
    assert prov[0.0] == str(a)
