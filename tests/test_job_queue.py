"""The job queue page (issue #87, lectern.jobqueue): grouping and batches, pause / resume /
reorder / cancel / retry across kinds and every claim path, worker leases, and who may see
and do what."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from tests.conftest import api_post, login, make_user

from lectern import limits, queue
from lectern.align import jobs as align_jobs
from lectern.db import get_engine, session_factory
from lectern.jobs import claim as local_claim
from lectern.models import Job, JobStatus, Work, WorkerCredential, utcnow
from lectern.tts import jobs as tts_jobs


def add_work(title: str) -> int:
    with session_factory()() as db:
        w = Work(title=title, sort_title=title.lower(), author="A", sort_author="a",
                 dir_path=f"A/{title}")  # fmt: skip
        db.add(w)
        db.commit()
        return w.id


def add_job(kind: str, work_id: int | None = None, payload: dict | None = None, **kw) -> int:
    with session_factory()() as db:
        kw.setdefault("status", JobStatus.QUEUED)
        job = Job(kind=kind, work_id=work_id, payload=payload or {}, **kw)
        db.add(job)
        db.commit()
        return job.id


def job(job_id: int) -> Job:
    with session_factory()() as db:
        return db.get(Job, job_id)


def overview(client) -> dict:
    r = client.get("/api/job-queue")
    assert r.status_code == 200, r.text
    return r.json()


def by_id(data: dict) -> dict[int, dict]:
    rows = [*data["jobs"], *(j for b in data["batches"] for j in b["jobs"])]
    return {j["id"]: j for j in rows}


def act(client, owner, job_id: int, action: str, **body):
    return api_post(client, f"/api/job-queue/jobs/{job_id}/{action}", owner["csrf"], body)


def claim_ocr(db, **kw):
    return align_jobs.claim(db, kinds=["ocr"], worker="local", lease_s=600, credential_id=None,
                            **kw)  # fmt: skip


def test_jobs_are_grouped_batches_are_one_row_and_a_held_job_says_limit_reached(client, owner):
    w = [add_work(f"Book {i}") for i in range(5)]
    running = add_job("transcode", w[0], {"tracks": []})
    with session_factory()() as db:
        assert local_claim(db)[0].id == running  # the local runner holds it
    waiting = add_job("pdf2epub", w[1], {"mode": "convert"})
    paused = add_job("ocr", w[2])
    failed = add_job("ocr", w[3], status=JobStatus.FAILED, error="tesseract crashed")
    batch = "0123456789abcdef"
    b1 = add_job("pdf2epub", w[3], {"batch": batch}, status=JobStatus.SUCCEEDED)
    b2 = add_job("pdf2epub", w[4], {"batch": batch})
    assert act(client, owner, paused, "pause").status_code == 200

    data = overview(client)
    rows = by_id(data)
    assert data["can_manage"] is True and data["queue_paused"] is False
    assert {i: rows[i]["group"] for i in (running, waiting, paused, failed, b1, b2)} == {
        running: "running", waiting: "waiting", paused: "paused", failed: "failed",
        b1: "done", b2: "waiting",
    }  # fmt: skip
    assert rows[running]["runs_on"] == "this server" and rows[running]["title"] == "Book 0"
    assert rows[paused]["waiting"] == "Paused"
    assert rows[failed]["error"] == "tesseract crashed"
    # the batch is one row with per-book status, not loose jobs
    assert {j["id"] for j in data["jobs"]}.isdisjoint({b1, b2})
    (row,) = data["batches"]
    assert (row["id"], row["total"], row["group"]) == (batch, 2, "waiting")
    assert row["counts"] == {"running": 0, "waiting": 1, "paused": 0, "failed": 0, "done": 1}
    assert [(j["title"], j["status"]) for j in row["jobs"]] == [
        ("Book 3", "succeeded"), ("Book 4", "queued"),
    ]  # fmt: skip

    # A conversion holding the only convert slot: the waiting ones say "limit reached".
    with limits.GOVERNOR.gate(limits.CONVERT) as gate:
        slot = gate.admit(limits.CONVERT, 0)
    with slot:
        rows = by_id(overview(client))
        assert rows[waiting]["limit"] == "feature_limit"
        assert rows[waiting]["waiting"] == "Waiting: limit reached"
        assert rows[paused]["limit"] is None  # held by the owner, not a limit
    assert by_id(overview(client))[waiting]["limit"] is None


def test_pause_and_resume_a_job_or_the_whole_queue_stop_every_claim_path(client, owner):
    w = [add_work(f"Book {i}") for i in range(3)]
    convert = add_job("pdf2epub", w[0])
    ocr = add_job("ocr", w[1])
    tts = add_job("tts", w[2], {"engine": "kokoro"})
    for j in (convert, ocr, tts):
        assert act(client, owner, j, "pause").status_code == 200
    with session_factory()() as db:
        assert local_claim(db, kinds=("pdf2epub",)) is None
        assert claim_ocr(db) is None
        assert tts_jobs.claim_local(db, {"kokoro"}) is None
        # still each work's one live job of its kind: a paused job is not a free slot
        assert queue.is_paused(db.get(Job, convert))
    # elsewhere (the book's status, GET /api/jobs) a paused job says so: no year-9000 wait
    shown = client.get(f"/api/jobs/{tts}").json()
    assert (shown["paused"], shown["run_after"]) == (True, None)
    assert shown["waiting"]["summary"] == "Paused by the owner"
    for j in (convert, ocr, tts):
        assert act(client, owner, j, "resume").status_code == 200

    # The whole queue: nothing new starts anywhere, including on an external worker.
    r = api_post(client, "/api/job-queue/pause", owner["csrf"], {"paused": True})
    assert r.json() == {"queue_paused": True}
    key = api_post(client, "/api/worker-credentials", owner["csrf"], {"label": "mac"}).json()
    wc = TestClient(client.app, headers={"Authorization": f"Bearer {key['secret']}"})
    assert wc.post("/api/worker/jobs/claim", json={"capabilities": ["ocr"]}).status_code == 204
    with session_factory()() as db:
        assert local_claim(db, kinds=("pdf2epub",)) is None
        assert claim_ocr(db) is None
        assert tts_jobs.claim_local(db, {"kokoro"}) is None
    assert {r["waiting"] for r in overview(client)["jobs"]} == {"Queue paused"}
    api_post(client, "/api/job-queue/pause", owner["csrf"], {"paused": False})
    with session_factory()() as db:
        assert local_claim(db, kinds=("pdf2epub",))[0].id == convert
        assert tts_jobs.claim_local(db, {"kokoro"})[0].id == tts
    assert act(client, owner, convert, "pause").json() == {"detail": "not_waiting"}  # running
    r = wc.post("/api/worker/jobs/claim", json={"capabilities": ["ocr"]})
    assert r.status_code == 200 and r.json()["job"]["id"] == ocr


def test_moving_an_ocr_job_to_the_top_makes_it_run_next_where_it_can(client, owner):
    w = [add_work(f"Book {i}") for i in range(3)]
    first = add_job("ocr", w[0])
    later = add_job("ocr", w[1])
    convert = add_job("pdf2epub", w[2])  # another capability: its own runner, its own line
    assert act(client, owner, later, "move", to="top").json()["priority"] < job(first).priority
    with session_factory()() as db:
        assert claim_ocr(db)[0].id == later
    assert act(client, owner, convert, "move", to="bottom").status_code == 200
    assert job(convert).priority > job(first).priority
    with session_factory()() as db:
        assert local_claim(db, kinds=("pdf2epub",))[0].id == convert  # still runs: own runner
    assert act(client, owner, later, "move", to="top").status_code == 409  # running now
    assert act(client, owner, first, "move", to="sideways").status_code == 422


def add_workers() -> None:
    """A ready worker, one busy with a job, one offline, one with an allowlist that leaves out
    ocr; all of them offer ocr and pdf2epub's capability."""
    now = utcnow()
    with session_factory()() as db:
        caps = ["ocr", "pdf2epub"]
        ready = WorkerCredential(
            label="ready", key_hash="1" * 64, advertised=caps, last_used_at=now
        )
        busy = WorkerCredential(label="busy", key_hash="2" * 64, advertised=caps, last_used_at=now)
        off = WorkerCredential(label="off", key_hash="3" * 64, advertised=caps,
                               last_used_at=now - timedelta(days=1))  # fmt: skip
        limited = WorkerCredential(label="limited", key_hash="4" * 64, advertised=caps,
                                   capabilities=["align"], last_used_at=now)  # fmt: skip
        db.add_all([ready, busy, off, limited])
        db.flush()
        db.add(Job(kind="transcode", payload={}, status=JobStatus.LEASED, leased_by_id=busy.id,
                   lease_token_hash="h" * 64, lease_expires_at=now + timedelta(hours=1),
                   heartbeat_at=now))  # fmt: skip
        db.commit()


