import hashlib

from lectern.ingest.hashing import partial_md5, partial_md5_offsets, sha256_file


def test_offsets_match_koreader_fast_digest():
    # KOReader: for i = -1, 10 do offset = lshift(1024, 2*i); LuaJIT wraps lshift(1024, -2) to 0.
    assert list(partial_md5_offsets()) == [0] + [1024 << (2 * i) for i in range(11)]
    assert list(partial_md5_offsets())[-1] == 1024 << 20


def test_partial_md5_samples_exact_bytes(tmp_path):
    # Build a file where each 1 KiB sample region is distinct so a wrong offset changes the digest.
    size = (1024 << 20) + 2048
    path = tmp_path / "big.bin"
    with path.open("wb") as f:
        f.truncate(size)
        for n, off in enumerate(partial_md5_offsets()):
            f.seek(off)
            f.write(bytes([n + 1]) * 1024)
    expected = hashlib.md5(usedforsecurity=False)
    for n in range(12):
        expected.update(bytes([n + 1]) * 1024)
    assert partial_md5(path) == expected.hexdigest()


def test_partial_md5_short_file_stops_at_eof(tmp_path):
    path = tmp_path / "small.bin"
    data = b"a" * 1500  # covers offset 0 fully and offset 1024 partially; offset 4096 is past EOF
    path.write_bytes(data)
    expected = hashlib.md5(data[:1024] + data[1024:2048], usedforsecurity=False).hexdigest()
    assert partial_md5(path) == expected


def test_partial_md5_differs_for_different_middle(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    base = bytearray(b"\0" * 20000)
    a.write_bytes(base)
    base[4096] = 1  # inside the third sample
    b.write_bytes(base)
    assert partial_md5(a) != partial_md5(b)
    base[3000] = 1  # between samples: digest must not change
    c = tmp_path / "c.bin"
    c.write_bytes(base)
    assert partial_md5(b) == partial_md5(c)


def test_sha256_file(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"lectern" * 1000)
    assert sha256_file(p) == hashlib.sha256(b"lectern" * 1000).hexdigest()
