"""Step 0 contracts: migration 0005, model constraints, sentence map schema, positions,
visibility predicates, and the Gift of the Magi fixture."""

from __future__ import annotations

import json
import zipfile
from datetime import timedelta
from pathlib import Path

import mutagen
import pytest
import sqlalchemy as sa
from alembic import command
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from lectern.access import (
    can_create_shelf,
    can_edit_shelf,
    can_manage_shelf,
    can_see_work,
    visible_works,
)
from lectern.db import make_engine, session_factory
from lectern.ingest.epub import read_spine
from lectern.ingest.hashing import sha256_file
from lectern.migrate import alembic_config, upgrade_to_head
from lectern.models import (
    UPLOAD_CHUNK_MAX_BYTES,
    Alignment,
    AlignmentStatus,
    AudioTrack,
    Invite,
    Job,
    JobStatus,
    ProgressSource,
    Role,
    Sentence,
    Shelf,
    ShelfGrant,
    ShelfItem,
    ShelfPermission,
    UploadSession,
    UploadStatus,
    User,
    Work,
    WorkerCredential,
    WorkProgress,
    utcnow,
)
from lectern.positions import (
    SentenceIndex,
    SentencePos,
    TextPos,
    normalize_spine_text,
    text_to_xpointer,
    xpointer_spine_index,
    xpointer_to_text,
)
from lectern.sentence_map import (
    SentenceMap,
    audio_fingerprint,
    load_sentence_map,
    map_path,
    write_sentence_map,
)

FIXTURE = Path(__file__).parent / "fixtures" / "magi"
EPUB = FIXTURE / "the-gift-of-the-magi.epub"
EXCERPT = FIXTURE / "excerpt.mp3"
MAP = FIXTURE / "sentence_map.json"

NEW_TABLES = {
    "audio_tracks",
    "sentences",
    "alignments",
    "work_progress",
    "invites",
    "shelves",
    "shelf_items",
    "shelf_grants",
    "worker_credentials",
    "jobs",
    "upload_sessions",
}


# --- helpers -----------------------------------------------------------------------------


def _work(db, title="Book", dir_path=None) -> Work:
    w = Work(
        title=title,
        sort_title=title.lower(),
        author="A",
        sort_author="a",
        dir_path=dir_path or f"A/{title}",
    )
    db.add(w)
    db.flush()
    return w


def _user(db, name: str, role: Role) -> User:
    u = User(username=name, role=role, webauthn_user_handle=name.encode().ljust(16, b"\0"))
    db.add(u)
    db.flush()
    return u


def _rejected(db, obj) -> None:
    """The row violates a constraint; a SAVEPOINT keeps the rest of the test's rows."""
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(obj)
        db.flush()


@pytest.fixture
def db(client):
    """A session on the app's migrated SQLite database (client enters the lifespan)."""
    s = session_factory()()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def fixture_map() -> dict:
    return json.loads(MAP.read_text())


# --- migration ---------------------------------------------------------------------------


