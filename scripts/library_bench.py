"""Large-library benchmark (issue #140). Local only: never point it at a live deployment.

    uv run python scripts/library_bench.py seed <dir> [books]      # default 4000 books
    uv run python scripts/library_bench.py run <dir> [--cpus 1]    # start a server, time endpoints

``seed`` builds ``<dir>/data`` and ``<dir>/library``: an owner and a reader, ``books``
works (70% with a real JPEG cover, the rest with a generated one), 120 shelves, reading
state, progress, sentences, and a year of reading stats. ``run`` starts Lectern on a
loopback port pinned to ``--cpus`` CPUs (``taskset``), times each endpoint (p50/p95 of 15
requests; covers: 15 different works, so every request is a cold cache), and prints the
server's peak RSS (``VmHWM``). Constrain memory with a container (``--memory=512m``) when
Docker is available; RSS is reported either way.
"""

from __future__ import annotations

import base64
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

COOKIE = "lectern_session"
OPDS = ("owner", "bench-opds-app-password")
TOKENS = {"owner": "bench-owner-session-token", "reader": "bench-reader-session-token"}
PORT = 18140


def settings(root: Path):
    from lectern.config import Settings

    return Settings(
        data_dir=root / "data", library_dir=root / "library", base_url=f"http://127.0.0.1:{PORT}",
        insecure_http=True,
    )  # fmt: skip


def covers() -> list[bytes]:
    """Two dozen distinct, noisy 600x900 JPEGs (about 150 KB each, like scanned covers)."""
    from io import BytesIO

    from PIL import Image

    rng = random.Random(1)  # noqa: S311
    out = []
    for i in range(24):
        noise = Image.frombytes("RGB", (150, 225), rng.randbytes(150 * 225 * 3))
        img = noise.resize((600, 900), Image.BICUBIC)
        img = Image.blend(img, Image.new("RGB", img.size, (i * 10 % 255, 90, 160)), 0.6)
        buf = BytesIO()
        img.save(buf, "JPEG", quality=88)
        out.append(buf.getvalue())
    return out


