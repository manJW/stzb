"""Build a self-contained Windows x64 installer from the local dashboard source."""
import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PYTHON = (
    "https://www.python.org/ftp/python/3.13.14/python-3.13.14-embed-amd64.zip",
    "90b4e5b9898b72d744650524bff92377c367f44bd5fbd09e3148656c080ad907",
)
PILLOW = (
    "https://files.pythonhosted.org/packages/d7/ed/1b16fb66a37e1b9e41370b93b2c614c7da761a5b2a41dc333f7af4639962/pillow-13.0.0-cp313-cp313-win_amd64.whl",
    "9dda0788a9932776a75d8bcc020076edc308ce2508b51f6739a45e3158b3fc42",
)
MODULES = (
    "archive.py", "capture_decoder.py", "collector.py", "combat_loadout.py", "defense.py", "defense_model.py",
    "game_locator.py", "installed_mode.py", "intelligence.py", "lineup_prediction.py",
    "member_receiver.py", "model.py", "observation_codec.py", "official_campaigns.py", "server.py",
    "share_protocol.py", "field-definitions.json", "Windows-Ocr.ps1",
)


def extract_checked(artifact, destination):
    url, expected = artifact
    with urlopen(url, timeout=60) as response:
        raw = response.read()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(f"Checksum mismatch for {url}")
    with ZipFile(io.BytesIO(raw)) as archive:
        archive.extractall(destination)


def stage_payload(folder):
    dashboard = folder / "Dashboard"
    dashboard.mkdir(parents=True)
    for name in MODULES:
        shutil.copyfile(ROOT / name, dashboard / name)
    shutil.copytree(ROOT / "static", dashboard / "static")
    shutil.copytree(ROOT / "catalog", dashboard / "catalog", ignore=shutil.ignore_patterns("garrison-*.png"))
    # The development checkout still has account-specific defaults. An install
    # has a new local profile and must contain only neutral fallback values.
    defaults = (
        ("archive.py", r'self\.profile = profile or \{[^}]+\}',
         'self.profile = profile or {"server": "待核对", "season": "待核对", "account_role_id": "0", '
         '"account_name": "待核对", "union_id": "0", "union_name": "待核对"}'),
        ("model.py", r'"self": \{"user_id": [^}]+\}',
         '"self": {"user_id": "0", "name": "待核对", "union_id": "0", "union_name": "待核对", "confirmed": False}'),
        ("official_campaigns.py", r'(?m)^VERIFIED_NAMES = .+$', "VERIFIED_NAMES = {}"),
    )
    for name, pattern, replacement in defaults:
        path = dashboard / name
        contents, count = re.subn(pattern, replacement, path.read_text(encoding="utf-8"))
        if count != 1:
            raise ValueError(f"Could not verify neutral defaults for {name}")
        path.write_text(contents, encoding="utf-8")
    source = (HERE / "Start-Installed.ps1").read_text(encoding="utf-8")
    (folder / "Start-Installed.ps1").write_text(source, encoding="utf-8-sig")
    shutil.copyfile(HERE / "使用说明.txt", folder / "使用说明.txt")
    python = folder / "Python"
    extract_checked(PYTHON, python)
    extract_checked(PILLOW, python / "site-packages")
    with (python / "python313._pth").open("a", encoding="ascii") as paths:
        paths.write("\nsite-packages\n..\\Dashboard\n")
    subprocess.run([str(python / "python.exe"), "-X", "utf8", "-B", "-c",
                    "import PIL.Image, sqlite3, server; print('Embedded runtime ready')"],
                   cwd=dashboard, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    compiler_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stzb-installer-") as temporary:
        work = Path(temporary)
        folder = work / "payload"
        stage_payload(folder)
        payload = work / "dashboard.zip"
        with ZipFile(payload, "w", ZIP_DEFLATED, compresslevel=7) as package:
            for file in sorted(folder.rglob("*")):
                if file.is_file():
                    package.writestr(file.relative_to(folder).as_posix(), file.read_bytes())
        built = work / "setup.exe"
        subprocess.run([
            str(compiler_dir / "csc.exe"), "/nologo", "/target:winexe", "/platform:x64", "/optimize+",
            "/codepage:65001", "/reference:System.Windows.Forms.dll",
            "/reference:System.IO.Compression.dll", "/reference:System.IO.Compression.FileSystem.dll",
            f"/resource:{payload},dashboard.zip", f"/out:{built}", str(HERE / "Installer.cs"),
        ], cwd=compiler_dir, check=True)
        shutil.copyfile(built, output)
    print(f"{output}  SHA256 {hashlib.sha256(output.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
