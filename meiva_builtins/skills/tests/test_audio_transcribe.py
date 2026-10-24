# meiva:note
# Hermetic tests for the audio-transcribe skill (no network, no real ffmpeg).
#
# The real OpenAI + ffmpeg path was verified live by hand during the build (single-chunk and
# forced multi-chunk segmentation). The decisions are tested as values (source resolution, the
# chunk-or-not choice and segment length, the part-size gate, the answer shape), and the shell
# runs for real: the programs it launches are small stand-in scripts written under tmp_path that
# behave like ffprobe/ffmpeg/yt-dlp, and the OpenAI client talks to a real HTTP server on a
# loopback port.


from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from openai import OpenAI

from meiva_builtins.skills.tests.local_post_server import (
    RecordedPost,
    ServedPosts,
    serve_posts,
    text_reply,
)
from meiva_builtins.skills.tests.skill_script import load_skill_script

plan = load_skill_script(
    "integrations", "audio-transcribe", "scripts", "transcribe_plan.py", name="transcribe_plan"
)
T = load_skill_script("integrations", "audio-transcribe", "scripts", "transcribe.py")


_STAND_IN = """
import json
import math
import sys
from pathlib import Path

invoked = Path(sys.argv[0])
config = json.loads((invoked.parent / "stand-in.json").read_text())
args = sys.argv[1:]
if invoked.name == "ffprobe":
    print(config["duration"])
elif invoked.name == "ffmpeg":
    if config["ffmpeg_fails"]:
        sys.stderr.write("Invalid data found when processing input")
        sys.exit(1)
    if "segment" in args:
        seconds = int(args[args.index("-segment_time") + 1])
        for i in range(math.ceil(float(config["duration"]) / seconds)):
            Path(args[-1] % i).write_bytes(b"p" * config["part_size"])
    else:
        Path(args[-1]).write_bytes(b"c" * config["compact_size"])
elif invoked.name == "yt-dlp":
    if config["ytdlp_writes"]:
        Path(args[args.index("-o") + 1].replace("%(ext)s", "mp3")).write_bytes(b"x")
"""


@pytest.fixture(scope="module")
def stand_in_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    script = tmp_path_factory.mktemp("stand-in") / "stand_in.py"
    script.write_text(f"#!{sys.executable}\n{_STAND_IN}")
    script.chmod(0o755)
    return script


# meiva:note
# Stand-ins for the three programs, made once per module and linked under the names the
# skill runs (a fresh executable costs a first-run scan on macOS). Each test's behavior is
# the JSON next to the links: ffprobe answers `duration`; ffmpeg writes a `compact_size`-byte
# compact mp3, or cuts it into `part_size`-byte parts of the requested segment length (and
# exits 1 with a message when `ffmpeg_fails`); yt-dlp writes the mp3 it was asked for unless
# `ytdlp_writes` is false.
class StandIns:
    def __init__(self, script: Path, bin_dir: Path) -> None:
        self._bin_dir = bin_dir
        bin_dir.mkdir()
        for name in ("ffmpeg", "ffprobe", "yt-dlp"):
            (bin_dir / name).symlink_to(script)

    def tools(
        self,
        *,
        compact_size: int = 1000,
        duration: str = "190.0",
        part_size: int = 1,
        ffmpeg_fails: bool = False,
        ytdlp_writes: bool = True,
    ) -> Any:
        config = {
            "compact_size": compact_size,
            "duration": duration,
            "part_size": part_size,
            "ffmpeg_fails": ffmpeg_fails,
            "ytdlp_writes": ytdlp_writes,
        }
        (self._bin_dir / "stand-in.json").write_text(json.dumps(config))
        return plan.Tools(
            ffmpeg=str(self._bin_dir / "ffmpeg"),
            ffprobe=str(self._bin_dir / "ffprobe"),
            yt_dlp=str(self._bin_dir / "yt-dlp"),
        )


@pytest.fixture
def stand_ins(tmp_path: Path, stand_in_script: Path) -> StandIns:
    return StandIns(stand_in_script, tmp_path / "bin")


def _media(tmp_path: Path) -> Path:
    media = tmp_path / "talk.mp3"
    media.write_bytes(b"audio")
    return media