def wait_statements(client, per_capability: int) -> tuple[int, int]:
    """SQL statements and non-revoked credential loads of one GET /api/job-queue."""
    import sqlalchemy as sa

    with session_factory()() as db:
        db.query(Job).filter(Job.status == JobStatus.QUEUED).delete()
        db.commit()
    for i in range(per_capability):
        add_job("ocr", None, priority=i)
        add_job("pdf2epub", None, priority=i)
    seen: list[str] = []

    def hook(conn, cursor, statement, *rest):
        seen.append(statement)

    engine = get_engine()
    sa.event.listen(engine, "before_cursor_execute", hook)
    try:
        overview(client)
    finally:
        sa.event.remove(engine, "before_cursor_execute", hook)
    return len(seen), sum("revoked_at IS NULL" in s for s in seen)


def test_the_jobs_page_costs_the_same_however_many_jobs_wait(client, owner):
    add_workers()
    small = wait_statements(client, 5)
    large = wait_statements(client, 25)
    assert small == large
    assert large[1] == 1  # the credentials are loaded once for the whole page


def test_a_waiting_job_reads_the_same_on_the_page_as_when_explained_alone(client, owner):
    member = make_user(client, owner["csrf"], "mia", "member")
    add_workers()
    work = iter([add_work(f"Shared {i}") for i in range(7)])
    tie = utcnow() - timedelta(minutes=10)
    ids = [
        add_job("ocr", next(work), priority=5, created_by=member["id"], created_at=tie),
        add_job("ocr", next(work), priority=5, created_by=member["id"], created_at=tie),  # a tie
        add_job("ocr", next(work), priority=5, created_by=member["id"]),
        add_job("ocr", next(work), priority=9, created_by=member["id"]),
        # Paused and ahead of everything: explain_wait counts it, the page's own place does not.
        add_job("ocr", next(work), priority=1, created_by=member["id"], run_after=queue.PAUSED_UNTIL),
        add_job("pdf2epub", next(work), priority=2, created_by=member["id"], attempts=1,
                error="boom", run_after=utcnow() + timedelta(minutes=5)),
        add_job("pdf2epub", next(work), priority=3, created_by=member["id"]),
    ]  # fmt: skip
    checked = 0
    for for_owner in (True, False):
        if not for_owner:
            client.cookies.clear()
            login(client, "mia", member["password"])
        rows = by_id(overview(client))
        with session_factory()() as db:
            for jid in ids:
                row = rows[jid]
                if row["group"] != "waiting":
                    continue
                want = queue.explain_wait(db, db.get(Job, jid), for_owner=for_owner)
                assert row["waiting"] == want["summary"], (jid, for_owner)
                checked += 1
    assert checked == 12  # six waiting jobs, seen by both
    with session_factory()() as db:
        third = queue.explain_wait(db, db.get(Job, ids[2]), for_owner=True)
    assert "(3 ahead)" in third["summary"]  # the tie pair and the paused job are all ahead


