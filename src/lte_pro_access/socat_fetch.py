import hashlib
import logging
import os
import posixpath
import tarfile
import tempfile
import urllib.request
from io import BytesIO
from pathlib import Path

logger = logging.getLogger("lte_pro_access")

SOCAT_IPK_URL = (
    "https://downloads.openwrt.org/releases/17.01.6/packages/mips_24kc/packages/socat_1.7.3.1-1_mips_24kc.ipk"
)
SOCAT_IPK_SHA256 = "57f96162c901467bde1aa1f57c695fffe591e4f924c3760831b83d21dd718e7b"
SOCAT_BINARY_SHA256 = "ec8bb6f7b197eb086de29c48457d803347b4252ba2e9eee0e0197b7d871e39a5"

_IPK_MEMBER = "data.tar.gz"
_BINARY_MEMBER = "usr/bin/socat"
_MAX_IPK_BYTES = 4 * 1024 * 1024
_MAX_BINARY_BYTES = 4 * 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 60


class SocatFetchError(RuntimeError):
    """The socat package could not be downloaded, did not match its pinned hash, or has an unexpected layout."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _download(url: str, max_bytes: int) -> bytes:
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SocatFetchError(f"{url} is larger than {max_bytes} bytes")
    return data


def _binary_from_ipk(ipk: bytes) -> bytes:
    """An OpenWrt .ipk is a gzipped tar holding data.tar.gz, which holds the package files."""
    try:
        with tarfile.open(fileobj=BytesIO(ipk), mode="r:gz") as outer:
            data_member = next((m for m in outer.getmembers() if posixpath.normpath(m.name) == _IPK_MEMBER), None)
            if data_member is None or not data_member.isfile():
                raise SocatFetchError(f"{_IPK_MEMBER} not found in the .ipk")
            inner_bytes = outer.extractfile(data_member).read(_MAX_IPK_BYTES + 1)  # type: ignore[union-attr]
        if len(inner_bytes) > _MAX_IPK_BYTES:
            raise SocatFetchError(f"{_IPK_MEMBER} is larger than {_MAX_IPK_BYTES} bytes")
        with tarfile.open(fileobj=BytesIO(inner_bytes), mode="r:gz") as inner:
            binary_member = next((m for m in inner.getmembers() if posixpath.normpath(m.name) == _BINARY_MEMBER), None)
            if binary_member is None or not binary_member.isfile():
                raise SocatFetchError(f"{_BINARY_MEMBER} not found in {_IPK_MEMBER}")
            if binary_member.size > _MAX_BINARY_BYTES:
                raise SocatFetchError(f"{_BINARY_MEMBER} is larger than {_MAX_BINARY_BYTES} bytes")
            return inner.extractfile(binary_member).read()  # type: ignore[union-attr]
    except tarfile.TarError as exc:
        raise SocatFetchError(f"the .ipk is not a valid package: {exc}") from exc


def _write_atomically(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temp_name, 0o755)
        os.replace(temp_name, destination)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def install_socat(destination: Path, url: str, ipk_sha256: str, binary_sha256: str) -> Path:
    """Download the .ipk at url, verify it and the socat binary inside it against the given hashes, and write
    the binary to destination (mode 0755, replaced atomically). A destination that already holds the pinned
    binary is left alone and nothing is downloaded. fetch_socat calls this with the pinned constants.
    """
    if destination.is_file() and _sha256(destination.read_bytes()) == binary_sha256:
        logger.info("socat already present at %s", destination)
        os.chmod(destination, 0o755)
        return destination
    logger.info("downloading socat from %s", url)
    ipk = _download(url, _MAX_IPK_BYTES)
    if _sha256(ipk) != ipk_sha256:
        raise SocatFetchError(f"the .ipk from {url} does not match its pinned sha256 {ipk_sha256}")
    binary = _binary_from_ipk(ipk)
    if _sha256(binary) != binary_sha256:
        raise SocatFetchError(f"the socat binary in the .ipk does not match its pinned sha256 {binary_sha256}")
    _write_atomically(destination, binary)
    logger.info("socat written to %s", destination)
    return destination


def fetch_socat(destination: Path) -> Path:
    """Download the pinned MIPS socat (OpenWrt 17.01.6, package socat 1.7.3.1-1, mips_24kc, musl) to destination.

    The URL, the .ipk sha256 and the extracted binary's sha256 are pinned in this module and both hashes are
    verified before anything is written. This is the binary the device relay uploads as /tmp/socat; point
    Config.socat_device_binary at the returned path. Raises SocatFetchError on a mismatch or unexpected
    package layout, and OSError (including urllib.error.URLError) on network or filesystem failures.
    """
    return install_socat(Path(destination), SOCAT_IPK_URL, SOCAT_IPK_SHA256, SOCAT_BINARY_SHA256)