def seed(root: Path, books: int) -> None:
    from sqlalchemy import insert

    from lectern import db as lectern_db
    from lectern import premigration
    from lectern.models import (
        AppPassword,
        BookFile,
        FileKind,
        ProgressSource,
        ReadingActivity,
        ReadingProgress,
        ReadingState,
        ReadingStatsDay,
        Role,
        Sentence,
        Session,
        Shelf,
        ShelfGrant,
        ShelfItem,
        ShelfPermission,
        User,
        Work,
        WorkProgress,
    )  # fmt: skip
    from lectern.security.tokens import sha256_hex

    s = settings(root)
    if s.db_path.exists():
        sys.exit(f"{s.db_path} exists; use an empty directory")
    s.library_dir.mkdir(parents=True, exist_ok=True)
    engine = lectern_db.init_engine(s)
    premigration.upgrade(engine, s)
    rng = random.Random(140)  # noqa: S311
    now = datetime.now(UTC)
    jpgs = covers()
    first = "Ada Ben Cleo Dara Eli Fay Gus Hana Ivo Jun Kai Lena Milo Nia Omar Pia".split()
    last = "Abbott Brandt Chen Duarte Ek Farouk Grimm Holt Ito Jansen Kruger Lopez Mori".split()
    words = "river stone glass winter signal harbor ember paper orchard lantern atlas".split()
    series_names = [f"{rng.choice(words).title()} Cycle {i}" for i in range(250)]
    with lectern_db.session_factory()() as db:
        owner = User(username="owner", role=Role.OWNER, webauthn_user_handle=b"o" * 16)
        reader = User(username="reader", role=Role.READER, webauthn_user_handle=b"r" * 16)
        db.add_all([owner, reader])
        db.flush()
        for u, tok in ((owner, TOKENS["owner"]), (reader, TOKENS["reader"])):
            db.add(Session(user_id=u.id, token_hash=sha256_hex(tok), csrf_token="c" * 32,
                           expires_at=now + timedelta(days=30)))  # fmt: skip
        db.add(AppPassword(user_id=owner.id, label="bench", key_hash=sha256_hex(OPDS[1])))
        work_rows, file_rows = [], []
        for i in range(1, books + 1):
            a = f"{rng.choice(first)} {rng.choice(last)}"
            title = f"{rng.choice(words).title()} {rng.choice(words).title()} {i}"
            has_cover = i % 10 < 7
            series = rng.choice(series_names) if i % 3 == 0 else None
            work_rows.append(dict(
                id=i, title=title, sort_title=title.lower(), author=a, sort_author=a.lower(),
                language=rng.choice(["en", "en", "en", "fr", "de"]), isbn=None,
                description="A synthetic description. " * 12, publisher=f"Press {i % 40}",
                published=str(1950 + i % 70), dir_path=f"{a}/{title}", has_cover=has_cover,
                created_at=now - timedelta(minutes=i), updated_at=now, version=1,
                authors=[{"name": a, "sort": a.lower()}], series=series,
                series_index=float(i % 12) if series else None,
                tags=rng.sample(words, 3), narrators=[], cover_seed=0,
                readalong_enabled=True, data_saver_enabled=True,
            ))  # fmt: skip
            file_rows.append(dict(
                id=i, work_id=i, kind=FileKind.EBOOK, rel_path=f"{title}.epub",
                media_type="application/epub+zip", size=500_000, sha256=f"{i:064x}",
                partial_md5=f"{i:032x}", added_at=now,
            ))  # fmt: skip
            folder = s.library_dir / f"{a}/{title}"
            folder.mkdir(parents=True, exist_ok=True)
            if has_cover:
                (folder / "cover.jpg").write_bytes(jpgs[i % len(jpgs)])
        db.execute(insert(Work), work_rows)
        db.execute(insert(BookFile), file_rows)
        shelves = [Shelf(owner_id=owner.id, name=f"Shelf {i}") for i in range(120)]
        db.add_all(shelves)
        db.flush()
        db.add(ShelfGrant(shelf_id=shelves[0].id, user_id=reader.id,
                          permission=ShelfPermission.VIEW))  # fmt: skip
        db.execute(insert(ShelfItem), [
            dict(shelf_id=shelves[i % 120].id, work_id=i, position=i, added_at=now)
            for i in range(1, books + 1) if i % 7 != 0
        ])  # fmt: skip
        started = range(1, books + 1, 3)
        db.execute(insert(ReadingState), [
            dict(user_id=owner.id, work_id=i, fraction=rng.random(), last_read_at=now)
            for i in started
        ])  # fmt: skip
        db.execute(insert(WorkProgress), [
            dict(user_id=owner.id, work_id=i, sentence_n=rng.randrange(100), sentence_offset=0,
                 source=ProgressSource.WEB, raw="0:0", device="bench", received_at=now,
                 updated_at=now)
            for i in started
        ])  # fmt: skip
        db.execute(insert(ReadingProgress), [
            dict(user_id=owner.id, document=f"{i:032x}", progress="/body", percentage=rng.random(),
                 device="bench", device_id="bench", timestamp=int(now.timestamp()), updated_at=now)
            for i in range(1, books + 1, 2)
        ])  # fmt: skip
        db.execute(insert(Sentence), [
            dict(work_id=i, n=n, spine_index=n // 50, char_start=n * 80, char_end=n * 80 + 79,
                 matched=False)
            for i in range(1, books + 1, 8) for n in range(150)
        ])  # fmt: skip
        acts, days = [], {}
        for _ in range(30_000):
            w = rng.randrange(1, books + 1)
            at = now - timedelta(minutes=rng.randrange(365 * 24 * 60))
            acts.append(dict(user_id=owner.id, work_id=w, source="web", at=at, from_fraction=0.1,
                             to_fraction=0.11, read_seconds=60, listened_seconds=0, pages=1.0))  # fmt: skip
            key = (w, at.strftime("%Y-%m-%d"))
            days[key] = days.get(key, 0) + 60
        db.execute(insert(ReadingActivity), acts)
        db.execute(insert(ReadingStatsDay), [
            dict(user_id=owner.id, work_id=w, day=d, read_seconds=sec, listened_seconds=0, pages=1.0)
            for (w, d), sec in days.items()
        ])  # fmt: skip
        db.commit()
    print(f"seeded {books} books under {root}")