@pytest.mark.parametrize(("kind", "payload", "keeps"), [
    ("tts", {"engine": "kokoro"}, True),  # narration resumes from its chapters on Retry
    ("transcode", {"tracks": []}, False),
])  # fmt: skip
def test_cancelling_a_job_running_on_a_worker_stops_it_and_frees_the_worker(
    client, owner, settings, kind, payload, keeps
):
    key = api_post(client, "/api/worker-credentials", owner["csrf"], {"label": "mac"}).json()
    wc = TestClient(client.app, headers={"Authorization": f"Bearer {key['secret']}"})
    w = [add_work("Book 0"), add_work("Book 1")]
    running = add_job(kind, w[0], payload)
    nxt = add_job("ocr", w[1])
    with session_factory()() as db:
        cred = db.get(WorkerCredential, key["id"])
        claimed, token = align_jobs.claim(
            db, kinds=[kind], capabilities=[queue.capability_for(kind, payload)],
            worker="mac", lease_s=600, credential_id=cred.id, credential=cred,
        )  # fmt: skip
    assert claimed.id == running
    lease = {"X-Lease-Token": token}
    artifacts = queue.artifacts_dir(settings.data_dir, running)
    artifacts.mkdir(parents=True)
    (artifacts / "part-1").write_bytes(b"partial")
    beat = f"/api/worker/jobs/{running}/heartbeat"
    assert wc.post(beat, headers=lease, json={"progress": 0.4}).status_code == 200
    assert by_id(overview(client))[running]["runs_on"] == "mac"

    r = act(client, owner, running, "cancel")
    assert r.json() == {"id": running, "status": "cancelled"}
    # the worker's next heartbeat loses the lease (it stops the engine), and nothing it
    # uploads afterwards is accepted
    assert wc.post(beat, headers=lease, json={"progress": 0.5}).status_code == 409
    assert artifacts.is_dir() is keeps
    with session_factory()() as db:
        wait = queue.explain_wait(db, db.get(Job, nxt))
    assert not any("busy" in (w["why"] or "") for w in wait["workers"])
    assert act(client, owner, running, "cancel").json()["status"] == "cancelled"  # idempotent


