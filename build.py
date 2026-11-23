"""Build Arcnix's pgAdmin component from official pgAdmin releases.

Downloads the official pgAdmin wheel from PyPI and a pinned portable Python
(python-build-standalone), checks both against SHA-256 digests, installs the
wheel, packs the result in Arcnix's component format, then starts the packed
backend once to check it. Uses only the standard library; run with
`python3 -I build.py ...`.

Dependency versions are pinned in `constraints/<version>.txt`. For a new
pgAdmin version, run once with `--freeze` to resolve and write that file,
review it, and commit it before building.

A wheel without pgAdmin's compiled interface is refused. Pass `--frontend`
with the `web` folder of the same release, already built, only when pgAdmin
has published such a wheel.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import socket
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
PINS = json.loads((HERE / "pins.json").read_text(encoding="utf-8"))
MANIFEST = "component.json"
EPOCH = (1980, 1, 1, 0, 0, 0)
WINDOWS = os.name == "nt"
# Headers, import libraries, Tcl/Tk and Python's own command-line tools.
SKIP_TOP = {"include", "share", "libs", "tcl", "Scripts"}
SKIP_DLL = re.compile(r"^(_tkinter\.pyd|tcl\d+t?\.dll|tk\d+t?\.dll)$", re.I)
SKIP_LIB = {"test", "idlelib", "tkinter", "ensurepip", "turtledemo", "lib2to3"}
SKIP_BIN = re.compile(r"^(pip|idle|pydoc|2to3|python.*-config)")
KEEP_DISCOVERY = re.compile(r"^(sqladmin.*|compute\.v1\.json|cloudresourcemanager.*)$")
GENERATED = "pgadmin/static/js/generated"
if WINDOWS:
    # Python, pip and pgAdmin need these on Windows. Keep in step with the
    # variables Arcnix's launcher passes; nothing else is inherited.
    CLEAN_ENV = {name: os.environ[name] for name in
                 ("SystemRoot", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                  "CommonProgramFiles", "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432")
                 if name in os.environ}
    CLEAN_ENV["PATH"] = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
else:
    CLEAN_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8"}
# Arcnix installs under C:\Users\<name>\AppData\Local\arcnix\Arcnix\data\components\pgadmin\<version>\,
# about 75 characters for an 8-character user name. Python on Windows cannot
# open longer paths than 260 characters unless long paths are enabled.
MAX_WINDOWS_ENTRY = 180
CONSTRAINTS = HERE / "constraints"
# What Arcnix's launcher writes to pgAdmin's distribution config. pgAdmin
# ignores names it does not know, so each must still exist in config.py.
ARCNIX_SETTINGS = {
    "SERVER_MODE": False, "MASTER_PASSWORD_REQUIRED": False, "USE_OS_SECRET_STORAGE": False,
    "ALLOW_SAVE_PASSWORD": False, "ALLOW_SAVE_TUNNEL_PASSWORD": False, "UPGRADE_CHECK_ENABLED": False,
}
ARCNIX_PATHS = {
    "DATA_DIR": "", "LOG_FILE": "pgadmin4.log", "SQLITE_PATH": "pgadmin4.db",
    "SESSION_DB_PATH": "sessions", "STORAGE_DIR": "storage",
    "AZURE_CREDENTIAL_CACHE_DIR": "azure", "KERBEROS_CCACHE_DIR": "kerberos",
}


def fetch(url, target, sha256):
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as response, open(target, "wb") as out:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != sha256:
        raise SystemExit(f"{url}: SHA-256 {digest.hexdigest()}, expected {sha256}")


def official_wheel(version):
    """The wheel PyPI lists for this release, with PyPI's digest."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/pgadmin4/{version}/json", timeout=60) as response:
        files = json.load(response)["urls"]
    wheels = [f for f in files if f["packagetype"] == "bdist_wheel" and not f.get("yanked")]
    if len(wheels) != 1:
        raise SystemExit(f"Expected one pgAdmin {version} wheel on PyPI, found {len(wheels)}")
    return wheels[0]["filename"], wheels[0]["url"], wheels[0]["digests"]["sha256"]