def _client(server: ServedPosts) -> OpenAI:
    return OpenAI(api_key="test-key", base_url=f"{server.url}/v1", max_retries=0)


def _fields(post: RecordedPost) -> str:
    return post.body.decode("latin-1")


def test_resolve_local_file_passthrough(tmp_path: Path, stand_ins: StandIns) -> None:
    f = _media(tmp_path)
    assert T.resolve_to_audio(str(f), tmp_path, stand_ins.tools()) == f


def test_resolve_bad_source_raises(tmp_path: Path, stand_ins: StandIns) -> None:
    with pytest.raises(FileNotFoundError):
        T.resolve_to_audio("not-a-file-or-url", tmp_path, stand_ins.tools())


def test_resolve_video_id_downloads(tmp_path: Path, stand_ins: StandIns) -> None:
    got = T.resolve_to_audio("dRsjO-88nBs", tmp_path, stand_ins.tools())
    assert got.exists() and got.name == "src.mp3"


def test_resolve_url_downloads(tmp_path: Path, stand_ins: StandIns) -> None:
    got = T.resolve_to_audio("https://example.com/podcast.mp3", tmp_path, stand_ins.tools())
    assert got.exists() and got.name == "src.mp3"


def test_resolve_refuses_a_download_that_produced_no_mp3(
    tmp_path: Path, stand_ins: StandIns
) -> None:
    with pytest.raises(RuntimeError, match="yt-dlp produced no mp3"):
        T.resolve_to_audio("dRsjO-88nBs", tmp_path, stand_ins.tools(ytdlp_writes=False))


def test_plan_source_decides_by_existence_then_shape(tmp_path: Path) -> None:
    here = tmp_path / "a.mp3"
    assert plan.plan_source("a.mp3", here, True) == plan.LocalFile(here)
    assert plan.plan_source("dRsjO-88nBs", here, False) == plan.Download(
        "https://www.youtube.com/watch?v=dRsjO-88nBs"
    )
    assert plan.plan_source("http://h/x.mp3", here, False) == plan.Download("http://h/x.mp3")
    with pytest.raises(FileNotFoundError, match="neither a local file"):
        plan.plan_source("nope", here, False)


def test_an_existing_path_wins_over_the_video_id_shape(tmp_path: Path) -> None:
    eleven = tmp_path / "abcdefghijk"
    assert plan.plan_source("abcdefghijk", eleven, True) == plan.LocalFile(eleven)


def test_commands_name_the_programs_and_their_arguments() -> None:
    tools = plan.Tools(ffmpeg="FF", ffprobe="FP", yt_dlp="YD")
    assert tools.download("u", "o") == [
        "YD",
        "--no-config",
        "-x",
        "--audio-format",
        "mp3",
        "-o",
        "o",
        "u",
    ]
    assert tools.probe("m")[0] == "FP" and tools.probe("m")[-1] == "m"
    assert tools.transcode("in", "out") == [
        "FF",
        "-nostdin",
        "-y",
        "-i",
        "in",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "32k",
        "out",
    ]
    assert tools.segment("c", 68, "p_%03d.mp3") == [
        "FF",
        "-nostdin",
        "-y",
        "-i",
        "c",
        "-f",
        "segment",
        "-segment_time",
        "68",
        "-c",
        "copy",
        "p_%03d.mp3",
    ]


def test_duration_is_the_trimmed_probe_output() -> None:
    assert plan.parse_duration("12.5\n") == 12.5
    with pytest.raises(ValueError, match="could not convert"):
        plan.parse_duration("N/A")


def test_a_failing_program_surfaces_its_stderr(tmp_path: Path, stand_ins: StandIns) -> None:
    tools = stand_ins.tools(ffmpeg_fails=True)
    with pytest.raises(RuntimeError, match="exited with status 1: Invalid data found"):
        T.run_command(tools.transcode("in.mp3", str(tmp_path / "out.mp3")))


def test_segment_seconds_is_none_when_one_request_fits() -> None:
    limits = plan.Limits(max_bytes=10_000, max_segment_s=900)
    assert plan.segment_seconds(1000, 120.0, limits) is None
    assert plan.segment_seconds(10_000, 900.0, limits) is None