def test_retry_queues_a_failed_job_again_in_place(client, owner):
    w = add_work("Book 0")
    failed = add_job("ocr", w, {"language": "eng"}, status=JobStatus.FAILED, attempts=3,
                     error="boom", finished_at=utcnow())  # fmt: skip
    assert act(client, owner, failed, "retry").json()["group"] == "waiting"
    j = job(failed)
    assert (j.status, j.attempts, j.error, j.finished_at) == (JobStatus.QUEUED, 0, None, None)
    assert j.payload == {"language": "eng"}
    assert act(client, owner, failed, "retry").status_code == 409  # not failed now
    # another live job of the work and kind: no second one
    with session_factory()() as db:
        db.get(Job, failed).status = JobStatus.FAILED
        db.commit()
    add_job("ocr", w)
    assert act(client, owner, failed, "retry").json() == {"detail": "already_running"}
    oom = add_job("align", add_work("Book 1"), status=JobStatus.FAILED,
                  error="worker ran out of memory")  # fmt: skip
    assert act(client, owner, oom, "retry").json() == {"detail": "retry_from_book"}


def test_members_and_readers_see_and_cancel_only_their_own_jobs(client, owner, settings):
    member = make_user(client, owner["csrf"], "mia", "member")
    reader = make_user(client, owner["csrf"], "rex", "reader")
    shown, hidden = add_work("Shared"), add_work("Owner's private book")
    theirs = add_job("pdf2epub", shown, created_by=member["id"])
    batch = "fedcba9876543210"
    in_batch = add_job("ocr", shown, {"batch": batch}, created_by=member["id"])
    others = add_job("ocr", hidden)
    add_job("storage-cleanup", None, created_by=member["id"])  # owner-only kind
    reader_job = add_job("ocr", hidden, created_by=reader["id"], status=JobStatus.FAILED,
                         error="/srv/library/secret path")  # fmt: skip
    # Waiting out a backoff: the owner's "waiting" quotes the error ("… after: <error>").
    leak = "/srv/library/Owner/Owner's private book/book.pdf: boom"
    backoff = dict(attempts=1, error=leak, run_after=utcnow() + timedelta(minutes=5))
    reader_wait = add_job("transcode", hidden, created_by=reader["id"], **backoff)
    gone = add_work("Trashed later")
    member_wait = add_job("transcode", gone, created_by=member["id"], **backoff)
    with session_factory()() as db:
        db.get(Work, gone).trashed_at = utcnow()
        db.commit()
    assert leak in by_id(overview(client))[reader_wait]["waiting"]  # the owner sees it
    m = TestClient(client.app)
    m_csrf = login(m, "mia", member["password"])
    data = overview(m)
    assert data["can_manage"] is False
    assert set(by_id(data)) == {theirs, in_batch, member_wait}
    # a member's own job on a book they cannot see (now in the trash): no error text either
    row = by_id(data)[member_wait]
    assert (row["title"], row["error"]) == (None, None)
    assert row["waiting"].startswith("Waiting") and "boom" not in row["waiting"]
    assert api_post(m, f"/api/job-queue/jobs/{others}/cancel", m_csrf).status_code == 404
    for action in ("pause", "retry"):
        assert api_post(m, f"/api/job-queue/jobs/{theirs}/{action}", m_csrf).status_code == 403
    assert api_post(m, "/api/job-queue/pause", m_csrf, {"paused": True}).status_code == 403
    assert api_post(m, f"/api/job-queue/batches/{batch}/pause", m_csrf).status_code == 403
    assert api_post(m, f"/api/job-queue/jobs/{theirs}/cancel", m_csrf).json()["status"] == (
        "cancelled"
    )
    assert api_post(m, f"/api/job-queue/batches/{batch}/cancel", m_csrf).json() == {"cancelled": 1}
    assert job(others).status == JobStatus.QUEUED

    r = TestClient(client.app)
    login(r, "rex", reader["password"])
    rows = by_id(overview(r))
    assert set(rows) == {reader_job, reader_wait}
    # their own jobs, on a book they cannot see: no title, and no error text for readers,
    # not even inside the "why it waits" summary
    assert (rows[reader_job]["title"], rows[reader_job]["error"]) == (None, None)
    assert rows[reader_job]["work_id"] == hidden  # the id alone says nothing
    wait = rows[reader_wait]
    assert (wait["title"], wait["error"]) == (None, None)
    assert wait["waiting"].startswith("Waiting") and "private book" not in wait["waiting"]