def test_migration_0005_upgrades_and_downgrades(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    upgrade_to_head(engine)
    insp = sa.inspect(engine)
    assert NEW_TABLES <= set(insp.get_table_names())
    assert "reading_progress" in insp.get_table_names()  # KOReader mapping kept
    job_indexes = {i["name"]: i for i in insp.get_indexes("jobs")}
    assert job_indexes["uq_jobs_active_work_kind"]["unique"]
    assert job_indexes["ix_jobs_claim"]["column_names"] == ["status", "run_after", "priority"]
    assert {c["name"] for c in insp.get_unique_constraints("work_progress")} >= {
        "uq_work_progress_user_work"
    }

    cfg = alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0004")
    tables = set(sa.inspect(engine).get_table_names())
    assert not (NEW_TABLES & tables)
    assert {"users", "works", "book_files", "reading_progress"} <= tables

    upgrade_to_head(engine)
    assert NEW_TABLES <= set(sa.inspect(engine).get_table_names())


# --- model constraints -------------------------------------------------------------------


def test_sentence_constraints(db):
    w = _work(db)
    db.add(Sentence(work_id=w.id, n=0, spine_index=0, char_start=0, char_end=5))
    db.add(
        Sentence(
            work_id=w.id,
            n=1,
            spine_index=0,
            char_start=6,
            char_end=9,
            audio_start_s=1.0,
            audio_end_s=2.0,
            matched=True,
        )
    )
    db.flush()
    # matched requires audio times
    _rejected(
        db, Sentence(work_id=w.id, n=2, spine_index=0, char_start=10, char_end=12, matched=True)
    )
    # audio times come as a pair
    _rejected(
        db,
        Sentence(work_id=w.id, n=2, spine_index=0, char_start=10, char_end=12, audio_start_s=1.0),
    )
    # empty span
    _rejected(db, Sentence(work_id=w.id, n=2, spine_index=0, char_start=10, char_end=10))
    # (work, n) unique
    _rejected(db, Sentence(work_id=w.id, n=0, spine_index=1, char_start=0, char_end=5))


def test_audio_track_constraints(db):
    w = _work(db)
    t = AudioTrack(
        work_id=w.id,
        position=0,
        rel_path="a.mp3",
        media_type="audio/mpeg",
        codec="mp3",
        duration_s=10.0,
        offset_s=0.0,
        size=1,
        sha256="a" * 64,
    )
    db.add(t)
    db.flush()
    dup = dict(media_type="audio/mpeg", codec="mp3", duration_s=1.0, offset_s=10.0, size=1)
    _rejected(db, AudioTrack(work_id=w.id, position=0, rel_path="b.mp3", sha256="b" * 64, **dup))
    _rejected(db, AudioTrack(work_id=w.id, position=1, rel_path="a.mp3", sha256="b" * 64, **dup))
    _rejected(db, AudioTrack(work_id=w.id, position=1, rel_path="b.mp3", sha256="a" * 64, **dup))
    # the same file may belong to a different work
    w2 = _work(db, "Other")
    db.add(AudioTrack(work_id=w2.id, position=0, rel_path="a.mp3", sha256="a" * 64, **dup))
    db.flush()


def test_job_lease_and_single_active_job_per_work(db):
    w = _work(db)
    cred = WorkerCredential(label="gpu", key_hash="c" * 64)
    db.add(cred)
    db.flush()
    j = Job(kind="align", work_id=w.id, status=JobStatus.QUEUED)
    db.add(j)
    db.flush()
    # a second live align job for the same work is impossible
    _rejected(db, Job(kind="align", work_id=w.id, status=JobStatus.QUEUED))
    _rejected(
        db, Job(kind="align", work_id=w.id, status=JobStatus.LEASED, lease_token_hash="t" * 64)
    )
    # a different kind is fine
    db.add(Job(kind="readaloud", work_id=w.id, status=JobStatus.QUEUED))
    db.flush()
    # leased jobs must carry a lease token
    with pytest.raises(IntegrityError), db.begin_nested():
        j.status = JobStatus.LEASED
        j.leased_by_id = cred.id
        db.flush()
    assert j.status == JobStatus.QUEUED
    j.status = JobStatus.LEASED
    j.lease_token_hash = "t" * 64
    j.leased_by_id = cred.id
    j.lease_expires_at = utcnow() + timedelta(minutes=10)
    db.flush()
    # finished jobs keep the token (idempotent result replies) and free the slot
    j.status = JobStatus.SUCCEEDED
    j.finished_at = utcnow()
    db.flush()
    db.add(Job(kind="align", work_id=w.id, status=JobStatus.QUEUED))
    db.flush()
    _rejected(db, Job(kind="align", work_id=w.id, status=JobStatus.QUEUED, max_attempts=0))


def test_alignment_one_per_work_and_coverage_range(db):
    w = _work(db)
    db.add(
        Alignment(
            work_id=w.id,
            model="m",
            status=AlignmentStatus.SUCCEEDED,
            coverage=0.5,
            epub_sha256="e" * 64,
            audio_fingerprint="f" * 64,
        )
    )
    db.flush()
    common = dict(
        model="m", status=AlignmentStatus.PENDING, epub_sha256="e" * 64, audio_fingerprint="f" * 64
    )
    _rejected(db, Alignment(work_id=w.id, **common))
    w2 = _work(db, "Two")
    _rejected(db, Alignment(work_id=w2.id, coverage=1.5, **common))


def test_work_progress_is_one_row_per_user_and_work(db):
    w = _work(db)
    u = _user(db, "r", Role.READER)
    db.add(
        WorkProgress(
            user_id=u.id,
            work_id=w.id,
            sentence_n=3,
            sentence_offset=4,
            source=ProgressSource.WEB,
            raw="3:4",
        )
    )
    db.flush()
    _rejected(
        db,
        WorkProgress(
            user_id=u.id, work_id=w.id, sentence_n=0, source=ProgressSource.AUDIO, raw="0"
        ),
    )
    _rejected(
        db,
        WorkProgress(
            user_id=u.id,
            work_id=_work(db, "B").id,
            sentence_n=-1,
            source=ProgressSource.AUDIO,
            raw="x",
        ),
    )


def test_upload_session_constraints(db):
    u = _user(db, "up", Role.MEMBER)
    later = utcnow() + timedelta(hours=24)

    def session(**kw) -> UploadSession:
        base = dict(
            public_id=kw.pop("public_id", "u" + str(len(db.new) + len(db.identity_map))),
            user_id=u.id,
            filename="book.epub",
            media_type="application/epub+zip",
            size=100,
            chunk_size=UPLOAD_CHUNK_MAX_BYTES,
            expires_at=later,
        )
        base.update(kw)
        return UploadSession(**base)

    ok = session(public_id="ok")
    db.add(ok)
    db.flush()
    assert ok.status is UploadStatus.ACTIVE and ok.received_bytes == 0

    _rejected(db, session(public_id="ok"))  # public_id is unique
    _rejected(db, session(size=0))
    _rejected(db, session(chunk_size=UPLOAD_CHUNK_MAX_BYTES + 1))  # one request > 50 MiB
    _rejected(db, session(chunk_size=0))
    _rejected(db, session(received_bytes=101))  # more than declared
    _rejected(db, session(received_bytes=-1))
    # COMPLETED needs every byte and the inbox hand-off name.
    _rejected(db, session(status=UploadStatus.COMPLETED, received_bytes=100))
    _rejected(db, session(status=UploadStatus.COMPLETED, inbox_name="book-abc.epub"))
    done = session(status=UploadStatus.COMPLETED, received_bytes=100, inbox_name="book-abc.epub")
    db.add(done)
    db.flush()

    db.delete(u)  # a user's partial uploads go with them
    db.flush()
    assert db.scalar(sa.select(sa.func.count()).select_from(UploadSession)) == 0


def test_invite_role_and_usability(db):
    owner = _user(db, "o", Role.OWNER)
    now = utcnow()
    inv = Invite(
        token_hash="a" * 64,
        role=Role.READER,
        created_by=owner.id,
        expires_at=now + timedelta(days=1),
    )
    db.add(inv)
    db.flush()
    assert inv.usable_at(now)
    assert not inv.usable_at(now + timedelta(days=2))
    inv.revoked_at = now
    assert not inv.usable_at(now)
    inv.revoked_at = None
    inv.used_at, inv.used_by = now, owner.id
    db.flush()
    assert not inv.usable_at(now)
    _rejected(db, Invite(token_hash="b" * 64, role=Role.OWNER, expires_at=now))
    _rejected(db, Invite(token_hash="b" * 64, role=Role.MEMBER, expires_at=now, used_at=now))
    _rejected(db, Invite(token_hash="a" * 64, role=Role.MEMBER, expires_at=now))


def test_shelf_deletion_cascades_to_items_and_grants_not_works(db):
    owner = _user(db, "o", Role.OWNER)
    reader = _user(db, "r", Role.READER)
    w = _work(db)
    shelf = Shelf(owner_id=owner.id, name="Kids")
    db.add(shelf)
    db.flush()
    db.add(ShelfItem(shelf_id=shelf.id, work_id=w.id))
    db.add(ShelfGrant(shelf_id=shelf.id, user_id=reader.id, permission=ShelfPermission.VIEW))
    db.flush()
    _rejected(db, Shelf(owner_id=owner.id, name="Kids"))
    db.delete(shelf)
    db.flush()
    db.expire_all()
    assert db.scalar(sa.select(sa.func.count()).select_from(ShelfItem)) == 0
    assert db.scalar(sa.select(sa.func.count()).select_from(ShelfGrant)) == 0
    assert db.get(Work, w.id) is not None


def test_deleting_a_work_removes_its_audio_sentences_and_progress(db):
    w = _work(db)
    u = _user(db, "m", Role.MEMBER)
    db.add(
        AudioTrack(
            work_id=w.id, position=0, rel_path="a.mp3", media_type="audio/mpeg", codec="mp3",
            duration_s=1.0, offset_s=0.0, size=1, sha256="a" * 64,
        )
    )  # fmt: skip
    db.add(Sentence(work_id=w.id, n=0, spine_index=0, char_start=0, char_end=1))
    db.add(
        WorkProgress(user_id=u.id, work_id=w.id, sentence_n=0, source=ProgressSource.WEB, raw="0:0")
    )
    db.flush()
    db.execute(sa.delete(Work).where(Work.id == w.id))
    for model in (AudioTrack, Sentence, WorkProgress):
        assert db.scalar(sa.select(sa.func.count()).select_from(model)) == 0


# --- sentence map schema ------------------------------------------------------------------


def test_fixture_map_validates(fixture_map):
    m = SentenceMap.model_validate(fixture_map)
    assert m.coverage == 1.0 and len(m.sentences) == 15
    assert m.total_duration_s == pytest.approx(77.166)


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda m: m.update(version=2), "version"),
        (lambda m: m.update(unexpected=1), "unexpected"),
        (lambda m: m.update(audio_fingerprint="0" * 64), "audio_fingerprint"),
        (lambda m: m.update(epub_sha256="nothex"), "epub_sha256"),
        (lambda m: m["sentences"][3].update(n=4), "sentence n must be"),
        (lambda m: m["sentences"][3].update(char_start=70, text=None), "overlaps"),
        (lambda m: m["sentences"][3].update(spine_index=1), "reading order"),
        (lambda m: m["sentences"][3].update(char_end=87), "char_end must be"),
        (lambda m: m["sentences"][3].update(audio_end_s=None), "go together"),
        (lambda m: m["sentences"][3].update(audio_start_s=None, audio_end_s=None), "need audio"),
        (lambda m: m["sentences"][3].update(audio_end_s=5.0), "audio_end_s must be"),
        (lambda m: m["sentences"][3].update(audio_start_s=1.0, audio_end_s=2.0), "backwards"),
        (lambda m: m["sentences"][14].update(audio_end_s=900.0), "beyond the last track"),
        (lambda m: m["sentences"][3].update(text="wrong"), "text length"),
        (lambda m: m["tracks"][0].update(position=1), "track positions"),
        (lambda m: m["tracks"][0].update(offset_s=1.0), "offset_s"),
        (lambda m: m.update(sentences=[]), "at least 1"),
    ],
)
def test_sentence_map_rejects(fixture_map, mutation, message):
    broken = json.loads(json.dumps(fixture_map))
    mutation(broken)
    with pytest.raises(ValidationError, match=message):
        SentenceMap.model_validate(broken)