def get(path: str, who: str = "owner") -> tuple[int, float, int]:
    auth = {"Cookie": f"{COOKIE}={TOKENS[who]}"}
    if path.startswith("/opds"):
        auth = {"Authorization": "Basic " + base64.b64encode(":".join(OPDS).encode()).decode()}
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", headers=auth)
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req) as r:  # noqa: S310 - loopback only
            size, code = len(r.read()), r.status
    except urllib.error.HTTPError as e:
        size, code = 0, e.code
    return code, (time.perf_counter() - t) * 1000, size


def rss_peak_mb(pid: int) -> float:
    kids = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()  # noqa: S603, S607
    total = 0
    for p in [pid, *map(int, kids)]:
        try:
            for line in Path(f"/proc/{p}/status").read_text().splitlines():
                if line.startswith("VmHWM"):
                    total += int(line.split()[1])
        except OSError:
            pass
    return total / 1024


# (name, path, who). "{k}" is replaced by a different work per request (cold cover cache);
# works with i % 10 < 7 have cover art, 8 and 9 mod 10 do not. Covers are cold: the first
# request after ``seed`` draws and caches them.
ENDPOINTS = [
    ("library (all, title)", "/api/library", "owner"),
    ("library first page (limit=500)", "/api/library?limit=500", "owner"),
    ("library page 5 (offset=2000)", "/api/library?limit=500&offset=2000", "owner"),
    ("library (sort=author)", "/api/library?sort=author", "owner"),
    ("library (search q=Glass)", "/api/library?q=Glass", "owner"),
    ("library (one shelf)", "/api/library?shelf=2", "owner"),
    ("library (reader, 1 shelf)", "/api/library", "reader"),
    ("library facets author", "/api/library/facets?field=author", "owner"),
    ("library count", "/api/library/count", "owner"),
    ("shelves", "/api/shelves", "owner"),
    ("stats", "/api/stats", "owner"),
    ("opds all p3", "/opds/all?page=3", "owner"),
    ("opds recent p3", "/opds/recent?page=3", "owner"),
    ("cover art, full size", "/library/works/{art}/cover.jpg", "owner"),
    ("cover art, thumbnail", "/library/works/{art}/cover.jpg?w=360", "owner"),
    ("cover generated, full size", "/library/works/{gen}/cover.jpg", "owner"),
    ("cover generated, thumbnail", "/library/works/{gen2}/cover.jpg?w=360", "owner"),
]


def run(root: Path, cpus: int) -> None:
    env = {
        **os.environ, "LECTERN_DATA_DIR": str(root / "data"),
        "LECTERN_LIBRARY_DIR": str(root / "library"), "LECTERN_INSECURE_HTTP": "1",
        "LECTERN_BASE_URL": f"http://127.0.0.1:{PORT}", "LECTERN_INGEST_ENABLED": "0",
        "LECTERN_BACKGROUND_JOBS": "0",
    }  # fmt: skip
    pin = ["taskset", "-c", ",".join(map(str, range(cpus)))] if shutil.which("taskset") else []
    cmd = [*pin, sys.executable, "-m", "uvicorn", "--factory", "lectern.main:create_app",
           "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"]  # fmt: skip
    srv = subprocess.Popen(cmd, env=env)  # noqa: S603
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz")
                break
            except OSError:
                time.sleep(0.5)
        rows = []
        for name, path, who in ENDPOINTS:
            ms, code, size = [], 0, 0
            for k in range(15):
                art, gen, gen2 = 1 + k * 10, 8 + k * 10, 9 + k * 10
                code, t, size = get(path.format(art=art, gen=gen, gen2=gen2), who)
                ms.append(t)
            ms.sort()
            rows.append((name, code, size, statistics.median(ms), ms[int(len(ms) * 0.95) - 1]))
        print("| endpoint | status | bytes | p50 ms | p95 ms |\n|---|---|---|---|---|")
        for name, code, size, p50, p95 in rows:
            print(f"| {name} | {code} | {size} | {p50:.1f} | {p95:.1f} |")
        print(f"\npeak RSS (server, {cpus} CPU): {rss_peak_mb(srv.pid):.0f} MB")
    finally:
        srv.terminate()
        srv.wait(10)


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("seed", "run"):
        sys.exit(__doc__)
    where = Path(sys.argv[2]).resolve()
    if sys.argv[1] == "seed":
        seed(where, int(sys.argv[3]) if len(sys.argv) > 3 else 4000)
    else:
        run(where, int(sys.argv[sys.argv.index("--cpus") + 1]) if "--cpus" in sys.argv else 1)