def test_pause_move_and_retry_lose_cleanly_to_a_claim_or_finish_that_got_there_first(client, owner):
    from lectern import jobqueue

    w = [add_work(f"Book {i}") for i in range(2)]
    queued = add_job("pdf2epub", w[0])
    failed = add_job("ocr", w[1], status=JobStatus.FAILED, error="boom")
    with session_factory()() as db:
        stale_q, stale_f = db.get(Job, queued), db.get(Job, failed)  # loaded before the race
        with session_factory()() as other:
            assert local_claim(other, kinds=("pdf2epub",))[0].id == queued
            other.get(Job, failed).status = JobStatus.CANCELLED  # changed meanwhile
            other.commit()
        for fn in (jobqueue.pause, lambda d, j: jobqueue.move(d, j, "top")):
            with pytest.raises(jobqueue.Refused) as exc:
                fn(db, stale_q)
            assert exc.value.code == "not_waiting"
        with pytest.raises(jobqueue.Refused) as exc:
            jobqueue.retry(db, stale_f)
        assert exc.value.code == "not_failed"
        db.rollback()
    j = job(queued)
    assert (j.status, j.run_after < queue.PAUSED_UNTIL) == (JobStatus.LEASED, True)


def test_batch_pause_resume_and_cancel_act_on_the_rest_of_the_batch(client, owner):
    batch = "00112233aabbccdd"
    done = add_job("pdf2epub", add_work("A"), {"batch": batch}, status=JobStatus.SUCCEEDED)
    rest = [add_job("pdf2epub", add_work(t), {"batch": batch}) for t in ("B", "C")]
    url = f"/api/job-queue/batches/{batch}"
    assert api_post(client, f"{url}/pause", owner["csrf"]).json() == {"paused": 2}
    with session_factory()() as db:
        assert local_claim(db, kinds=("pdf2epub",)) is None
    assert overview(client)["batches"][0]["group"] == "paused"
    assert api_post(client, f"{url}/resume", owner["csrf"]).json() == {"resumed": 2}
    assert api_post(client, f"{url}/cancel", owner["csrf"]).json() == {"cancelled": 2}
    assert [job(i).status for i in (done, *rest)] == [JobStatus.SUCCEEDED] + [
        JobStatus.CANCELLED
    ] * 2
    assert api_post(client, "/api/job-queue/batches/ffffffffffffffff/cancel",
                    owner["csrf"]).status_code == 404  # fmt: skip