def release(folder):
    source = (folder / "version.py").read_text(encoding="utf-8")
    return ".".join(re.search(rf"^APP_{part} = (\d+)", source, re.M).group(1) for part in ("RELEASE", "REVISION"))


def safe(name):
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts


def extract(archive, target):
    with tarfile.open(archive) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(target, filter="data")
            return
        # Python before 3.12: the same checks for what this archive contains.
        for member in tar.getmembers():
            link = (member.issym() or member.islnk()) and not safe(member.linkname)
            if not safe(member.name) or link or not (member.isfile() or member.isdir() or member.issym()):
                raise SystemExit(f"Unsafe entry in {archive.name}: {member.name}")
        tar.extractall(target)


def interpreter(python):
    """The interpreter inside a python-build-standalone tree, relative to it."""
    return Path("python.exe") if WINDOWS else Path("bin/python3")


def site_library(python):
    """Where the standard library and site-packages live, relative to the tree."""
    if WINDOWS:
        return Path("Lib")
    return next(p.relative_to(python) for p in (python / "lib").glob("python3.*") if p.is_dir())


def pip(python, work, *arguments):
    env = {**CLEAN_ENV, "HOME": str(work), "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INPUT": "1",
           "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    return subprocess.run([python / interpreter(python), "-I", "-m", "pip", *arguments], env=env,
                          check=True, stdout=subprocess.PIPE, text=True).stdout


def prepare(platform, version, work, constraints):
    """A portable Python with the official pgAdmin wheel installed. Without
    `constraints`, pip resolves the newest matching dependencies."""
    pin = PINS["python"][platform]
    archive = work / "python.tar.gz"
    fetch(pin["url"], archive, pin["sha256"])
    extract(archive, work / "runtime")
    python = work / "runtime" / "python"
    name, url, sha256 = official_wheel(version)
    wheel = work / name
    fetch(url, wheel, sha256)
    pinned = ["--constraint", str(constraints)] if constraints else []
    print(pip(python, work, "install", "--no-cache-dir", "--prefer-binary", *pinned, str(wheel)))
    return python, sha256


def freeze(python, work, version, path):
    """Record the dependency versions pip resolved for this pgAdmin."""
    installed = pip(python, work, "freeze", "--all", "--exclude", "pgadmin4", "--exclude", "pip")
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"# Dependencies of pgAdmin {version}, resolved with build.py --freeze.\n"
                    + installed, encoding="utf-8")


def wanted(path, library):
    parts = path.parts
    if "__pycache__" in parts or path.suffix in {".map", ".pyc"}:
        return False
    if parts[0] in SKIP_TOP or (parts[0] == "bin" and SKIP_BIN.match(parts[1])):
        return False
    if parts[0] == "DLLs" and len(parts) > 1 and SKIP_DLL.match(parts[1]):
        return False
    if parts[0] == "lib" and len(parts) > 1 and re.match(r"^(tcl|tk|itcl|thread|pkgconfig)", parts[1]):
        return False
    if path.is_relative_to(library):
        inner = path.relative_to(library).parts
        if inner and inner[0] in SKIP_LIB:
            return False
        if inner[:1] == ("site-packages",) and len(inner) > 1:
            if inner[1] == "pip" or inner[1].startswith("pip-") or inner[1:3] == ("pgadmin4", "docs"):
                return False
            # pgAdmin's regression suite and test data: not used at runtime,
            # and they hold the longest paths.
            if inner[1] == "pgadmin4" and ("tests" in inner[2:-1] or inner[2:3] == ("regression",)):
                return False
            if inner[1:4] == ("googleapiclient", "discovery_cache", "documents"):
                return len(inner) < 5 or bool(KEEP_DISCOVERY.match(inner[4]))
    return True


