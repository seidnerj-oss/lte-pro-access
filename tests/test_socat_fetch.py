import gzip
import hashlib
import io
import os
import stat
import tarfile

import pytest

import lte_pro_access
from lte_pro_access import __main__ as main_mod
from lte_pro_access import socat_fetch
from lte_pro_access.socat_fetch import SocatFetchError, install_socat

BINARY = b"\x7fELF fake socat binary"


def _tar_gz(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _ipk(binary: bytes = BINARY, binary_name: str = "./usr/bin/socat") -> bytes:
    data = _tar_gz({"./etc/config/socat": b"cfg", binary_name: binary})
    return _tar_gz({"./debian-binary": b"2.0\n", "./data.tar.gz": data, "./control.tar.gz": b"x"})


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def ipk_file(tmp_path):
    path = tmp_path / "pkg" / "socat.ipk"
    path.parent.mkdir()
    path.write_bytes(_ipk())
    return path


def install(destination, ipk_file, ipk_sha=None, binary_sha=None):
    return install_socat(
        destination,
        ipk_file.as_uri(),
        ipk_sha or _sha(ipk_file.read_bytes()),
        binary_sha or _sha(BINARY),
    )


def test_writes_the_verified_binary_executable(tmp_path, ipk_file):
    destination = tmp_path / "data" / "socat-mips"
    assert install(destination, ipk_file) == destination
    assert destination.read_bytes() == BINARY
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755
    assert [p.name for p in destination.parent.iterdir()] == ["socat-mips"]


def test_rejects_an_ipk_hash_mismatch_and_writes_nothing(tmp_path, ipk_file):
    destination = tmp_path / "socat-mips"
    with pytest.raises(SocatFetchError, match=".ipk .* does not match"):
        install(destination, ipk_file, ipk_sha="0" * 64)
    assert not destination.exists()


def test_rejects_a_binary_hash_mismatch_and_writes_nothing(tmp_path, ipk_file):
    destination = tmp_path / "socat-mips"
    with pytest.raises(SocatFetchError, match="socat binary .* does not match"):
        install(destination, ipk_file, binary_sha="0" * 64)
    assert not destination.exists()


def test_failed_fetch_keeps_an_existing_destination_intact(tmp_path, ipk_file):
    destination = tmp_path / "socat-mips"
    destination.write_bytes(b"old")
    with pytest.raises(SocatFetchError):
        install(destination, ipk_file, binary_sha="0" * 64)
    assert destination.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_replaces_a_destination_that_holds_something_else(tmp_path, ipk_file):
    destination = tmp_path / "socat-mips"
    destination.write_bytes(b"old")
    os.chmod(destination, 0o600)
    install(destination, ipk_file)
    assert destination.read_bytes() == BINARY
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755


def test_skips_the_download_when_the_destination_already_holds_the_pinned_binary(tmp_path):
    destination = tmp_path / "socat-mips"
    destination.write_bytes(BINARY)
    os.chmod(destination, 0o600)
    got = install_socat(destination, (tmp_path / "missing.ipk").as_uri(), "0" * 64, _sha(BINARY))
    assert got == destination
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755


def test_rejects_an_ipk_without_the_socat_binary(tmp_path):
    ipk = tmp_path / "other.ipk"
    ipk.write_bytes(_ipk(binary_name="./usr/bin/other"))
    with pytest.raises(SocatFetchError, match="usr/bin/socat not found"):
        install_socat(tmp_path / "out", ipk.as_uri(), _sha(ipk.read_bytes()), _sha(BINARY))


def test_rejects_an_ipk_without_data_tar(tmp_path):
    ipk = tmp_path / "nodata.ipk"
    ipk.write_bytes(_tar_gz({"./debian-binary": b"2.0\n"}))
    with pytest.raises(SocatFetchError, match="data.tar.gz not found"):
        install_socat(tmp_path / "out", ipk.as_uri(), _sha(ipk.read_bytes()), _sha(BINARY))


def test_rejects_a_file_that_is_not_a_package(tmp_path):
    ipk = tmp_path / "junk.ipk"
    ipk.write_bytes(b"not a package")
    with pytest.raises(SocatFetchError, match="not a valid package"):
        install_socat(tmp_path / "out", ipk.as_uri(), _sha(b"not a package"), _sha(BINARY))


def test_rejects_an_oversized_download(tmp_path, monkeypatch):
    ipk = tmp_path / "big.ipk"
    ipk.write_bytes(gzip.compress(b"0" * 100))
    monkeypatch.setattr(socat_fetch, "_MAX_IPK_BYTES", 10)
    with pytest.raises(SocatFetchError, match="larger than"):
        install_socat(tmp_path / "out", ipk.as_uri(), "0" * 64, _sha(BINARY))


def test_fetch_socat_uses_the_pinned_url_and_hashes(monkeypatch, tmp_path):
    calls = []

    def fake_install(destination, url, ipk_sha256, binary_sha256):
        calls.append((destination, url, ipk_sha256, binary_sha256))
        return destination

    monkeypatch.setattr(socat_fetch, "install_socat", fake_install)
    destination = tmp_path / "socat-mips"
    assert lte_pro_access.fetch_socat(destination) == destination
    assert calls == [
        (
            destination,
            "https://downloads.openwrt.org/releases/17.01.6/packages/mips_24kc/packages/socat_1.7.3.1-1_mips_24kc.ipk",
            "57f96162c901467bde1aa1f57c695fffe591e4f924c3760831b83d21dd718e7b",
            "ec8bb6f7b197eb086de29c48457d803347b4252ba2e9eee0e0197b7d871e39a5",
        )
    ]


def test_pinned_hashes_are_sha256_hex():
    for value in (socat_fetch.SOCAT_IPK_SHA256, socat_fetch.SOCAT_BINARY_SHA256):
        assert len(value) == 64 and int(value, 16) >= 0


def test_cli_fetch_socat_prints_the_path(monkeypatch, tmp_path, capsys):
    seen = []
    monkeypatch.setattr(main_mod, "fetch_socat", lambda destination: seen.append(destination) or destination)
    monkeypatch.setattr("sys.argv", ["lte_pro_access", "fetch-socat", "--destination", str(tmp_path / "s")])
    main_mod.main()
    assert seen == [tmp_path / "s"]
    assert capsys.readouterr().out.strip() == str(tmp_path / "s")


def test_cli_fetch_socat_requires_a_destination(monkeypatch):
    monkeypatch.setattr("sys.argv", ["lte_pro_access", "fetch-socat"])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 2


def test_cli_fetch_socat_reports_failures(monkeypatch, tmp_path):
    def fail(destination):
        raise SocatFetchError("boom")

    monkeypatch.setattr(main_mod, "fetch_socat", fail)
    monkeypatch.setattr("sys.argv", ["lte_pro_access", "fetch-socat", "--destination", str(tmp_path / "s")])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert "boom" in str(exc.value)


def test_cli_without_a_command_still_requires_config(monkeypatch):
    monkeypatch.setattr("sys.argv", ["lte_pro_access"])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 2


def test_cli_service_invocation_still_takes_config(monkeypatch, tmp_path):
    seen = []

    async def amain(path):
        seen.append(path)

    monkeypatch.setattr(main_mod, "amain", amain)
    monkeypatch.setattr("sys.argv", ["lte_pro_access", "--config", str(tmp_path / "c.json")])
    main_mod.main()
    assert seen == [tmp_path / "c.json"]