@pytest.mark.parametrize("kind", ["align", "tts", "ocr"])
def test_a_cancel_loses_cleanly_to_a_finish_that_got_there_first(client, owner, kind):
    """Issue #109 (b): a cancel that read the job as leased must not overwrite a success
    committed meanwhile (its output is published), nor fail the alignment it produced."""
    from lectern.models import Alignment, AlignmentStatus
    from lectern.ocr import jobs as ocr_jobs

    cancel = {"align": align_jobs.cancel_job, "tts": tts_jobs.cancel, "ocr": ocr_jobs.cancel}[kind]
    w = add_work("Raced")
    leased = add_job(kind, w, status=JobStatus.LEASED, lease_token_hash="0" * 64)
    with session_factory()() as db:
        db.add(Alignment(work_id=w, model="m", status=AlignmentStatus.RUNNING,
                         epub_sha256="e", audio_fingerprint="a"))  # fmt: skip
        db.commit()
    with session_factory()() as db:
        stale = db.get(Job, leased)
        alignment = align_jobs.work_alignment(db, w)  # both loaded before the race
        assert (stale.status, alignment.status) == (JobStatus.LEASED, AlignmentStatus.RUNNING)
        with session_factory()() as other:
            other.get(Job, leased).status = JobStatus.SUCCEEDED
            align_jobs.work_alignment(other, w).status = AlignmentStatus.SUCCEEDED
            other.commit()
        assert cancel(db, stale) is False
        db.commit()
    assert job(leased).status == JobStatus.SUCCEEDED
    with session_factory()() as db:
        assert align_jobs.work_alignment(db, w).status == AlignmentStatus.SUCCEEDED

    # still live: cancelled, and an alignment job fails its alignment
    w2 = add_work("Live")
    live = add_job(kind, w2, status=JobStatus.LEASED, lease_token_hash="0" * 64)
    with session_factory()() as db:
        db.add(Alignment(work_id=w2, model="m", status=AlignmentStatus.RUNNING,
                         epub_sha256="e", audio_fingerprint="a"))  # fmt: skip
        db.commit()
        assert cancel(db, db.get(Job, live)) is True
        db.commit()
        after = align_jobs.work_alignment(db, w2).status
    assert job(live).status == JobStatus.CANCELLED
    assert after == (AlignmentStatus.FAILED if kind == "align" else AlignmentStatus.RUNNING)