def test_sentence_map_multi_track_offsets_and_unmatched_sentences(fixture_map):
    m = json.loads(json.dumps(fixture_map))
    t0 = m["tracks"][0]
    t1 = {
        "position": 1,
        "sha256": "b" * 64,
        "duration_s": 10.0,
        "offset_s": t0["duration_s"] + 0.04,
    }
    m["tracks"] = [t0, t1]
    m["audio_fingerprint"] = audio_fingerprint([t0["sha256"], t1["sha256"]])
    # an unmatched sentence without times, then one with guessed times, in the second track
    m["sentences"] += [
        {"n": 15, "spine_index": 2, "char_start": 1092, "char_end": 1100, "matched": False},
        {
            "n": 16,
            "spine_index": 3,
            "char_start": 0,
            "char_end": 4,
            "audio_start_s": 80.0,
            "audio_end_s": 81.0,
        },
    ]
    parsed = SentenceMap.model_validate(m)
    assert parsed.coverage == pytest.approx(15 / 17)
    assert (
        audio_fingerprint(["x"]) != audio_fingerprint(["x", "y"]) != audio_fingerprint(["y", "x"])
    )


def test_sentence_map_file_roundtrip(tmp_path, fixture_map):
    path = map_path(tmp_path, 7)
    assert path == tmp_path / "align" / "7.json"
    write_sentence_map(path, SentenceMap.model_validate(fixture_map))
    assert not path.with_suffix(".json.tmp").exists()
    assert load_sentence_map(path) == SentenceMap.model_validate(fixture_map)