def test_segment_seconds_follows_the_byte_share_then_the_duration_ceiling() -> None:
    limits = plan.Limits(max_bytes=20_000, max_segment_s=900)
    assert plan.segment_seconds(50_000, 190.0, limits) == 68
    assert plan.segment_seconds(50_000_000, 190.0, limits) == 60
    assert plan.segment_seconds(1000, 3600.0, limits) == 900
    assert plan.segment_seconds(0, 3600.0, limits) == 900


def test_prepare_single_chunk_when_small(tmp_path: Path, stand_ins: StandIns) -> None:
    tools = stand_ins.tools(compact_size=1000, duration="120.0")
    limits = plan.Limits(max_bytes=10_000, max_segment_s=900)
    chunks = T.prepare_chunks(_media(tmp_path), tmp_path, tools, limits)
    assert [c.name for c in chunks] == ["compact.mp3"]


def test_prepare_segments_when_large(tmp_path: Path, stand_ins: StandIns) -> None:
    tools = stand_ins.tools(compact_size=50_000, duration="190.0")
    limits = plan.Limits(max_bytes=20_000, max_segment_s=900)
    chunks = T.prepare_chunks(_media(tmp_path), tmp_path, tools, limits)
    assert [c.name for c in chunks] == ["part_000.mp3", "part_001.mp3", "part_002.mp3"]


def test_prepare_segments_on_duration_even_when_small(tmp_path: Path, stand_ins: StandIns) -> None:
    # meiva:note
    # Under the byte cap but over the duration ceiling -> still segments (a long podcast that
    # compresses small must not be sent as one over-long request).
    tools = stand_ins.tools(compact_size=1000, duration="3600.0")
    limits = plan.Limits(max_bytes=10_000, max_segment_s=900)
    chunks = T.prepare_chunks(_media(tmp_path), tmp_path, tools, limits)
    assert len(chunks) == 4


def test_prepare_raises_on_oversize_part(tmp_path: Path, stand_ins: StandIns) -> None:
    tools = stand_ins.tools(compact_size=1000, duration="190.0", part_size=200)
    limits = plan.Limits(max_bytes=100, max_segment_s=900, api_cap_bytes=150)
    with pytest.raises(RuntimeError, match="25 MB API cap"):
        T.prepare_chunks(_media(tmp_path), tmp_path, tools, limits)


def test_prepare_raises_when_segmentation_produces_no_parts(
    tmp_path: Path, stand_ins: StandIns
) -> None:
    tools = stand_ins.tools(compact_size=50_000, duration="0.0")
    limits = plan.Limits(max_bytes=20_000, max_segment_s=900)
    with pytest.raises(RuntimeError, match="produced no parts"):
        T.prepare_chunks(_media(tmp_path), tmp_path, tools, limits)


def test_check_parts_names_the_oversize_segments() -> None:
    limits = plan.Limits(max_bytes=10, api_cap_bytes=100)
    plan.check_parts([("a", 100), ("b", 50)], limits)
    with pytest.raises(RuntimeError, match=r"\['b'\]"):
        plan.check_parts([("a", 100), ("b", 101)], limits)


def test_limits_reject_oversize_max_bytes() -> None:
    with pytest.raises(ValueError, match="25 MB"):
        plan.Limits(max_bytes=99 * 1024 * 1024)
    assert plan.Limits(max_bytes=plan.OPENAI_MAX_BYTES).max_bytes == plan.OPENAI_MAX_BYTES


def test_duration_ceiling_refuses_longer_audio_and_none_lifts_it() -> None:
    plan.check_duration(3600.0, plan.Limits(max_duration_s=3600))
    with pytest.raises(ValueError, match="max_duration_s"):
        plan.check_duration(3600.1, plan.Limits(max_duration_s=3600))
    plan.check_duration(10**9, plan.Limits(max_duration_s=None))


def test_transcript_json_joins_non_empty_texts_and_counts_every_chunk() -> None:
    transcript = plan.Transcript(texts=("one", "", "three"), model="m", seconds=12.34, source="s")
    assert transcript.as_json() == {
        "text": "one\nthree",
        "model": "m",
        "chunks": 3,
        "audio_seconds": 12.3,
        "source": "s",
    }


