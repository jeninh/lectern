"""Per-feature resource limits (lectern.limits, docs/resource-limits.md): .env parsing and
Pi-safe defaults, the scheduler's per-feature and global caps, and an engine child that
goes over its memory cap failing cleanly."""

from __future__ import annotations

import sys
import threading

import pytest
from tests.conftest import api_post

from lectern import limits
from lectern.config import Settings
from lectern.db import session_factory
from lectern.models import Job, JobStatus
from lectern.tts.child import ChildSynth, OverMemoryLimit
from lectern.tts.runner import TTSRunner


def _clean_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("LECTERN_"):
            monkeypatch.delenv(key)


def test_defaults_run_one_heavy_job_at_a_time_with_every_child_capped(monkeypatch):
    _clean_env(monkeypatch)
    lim = limits.Limits.from_settings(Settings())
    assert lim.heavy_jobs == 1
    f = lim.features
    assert {name: f[name].concurrency for name in limits.FEATURES} == dict.fromkeys(
        limits.FEATURES, 1
    )
    # the existing per-job limits keep their defaults
    assert f[limits.OCR].memory_mb == 1536 and f[limits.OCR].threads == 1
    assert f[limits.TRANSCODE].memory_mb == 1024 and f[limits.TRANSCODE].nice == 19
    # the gaps: Kokoro, Piper, and conversions are capped too, on a Pi 4B with 8 GB
    assert f[limits.KOKORO].memory_mb == 3072 and f[limits.KOKORO].threads == 2
    assert f[limits.PIPER].memory_mb == 1024 and f[limits.PIPER].threads == 2
    assert f[limits.CONVERT].memory_mb == 1536
    assert all(f[name].nice >= 10 for name in limits.FEATURES)