# --- positions -----------------------------------------------------------------------------


def test_normalize_spine_text():
    xhtml = b"""<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>
    <style>p{}</style></head><body>
    <h1>  One\n two </h1><script>var x = 1;</script>tail<p>a<em>b</em>c  d</p>
    <p>&amp; e&#8217;s</p><!-- c --><div><p>x<span>y</span>z</p>w</div></body></html>"""
    # Whitespace in the source collapses to one space; no whitespace means no space ("taila").
    assert normalize_spine_text(xhtml) == "One two tailabc d & e\u2019sxyzw"
    assert normalize_spine_text(b"<html><body></body></html>") == ""
    assert normalize_spine_text(b"not xml <") in ("", "not xml")


def test_sentence_index_text_and_time_conversions(fixture_map):
    idx = SentenceIndex(SentenceMap.model_validate(fixture_map).sentences)
    assert len(idx) == 15
    # text -> sentence, both sides of a boundary (sentence 0 is chars 0-34, sentence 1 starts at 35)
    assert idx.at_text(TextPos(2, 0)) == SentencePos(0, 0)
    assert idx.at_text(TextPos(2, 33)) == SentencePos(0, 33)
    assert idx.at_text(TextPos(2, 34)) == SentencePos(1, 0)  # the gap belongs to the next
    assert idx.at_text(TextPos(2, 35)) == SentencePos(1, 0)
    assert idx.at_text(TextPos(1, 999)) == SentencePos(0, 0)  # before the first sentence
    assert idx.at_text(TextPos(2, 5000)) == SentencePos(14, 85)  # after the last: its end
    assert idx.to_text(SentencePos(1, 3)) == TextPos(2, 38)
    assert idx.to_text(SentencePos(1, 999)) == TextPos(2, 48)  # clamped to the sentence end
    # audio -> sentence: sentence 1 is 3.28-4.01 s, sentence 2 starts at 4.88
    assert idx.at_time(0.0) == SentencePos(0, 0)
    assert idx.at_time(3.28) == SentencePos(1, 0)
    assert idx.at_time(4.5) == SentencePos(1, 13)  # in the pause after it: its end
    assert idx.at_time(4.88) == SentencePos(2, 0)
    mid = idx.at_time(3.28 + (4.01 - 3.28) / 2)
    assert mid.n == 1 and 6 <= mid.offset <= 7
    assert idx.at_time(999.0) == SentencePos(14, 85)
    # sentence -> audio
    assert idx.to_time(SentencePos(1, 0)) == pytest.approx(3.28)
    assert idx.to_time(SentencePos(1, 13)) == pytest.approx(4.01)
    assert idx.to_time(SentencePos(0, 17)) == pytest.approx(0.25 + (2.78 - 0.25) / 2, abs=0.01)