def test_transcribe_joins_chunk_texts(tmp_path: Path, stand_ins: StandIns) -> None:
    tools = stand_ins.tools(compact_size=50_000, duration="190.0")
    limits = plan.Limits(max_bytes=20_000, max_segment_s=900)
    with serve_posts([text_reply("text-0"), text_reply("text-1"), text_reply("text-2")]) as server:
        out = T.transcribe(
            str(_media(tmp_path)), client=_client(server), tools=tools, limits=limits
        )
    assert out == {
        "text": "text-0\ntext-1\ntext-2",
        "model": "gpt-4o-transcribe",
        "chunks": 3,
        "audio_seconds": 190.0,
        "source": str(tmp_path / "talk.mp3"),
    }
    assert len(server.requests) == 3
    assert {post.path for post in server.requests} == {"/v1/audio/transcriptions"}


def test_transcribe_sends_the_model_text_format_and_optional_hints(
    tmp_path: Path, stand_ins: StandIns
) -> None:
    with serve_posts([text_reply("hello"), text_reply("hello again")]) as server:
        T.transcribe(
            str(_media(tmp_path)),
            client=_client(server),
            tools=stand_ins.tools(),
            model="whisper-1",
            language="en",
            prompt="Claude, Meiva",
        )
        T.transcribe(str(_media(tmp_path)), client=_client(server), tools=stand_ins.tools())
    hinted, plain = (_fields(post) for post in server.requests)
    assert 'name="model"' in hinted and "whisper-1" in hinted
    assert 'name="response_format"' in hinted and "text" in hinted
    assert 'name="language"' in hinted and 'name="prompt"' in hinted and "Claude, Meiva" in hinted
    assert 'filename="compact.mp3"' in hinted
    assert 'name="language"' not in plain and 'name="prompt"' not in plain
    assert "gpt-4o-transcribe" in plain


def test_transcribe_downloads_a_video_id_then_transcribes(
    tmp_path: Path, stand_ins: StandIns
) -> None:
    with serve_posts([text_reply("spoken words")]) as server:
        out = T.transcribe("dRsjO-88nBs", client=_client(server), tools=stand_ins.tools())
    assert out["text"] == "spoken words" and out["source"] == "dRsjO-88nBs"


def test_transcribe_rejects_over_duration_before_any_api_call(
    tmp_path: Path, stand_ins: StandIns
) -> None:
    tools = stand_ins.tools(duration="10000.0")
    with serve_posts([]) as server, pytest.raises(ValueError, match="max_duration_s"):
        T.transcribe(
            str(_media(tmp_path)),
            client=_client(server),
            tools=tools,
            limits=plan.Limits(max_duration_s=3600),
        )
    assert server.requests == []


def test_main_prints_the_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], stand_ins: StandIns
) -> None:
    with serve_posts([text_reply("the talk")]) as server:
        access = T.OpenAIAccess(api_key="test-key", base_url=f"{server.url}/v1")
        T.main([str(_media(tmp_path))], access=access, tools=stand_ins.tools())
    assert capsys.readouterr().out == "the talk\n"
    assert "Bearer test-key" in server.requests[0].headers["authorization"]


def test_main_json_prints_the_full_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], stand_ins: StandIns
) -> None:
    media = _media(tmp_path)
    with serve_posts([text_reply("spoken")]) as server:
        access = T.OpenAIAccess(api_key="test-key", base_url=f"{server.url}/v1")
        argv = [str(media), "--json", "--model", "whisper-1", "--language", "de"]
        T.main(argv, access=access, tools=stand_ins.tools())
    result = json.loads(capsys.readouterr().out)
    assert result["model"] == "whisper-1" and result["chunks"] == 1
    assert result["source"] == str(media) and result["audio_seconds"] == 190.0
    assert 'name="language"' in _fields(server.requests[0])


def test_main_without_a_key_refuses_before_running_any_program(
    tmp_path: Path, stand_ins: StandIns
) -> None:
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY not set"):
        T.main([str(_media(tmp_path))], access=None, tools=stand_ins.tools(ffmpeg_fails=True))


def test_main_refuses_an_oversize_max_bytes(tmp_path: Path, stand_ins: StandIns) -> None:
    access = T.OpenAIAccess(api_key="test-key", base_url=None)
    with pytest.raises(ValueError, match="25 MB"):
        T.main(
            [str(_media(tmp_path)), "--max-bytes", str(99 * 1024 * 1024)],
            access=access,
            tools=stand_ins.tools(),
        )
