"""Extra video/audio tracks: A1-A8 validate like V1-V8; base tracks stay required."""
import pytest
from pydantic import ValidationError

from pipeline.sequence import Sequence, SequenceClip, Track


def _base(**over):
    tracks = [Track(id=t) for t in ("V2", "V1", "A1", "A2")]
    params = dict(source_sha256="a" * 64, width=64, height=64, fps=10,
                  tracks=tracks, clips=[])
    params.update(over)
    return params


def test_audio_track_up_to_a8_validates():
    seq = Sequence(**_base(tracks=[Track(id=t) for t in ("V2", "V1", "A1", "A2", "A3")]))
    assert [t.id for t in seq.tracks if t.id.startswith("A")] == ["A1", "A2", "A3"]
    with pytest.raises(ValidationError):
        Track(id="A9")


def test_audio_clip_allowed_on_extra_audio_track():
    clip = SequenceClip(id="c", source="s.mp4", source_sha256="b" * 64, kind="audio",
                        track="A3", start=0, source_start=0, source_end=1)
    assert clip.track == "A3"
    with pytest.raises(ValidationError, match="audio track"):
        SequenceClip(id="c", source="s.mp4", source_sha256="b" * 64, kind="audio",
                     track="V1", start=0, source_start=0, source_end=1)


def test_extra_tracks_must_stay_contiguous():
    with pytest.raises(ValidationError, match="contiguous"):
        Sequence(**_base(tracks=[Track(id=t) for t in ("V2", "V1", "A1", "A2", "V4")]))
    with pytest.raises(ValidationError, match="contiguous"):
        Sequence(**_base(tracks=[Track(id=t) for t in ("V2", "V1", "A1", "A2", "A4")]))
    ok = Sequence(**_base(tracks=[Track(id=t) for t in ("V2", "V1", "A1", "A2", "V3", "A3")]))
    assert ok.tracks[-1].id == "A3"


def test_base_tracks_still_required():
    with pytest.raises(ValidationError, match="V1, V2, A1, A2"):
        Sequence(**_base(tracks=[Track(id=t) for t in ("V1", "A1", "A2")]))
    with pytest.raises(ValidationError, match="V1, V2, A1, A2"):
        Sequence(**_base(tracks=[Track(id=t) for t in ("V2", "V1", "A2")]))