def test_sentence_index_with_unmatched_sentences():
    rows = [
        Sentence(n=0, spine_index=0, char_start=0, char_end=10),
        Sentence(
            n=1,
            spine_index=0,
            char_start=11,
            char_end=20,
            audio_start_s=5.0,
            audio_end_s=6.0,
            matched=True,
        ),
        Sentence(n=2, spine_index=0, char_start=21, char_end=30),
        Sentence(
            n=3,
            spine_index=1,
            char_start=0,
            char_end=8,
            audio_start_s=9.0,
            audio_end_s=10.0,
            matched=True,
        ),
    ]
    idx = SentenceIndex(reversed(rows))
    assert idx.to_time(SentencePos(0, 4)) == 5.0  # before any match: first matched start
    assert idx.to_time(SentencePos(2, 0)) == 6.0  # unmatched: end of previous matched
    assert idx.at_time(7.0) == SentencePos(1, 9)  # between matches: end of the earlier one
    assert idx.at_time(9.5) == SentencePos(3, 4)
    assert SentenceIndex([]).at_text(TextPos(0, 0)) is None
    assert SentenceIndex([rows[0]]).at_time(1.0) is None
    with pytest.raises(ValueError):
        SentenceIndex([rows[1]])


def test_xpointer_conversions():
    assert xpointer_spine_index("/body/DocFragment[3]/body/p[2]/text().7") == 2
    assert xpointer_spine_index("/body/p[1]") is None
    xhtml = b"<html><body><h1>Title</h1>\n<p>One <i>two</i> three.</p>\n<p>Four.</p></body></html>"
    # "Title One two three. Four."
    assert xpointer_to_text("/body/DocFragment[3]/body/p[1]/text().0", xhtml) == TextPos(2, 6)
    assert xpointer_to_text("/body/DocFragment[3]/body/p[1]/i/text().1", xhtml) == TextPos(2, 11)
    assert xpointer_to_text("/body/DocFragment[3]/body/p[2]/text().2", xhtml) == TextPos(2, 23)
    assert text_to_xpointer(TextPos(2, 0), xhtml) == "/body/DocFragment[3]/body/h1/text().0"
    assert text_to_xpointer(TextPos(2, 11), xhtml) == "/body/DocFragment[3]/body/p[1]/i/text().1"
    assert text_to_xpointer(TextPos(2, 23), xhtml) == "/body/DocFragment[3]/body/p[2]/text().2"
    # tolerant: unknown paths clamp instead of failing; non-EPUB pointers are rejected
    assert xpointer_to_text("/body/DocFragment[3]/body/div[9]/text().4", xhtml) == TextPos(2, 0)
    assert xpointer_to_text("/body/DocFragment[3]/body/p[2]/text().99", xhtml) == TextPos(2, 26)
    with pytest.raises(ValueError):
        xpointer_to_text("/body/p[1]/text().0", xhtml)