def test_env_overrides_keep_old_names_and_empty_means_default(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("LECTERN_OCR_MEMORY_LIMIT_MB", "2048")  # predates this feature
    monkeypatch.setenv("LECTERN_ALIGN_THREADS", "4")
    monkeypatch.setenv("LECTERN_HEAVY_JOBS_MAX", "2")
    monkeypatch.setenv("LECTERN_KOKORO_CONCURRENCY", "0")
    monkeypatch.setenv("LECTERN_PIPER_MEMORY_LIMIT_MB", "0")  # no cap of our own
    monkeypatch.setenv("LECTERN_CONVERT_NICE", "")  # what compose passes for an unset key
    lim = limits.Limits.from_settings(Settings())
    assert lim.heavy_jobs == 2
    assert lim.features[limits.OCR].memory_mb == 2048
    assert lim.features[limits.ALIGN].threads == 4
    assert lim.features[limits.KOKORO].concurrency == 0
    assert lim.features[limits.PIPER].memory_mb is None
    assert lim.features[limits.CONVERT].nice == 10


def test_an_invalid_limit_falls_back_to_its_default_and_names_only_the_key(monkeypatch, caplog):
    _clean_env(monkeypatch)
    bad = {
        "LECTERN_KOKORO_NICE": "abc-secretish",  # not a number
        "LECTERN_PIPER_NICE": "25",  # out of range
        "LECTERN_HEAVY_JOBS_MAX": "0",  # at least one
        "LECTERN_OCR_MEMORY_LIMIT_MB": "12",  # an existing key, below its minimum
        "LECTERN_ALIGN_MEMORY_LIMIT_MB": "-1",  # default: automatic
    }
    for k, v in bad.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LECTERN_PIPER_THREADS", "3")  # a valid neighbour is kept
    with caplog.at_level("WARNING", logger="lectern.config"):
        s = Settings()
    assert (s.kokoro_nice, s.piper_nice, s.heavy_jobs_max) == (10, 10, 1)
    assert s.ocr_memory_limit_mb == 1536 and s.align_memory_limit_mb is None
    assert s.piper_threads == 3
    text = caplog.text
    for key in bad:
        assert f"{key} is invalid, using default" in text
    assert "abc-secretish" not in text  # never the value


def _governor(heavy: int, **concurrency: int) -> limits.Governor:
    base = limits.Limits.defaults()
    return limits.Governor(limits.Limits(heavy, {
        name: limits.Limit(concurrency.get(name, 1), lim.threads, lim.memory_mb, lim.nice)
        for name, lim in base.features.items()
    }))  # fmt: skip


def test_scheduler_respects_the_feature_and_the_global_caps():
    gov = _governor(heavy=2, ocr=1, kokoro=2, convert=0)
    with gov.gate(limits.OCR, limits.KOKORO, limits.CONVERT) as gate:
        assert gate.allowed == {limits.OCR, limits.KOKORO}  # convert: workers only
        ocr = gate.admit(limits.OCR, 1)
    assert gov.blocked(limits.OCR) == limits.FEATURE_LIMIT
    assert gov.blocked(limits.CONVERT) == limits.OFF
    with gov.gate(limits.KOKORO) as gate:
        kokoro = gate.admit(limits.KOKORO, 2)
    # Kokoro has room for a second job, but two heavy jobs run already
    assert gov.blocked(limits.KOKORO) == limits.GLOBAL_LIMIT
    with gov.gate(limits.KOKORO, limits.PIPER) as gate:
        assert gate.allowed == frozenset()
        with pytest.raises(ValueError):
            gate.admit(limits.PIPER)
    snap = gov.snapshot()
    assert snap["heavy_jobs"] == {"limit": 2, "active": 2, "env": "LECTERN_HEAVY_JOBS_MAX"}
    by = {f["feature"]: f for f in snap["features"]}
    assert by["ocr"]["active"] == 1 and by["ocr"]["running"][0]["job_id"] == 1
    assert by["piper"]["blocked"] == limits.GLOBAL_LIMIT
    assert by["kokoro"]["env"]["memory_mb"] == "LECTERN_KOKORO_MEMORY_LIMIT_MB"
    ocr.release()
    ocr.release()  # twice is harmless
    assert gov.blocked(limits.PIPER) is None and gov.blocked(limits.OCR) is None
    kokoro.release()
    assert gov.snapshot()["heavy_jobs"]["active"] == 0


def test_two_runners_cannot_both_take_the_last_slot():
    gov = _governor(heavy=1)
    admitted, go = [], threading.Barrier(8)

    def runner(feature):
        go.wait()
        with gov.gate(feature) as gate:
            if feature in gate.allowed:
                admitted.append(gate.admit(feature))

    threads = [threading.Thread(target=runner, args=(f,))
               for f in [limits.OCR, limits.KOKORO, limits.PIPER, limits.ALIGN] * 2]  # fmt: skip
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(admitted) == 1


# --- runners -----------------------------------------------------------------------------------


def test_a_narration_waits_while_another_heavy_job_holds_the_slot(
    client, owner, settings, tmp_path, monkeypatch
):
    from tests.test_tts import FakeSynth, _work

    gov = _governor(heavy=1)
    monkeypatch.setattr(limits, "GOVERNOR", gov)
    work_id = _work(settings, owner, client, tmp_path)
    r = api_post(client, f"/api/works/{work_id}/audiobook", owner["csrf"],
                 {"engine": "kokoro", "voice": "af_heart"})  # fmt: skip
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    synth = FakeSynth()
    runner = TTSRunner(session_factory(), settings, synth_factory=lambda e, p, s: synth,
                       engines=lambda db: {"kokoro"})  # fmt: skip
    with gov.gate(limits.OCR) as gate:
        ocr = gate.admit(limits.OCR, 99)  # an OCR job is running on the server
    assert runner.run_once() is False
    with session_factory()() as db:
        assert db.get(Job, job_id).status == JobStatus.QUEUED and synth.calls == []
    state = client.get("/api/admin/resource-limits").json()
    kokoro = next(f for f in state["features"] if f["feature"] == "kokoro")
    assert kokoro["queued"] == 1 and kokoro["blocked"] == "global_limit"
    assert kokoro["waiting_for_limit"] is True
    assert state["heavy_jobs"]["active"] == 1
    ocr.release()
    with gov.gate(limits.KOKORO) as gate:
        assert limits.KOKORO in gate.allowed  # now it may start


# --- the engine child --------------------------------------------------------------------------

FAKE_ENGINE = """
import os, sys
from lectern.tts.child import serve

class Engine:
    sample_rate = 16000

    def sentence(self, text):
        if text == "hog":
            hoard = [bytearray(8 * 1024 * 1024) for _ in range(64)]  # 512 MiB, touched
            for b in hoard:
                b[::4096] = b"x" * len(b[::4096])
            import time; time.sleep(30)
        if text == "hang":
            import time; time.sleep(60)
        if text == "oom-killed":  # what the kernel's out-of-memory killer does
            import signal; os.kill(os.getpid(), signal.SIGKILL)
        return int(os.nice(0)).to_bytes(2, "little") * len(text)

sys.exit(serve(lambda spec: Engine()))
"""


def _child(limit: limits.Limit) -> ChildSynth:
    return ChildSynth("piper", {}, limit, command=[sys.executable, "-c", FAKE_ENGINE])


def test_the_engine_child_speaks_at_its_nice_level():
    synth = _child(limits.Limit(1, 1, None, 7))
    try:
        assert synth.sample_rate == 16000
        pcm = synth.sentence("abc")
        assert len(pcm) == 6 and int.from_bytes(pcm[:2], "little") == 7
    finally:
        synth.close()
    assert synth._proc.poll() is not None  # nothing outlives the job


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_an_engine_over_its_memory_cap_fails_the_job_not_the_server():
    synth = _child(limits.Limit(1, 1, 128, 10))
    try:
        with pytest.raises(OverMemoryLimit, match="LECTERN_PIPER_MEMORY_LIMIT_MB"):
            synth.sentence("hog")
        assert synth.peak_mb > 128
    finally:
        synth.close()


def test_a_wedged_engine_is_stopped_and_a_kernel_oom_kill_is_not_retried():
    synth = _child(limits.Limit(1, 1, None, 10))
    try:
        with pytest.raises(RuntimeError, match="stopped answering") as exc:
            synth.sentence("hang", timeout_s=1)
        assert not isinstance(exc.value, OverMemoryLimit)  # retryable: it may just be stuck
    finally:
        synth.close()
    synth = _child(limits.Limit(1, 1, None, 10))
    try:
        with pytest.raises(OverMemoryLimit, match="too much memory"):
            synth.sentence("oom-killed")
    finally:
        synth.close()