def pack(python, version, output, frontend=None):
    """Write the component: documentation, tests and build tools left out,
    links stored as files, and one interpreter instead of its aliases."""
    library = site_library(python)
    pgadmin = library / "site-packages" / "pgadmin4"
    generated = pgadmin / GENERATED
    if release(python / pgadmin) != version:
        raise SystemExit(f"Expected pgAdmin {version}, found {release(python / pgadmin)}")
    if frontend is not None:
        if release(frontend) != version:
            raise SystemExit(f"The interface was built from pgAdmin {release(frontend)}, not {version}")
        compiled = frontend / GENERATED
    else:
        compiled = python / generated
    if not (compiled / "app.bundle.js").is_file():
        raise SystemExit("This pgAdmin wheel has no compiled interface. Build it from the same "
                         "release and pass --frontend, or wait for a fixed wheel.")
    manifest = {"version": version, "python": interpreter(python).as_posix(), "pgadmin": pgadmin.as_posix()}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        def add(name, content, executable=False):
            entry = zipfile.ZipInfo(name, EPOCH)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
            package.writestr(entry, content)

        add(MANIFEST, json.dumps(manifest))
        if not WINDOWS:
            add("bin/python3", (python / "bin/python3").resolve().read_bytes(), executable=True)
        for path in sorted(python.rglob("*")):
            relative = path.relative_to(python)
            if relative.parts[0] == "bin" or not path.is_file() or not wanted(relative, library):
                continue
            if frontend is not None and relative.is_relative_to(generated):
                continue
            add(relative.as_posix(), path.read_bytes(), bool(path.stat().st_mode & 0o100))
        if frontend is not None:
            for path in sorted(compiled.rglob("*")):
                if path.is_file() and path.suffix != ".map":
                    add((generated / path.relative_to(compiled)).as_posix(), path.read_bytes())
    if WINDOWS:
        with zipfile.ZipFile(output) as package:
            long = [name for name in package.namelist() if len(name) > MAX_WINDOWS_ENTRY]
        if long:
            output.unlink()
            raise SystemExit(f"{len(long)} paths are longer than {MAX_WINDOWS_ENTRY} characters, "
                             f"such as {max(long, key=len)}")