# --- access ------------------------------------------------------------------------------


def test_readers_see_only_granted_shelves(db):
    owner = _user(db, "o", Role.OWNER)
    member = _user(db, "m", Role.MEMBER)
    reader = _user(db, "r", Role.READER)
    other = _user(db, "r2", Role.READER)
    a, b = _work(db, "A"), _work(db, "B")
    shelf = Shelf(owner_id=member.id, name="Shared")
    db.add(shelf)
    db.flush()
    db.add(ShelfItem(shelf_id=shelf.id, work_id=a.id))
    db.add(ShelfGrant(shelf_id=shelf.id, user_id=reader.id, permission=ShelfPermission.VIEW))
    db.flush()

    def ids(user):
        return set(db.scalars(sa.select(Work.id).where(visible_works(user)).order_by(Work.id)))

    assert ids(owner) == ids(member) == {a.id, b.id}
    assert ids(reader) == {a.id}
    assert ids(other) == set()
    assert can_see_work(db, reader, a.id) and not can_see_work(db, reader, b.id)
    assert can_see_work(db, member, b.id) and not can_see_work(db, other, a.id)

    assert can_manage_shelf(owner, shelf) and can_manage_shelf(member, shelf)
    assert not can_manage_shelf(reader, shelf)
    assert not can_edit_shelf(db, reader, shelf)
    db.get(ShelfGrant, (shelf.id, reader.id)).permission = ShelfPermission.EDIT
    db.flush()
    assert can_edit_shelf(db, reader, shelf) and not can_edit_shelf(db, other, shelf)
    assert can_create_shelf(member) and not can_create_shelf(reader)