def test_the_book_pages_cancel_and_job_json_follow_the_jobs_page_rules(client, owner):
    """Issue #109 (a) and (d): POST /api/jobs/{id}/cancel lets the owner or whoever started a
    job cancel it, and the legacy job JSON shows error text only where the Jobs page does."""
    member = make_user(client, owner["csrf"], "mia", "member")
    shown, gone = add_work("Shared"), add_work("Trashed later")
    leak = "/srv/library/A/Trashed later/book.m4b: boom"
    backoff = dict(attempts=1, error=leak, run_after=utcnow() + timedelta(minutes=5))
    mine = add_job("tts", shown, {"engine": "kokoro"}, created_by=member["id"], **backoff)
    auto = add_job("align", shown, {"model": "small"}, **backoff)  # auto-queued: no starter
    hidden = add_job("transcode", gone, created_by=member["id"], **backoff)
    with session_factory()() as db:
        db.get(Work, gone).trashed_at = utcnow()
        db.commit()
    m = TestClient(client.app)
    m_csrf = login(m, "mia", member["password"])

    rows = {j["id"]: j for j in m.get("/api/jobs").json()["jobs"]}
    assert {i: rows[i]["can_cancel"] for i in (mine, auto, hidden)} == {
        mine: True, auto: False, hidden: True,
    }  # fmt: skip
    # a book the member can see: the error as before; one they cannot: none, anywhere
    assert rows[auto]["error"] == leak and leak in rows[auto]["waiting"]["summary"]
    h = rows[hidden]
    assert (h["error"], h["waiting"]["last_error"]) == (None, None)
    assert "boom" not in h["waiting"]["summary"]
    assert m.get(f"/api/jobs/{hidden}").json()["error"] is None
    assert client.get(f"/api/jobs/{hidden}").json()["error"] == leak  # the owner sees it
    st = m.get(f"/api/works/{shown}/audiobook").json()
    assert (st["job"]["id"], st["job"]["can_cancel"]) == (mine, True)

    r = api_post(m, f"/api/jobs/{auto}/cancel", m_csrf)
    assert (r.status_code, r.json()["detail"]) == (403, "not_your_job")
    assert job(auto).status == JobStatus.QUEUED
    assert api_post(m, f"/api/jobs/{mine}/cancel", m_csrf).json()["status"] == "cancelled"
    assert api_post(client, f"/api/jobs/{auto}/cancel", owner["csrf"]).json()["status"] == (
        "cancelled"
    )


@pytest.mark.parametrize("kind", ["tts", "ocr", "transcode", "pdf2epub"])
def test_trashing_a_book_does_not_cancel_a_job_that_finished_meanwhile(client, owner, kind):
    """Issue #117 (3): trash cancels every kind with the conditional UPDATE
    (lectern.queue.cancel_row), so a job that finished after it was read stays finished."""
    from lectern import trash

    w = add_work("Trashed mid-run")
    leased = add_job(kind, w, status=JobStatus.LEASED, lease_token_hash="0" * 64)
    live = add_job(kind, add_work("Trashed idle"), status=JobStatus.LEASED,
                   lease_token_hash="0" * 64)  # fmt: skip

    class Rows(list):
        def all(self):
            return list(self)

    with session_factory()() as db:
        read = db.scalars

        def racing(stmt, *a, **kw):  # the job finishes right after trash read it as live
            rows = Rows(read(stmt, *a, **kw))
            with session_factory()() as other:
                other.get(Job, leased).status = JobStatus.SUCCEEDED
                other.commit()
            return rows

        db.scalars = racing
        trash._cancel_jobs(db, w)
        db.commit()
    assert job(leased).status == JobStatus.SUCCEEDED

    with session_factory()() as db:  # still live: cancelled, lease dropped
        trash._cancel_jobs(db, job(live).work_id)
        db.commit()
    after = job(live)
    assert (after.status, after.lease_token_hash) == (JobStatus.CANCELLED, None)


def test_cancelling_an_alignment_job_rereads_the_alignment_first(client, owner):
    """Issue #117 (5): align.jobs.cancel_job re-reads the alignment before failing it: a copy
    loaded earlier in this session must not overwrite a state committed meanwhile."""
    from lectern.models import Alignment, AlignmentStatus

    w = add_work("Re-read")
    queued = add_job("align", w)
    with session_factory()() as db:
        db.add(Alignment(work_id=w, model="m", status=AlignmentStatus.PENDING,
                         epub_sha256="e", audio_fingerprint="a"))  # fmt: skip
        db.commit()
    with session_factory()() as db:
        stale = db.get(Job, queued)
        alignment = align_jobs.work_alignment(db, w)  # loaded, and held, before the change
        assert alignment.status == AlignmentStatus.PENDING
        with session_factory()() as other:
            align_jobs.work_alignment(other, w).status = AlignmentStatus.SUCCEEDED
            other.commit()
        assert align_jobs.cancel_job(db, stale) is True  # the job itself was still live
        db.commit()
        assert alignment.status == AlignmentStatus.SUCCEEDED
    assert job(queued).status == JobStatus.CANCELLED
    with session_factory()() as db:
        assert align_jobs.work_alignment(db, w).status == AlignmentStatus.SUCCEEDED