def unpack(package, target):
    with zipfile.ZipFile(package) as archive:
        for entry in archive.infolist():
            name = PurePosixPath(entry.filename)
            if name.is_absolute() or ".." in name.parts:
                raise SystemExit(f"Unsafe path in package: {entry.filename}")
            path = target.joinpath(*name.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(entry))
            path.chmod((entry.external_attr >> 16) & 0o755)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def status(url):
    """The status of one request, without following redirects."""
    try:
        with urllib.request.build_opener(NoRedirect).open(url, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return None


def smoke_test(package, work):
    """Start the packed backend the way Arcnix does and check its interface."""
    root = work / "check"
    unpack(package, root)
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    python, pgadmin = root / manifest["python"], root / manifest["pgadmin"]
    data = work / "data"
    data.mkdir()
    settings = {**{name: str(data / path) for name, path in ARCNIX_PATHS.items()}, **ARCNIX_SETTINGS}
    distro = data / "config_distro.py"
    distro.write_text("".join(f"{name} = {value!r}\n" for name, value in settings.items()), encoding="utf-8")
    config = (pgadmin / "config.py").read_text(encoding="utf-8")
    unknown = [name for name in settings if not re.search(rf"^\s*{name} = ", config, re.M)]
    reads_distro = "CONFIG_DISTRO_FILE_PATH" in (pgadmin / "pgadmin/evaluate_config.py").read_text(encoding="utf-8")
    servers = data / "servers.json"
    servers.write_text(json.dumps({"Servers": {"1": {
        "Name": "check", "Group": "Arcnix", "Host": "127.0.0.1", "Port": 5432,
        "MaintenanceDB": "postgres", "Username": "postgres",
        "ConnectionParameters": {"sslmode": "prefer", "connect_timeout": 15},
    }}}), encoding="utf-8")
    setup_env = {**CLEAN_ENV, "HOME": str(data), "CONFIG_DISTRO_FILE_PATH": str(distro)}
    for command in (["setup-db"], ["load-servers", str(servers)]):
        subprocess.run([python, "-s", "-B", pgadmin / "setup.py", *command], cwd=data, env=setup_env,
                       stdin=subprocess.DEVNULL, check=True)
    # The columns Arcnix's launcher updates on every start.
    # Closed explicitly: Windows can't delete a file that is still open.
    database = sqlite3.connect(data / "pgadmin4.db")
    try:
        saved = database.execute(
            "SELECT server.host, server.port, server.maintenance_db, server.username FROM server "
            "JOIN servergroup ON servergroup.id = server.servergroup_id "
            "WHERE servergroup.name = 'Arcnix' AND server.name = 'check'").fetchall()
    finally:
        database.close()
    contract = {
        "pgAdmin knows every setting Arcnix overrides" + (f" (missing: {', '.join(unknown)})" if unknown else ""): not unknown,
        "pgAdmin reads CONFIG_DISTRO_FILE_PATH": reads_distro,
        "setup-db and load-servers save the server where Arcnix updates it":
            saved == [("127.0.0.1", 5432, "postgres", "postgres")],
    }
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    key = secrets.token_hex(16)
    env = {**CLEAN_ENV, "HOME": str(data), "PGADMIN_INT_PORT": str(port), "PGADMIN_INT_KEY": key,
           "PGADMIN_SERVER_MODE": "OFF", "CONFIG_DISTRO_FILE_PATH": str(distro)}
    log = data / "backend.log"
    origin = f"http://127.0.0.1:{port}"
    with open(log, "wb") as output:
        backend = subprocess.Popen(
            [python, "-s", "-B", pgadmin / "pgAdmin4.py"],
            cwd=data, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 180
        while status(f"{origin}/misc/ping?key={key}") != 200:
            if backend.poll() is not None or time.monotonic() > deadline:
                raise SystemExit("pgAdmin did not start:\n" + log.read_text(errors="replace")[-4000:])
            time.sleep(1)
        checks = {
            **contract,
            "a request without the key is refused": status(f"{origin}/misc/ping") == 401,
            "the window page loads": 200 <= (status(f"{origin}/?key={key}") or 0) < 400,
            "the compiled interface is served": status(f"{origin}/static/js/generated/app.bundle.js?key={key}") == 200,
        }
        for check, passed in checks.items():
            print(f"{'ok' if passed else 'FAILED'}: {check}")
        if not all(checks.values()):
            raise SystemExit("The packed backend failed its checks")
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=15)
        except subprocess.TimeoutExpired:
            backend.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pgadmin", required=True, help="pgAdmin version on PyPI, such as 9.19")
    parser.add_argument("--build", required=True, type=int, help="build number for this pgAdmin version")
    parser.add_argument("--platform", required=True, choices=sorted(PINS["python"]))
    parser.add_argument("--frontend", type=Path, help="built web folder of the same release")
    parser.add_argument("--out", type=Path, default=HERE / "dist")
    parser.add_argument("--freeze", action="store_true",
                        help="resolve dependencies, write constraints/<version>.txt and stop")
    arguments = parser.parse_args()
    match = re.fullmatch(r"(\d+\.\d+)(\.post\d+)?", arguments.pgadmin)
    if not match or arguments.build < 1:
        raise SystemExit("Expected a version such as 9.19 and a build number from 1")
    version = match.group(1)
    output = arguments.out.resolve() / f"pgadmin-{arguments.pgadmin}-{arguments.build}-{arguments.platform}.zip"
    constraints = CONSTRAINTS / f"{arguments.pgadmin}.txt"
    if not arguments.freeze and not constraints.is_file():
        raise SystemExit(f"Missing {constraints.relative_to(HERE)}. Run with --freeze, review and commit it.")
    with tempfile.TemporaryDirectory(prefix="pgadmin-component-") as temporary:
        work = Path(temporary)
        python, wheel_sha256 = prepare(arguments.platform, arguments.pgadmin, work,
                                       None if arguments.freeze else constraints)
        if arguments.freeze:
            freeze(python, work, arguments.pgadmin, constraints)
            print(f"Wrote {constraints.relative_to(HERE)}")
            return
        frontend = arguments.frontend.resolve() if arguments.frontend else None
        pack(python, version, output, frontend)
        smoke_test(output, work)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(f"{output.name}  {output.stat().st_size} bytes  sha256:{digest}")
    print(f"pgAdmin wheel sha256:{wheel_sha256}  interface: {'built from source' if frontend else 'official wheel'}")


if __name__ == "__main__":
    main()
