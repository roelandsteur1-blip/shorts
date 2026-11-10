from meta import runner, transcribe


def test_wer_and_words():
    assert transcribe.wer(["a", "b", "c", "d"], ["a", "x", "c"]) == 0.5
    assert transcribe.wer([], ["a"]) is None
    srt = "1\n00:00:01,000 --> 00:00:02,000\nHello <i>there</i>, Houston!\n\n2\n00:00:03,000 --> 00:00:04,000\nWe're go.\n"
    assert transcribe.srt_words(srt) == ["hello", "there", "houston", "we're", "go"]


def test_runner_allows_transcribe():
    cmd = runner.command({"id": "2026-11-10-transcribe-1", "name": "transcribe"})
    assert cmd[-2:] == ["-m", "meta.transcribe"]
    assert "transcribe" in runner.JSON_JOBS