# --- fixture integrity --------------------------------------------------------------------


def test_fixture_files_match_the_map(fixture_map):
    m = SentenceMap.model_validate(fixture_map)
    assert m.epub_sha256 == sha256_file(EPUB)
    assert m.tracks[0].sha256 == sha256_file(EXCERPT)
    assert m.audio_fingerprint == audio_fingerprint([m.tracks[0].sha256])
    audio = mutagen.File(EXCERPT)
    assert audio.info.length == pytest.approx(m.tracks[0].duration_s, abs=0.05)
    assert EXCERPT.stat().st_size < 300_000 and EPUB.stat().st_size < 100_000


def test_fixture_sentences_are_exact_epub_text_slices(fixture_map):
    spine = read_spine(EPUB)
    assert spine[2].endswith("7256-h-1.htm.html")
    with zipfile.ZipFile(EPUB) as zf:
        story = normalize_spine_text(zf.read(spine[2]))
    assert story.startswith("One dollar and eighty-seven cents.")
    assert len(story) == 29283
    m = SentenceMap.model_validate(fixture_map)
    prev_end = 0
    for s in m.sentences:
        assert s.spine_index == 2 and s.text is not None
        assert story[s.char_start : s.char_end] == s.text
        assert story[prev_end : s.char_start].strip() == ""  # nothing skipped between sentences
        prev_end = s.char_end
        assert s.matched and 0 <= s.audio_start_s < s.audio_end_s <= m.total_duration_s
    assert m.sentences[0].text == "One dollar and eighty-seven cents."
    assert m.sentences[14].text.endswith("\u201cMr. James Dillingham Young.\u201d")


def test_models_match_migrations_and_0006_round_trips(tmp_path):
    """The ORM models and the migration chain describe the same schema; 0006 downgrades."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from lectern.models import Base

    engine = make_engine(f"sqlite:///{tmp_path / 'm6.db'}")
    upgrade_to_head(engine)
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    m2 = {"notifications", "known_devices", "download_events", "match_suggestions"}
    assert m2 <= set(sa.inspect(engine).get_table_names())
    cfg = alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0005")
    insp = sa.inspect(engine)
    assert not (m2 & set(insp.get_table_names())) and NEW_TABLES <= set(insp.get_table_names())
    assert "idempotency_key" not in {c["name"] for c in insp.get_columns("upload_sessions")}
    upgrade_to_head(engine)


def test_migration_0007_backfills_authors_and_round_trips(tmp_path):
    """Existing works get ``authors`` from their author columns; 0007 downgrades cleanly."""
    from lectern.models import Base

    engine = make_engine(f"sqlite:///{tmp_path / 'm7.db'}")
    cfg = alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0006")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO works (title, sort_title, author, sort_author, dir_path, has_cover,"
                " created_at, updated_at) VALUES ('Kim', 'kim', 'Rudyard Kipling',"
                " 'kipling, rudyard', 'Rudyard Kipling/Kim', 0, '2026-01-01', '2026-01-01')"
            )
        )
    upgrade_to_head(engine)
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT authors, version, readalong_enabled FROM works")).one()
        assert json.loads(row[0]) == [{"name": "Rudyard Kipling", "sort": "kipling, rudyard"}]
        assert (row[1], row[2]) == (1, 1)
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0006")
    insp = sa.inspect(engine)
    assert "imports" not in insp.get_table_names()
    assert "authors" not in {c["name"] for c in insp.get_columns("works")}
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT title FROM works")).scalar() == "Kim"
