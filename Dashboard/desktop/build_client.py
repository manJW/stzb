"""Build the Windows desktop UI, reusing the existing local Dashboard service."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path
from zipfile import ZipFile

VERSION = "45.3.1"
ASSET = f"electron-v{VERSION}-win32-x64.zip"


def build(source, archive, checksums, output, dashboard):
    expected = next((line.split()[0] for line in checksums.read_text().splitlines()
                     if line.split()[-1].lstrip("*") == ASSET), None)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if not expected or digest != expected:
        raise ValueError("Official Electron checksum verification failed")
    if output.exists():
        raise FileExistsError(f"Output already exists; select a new folder: {output}")
    output.mkdir(parents=True)
    with ZipFile(archive) as package:
        for item in package.infolist():
            target = (output / item.filename).resolve()
            if not target.is_relative_to(output.resolve()):
                raise ValueError("Unsafe archive path")
        package.extractall(output)
    app = output / "resources" / "app"
    shutil.copytree(source / "app", app)
    import os
    settings = {"dashboardRoot": os.path.relpath(dashboard, app)}
    (app / "client-config.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "electron.exe").rename(output / "率土战场.exe")
    manifest = {"electron": VERSION, "sha256": digest, "source": f"https://github.com/electron/electron/releases/tag/v{VERSION}",
                "app_version": "0.1.0", "local_service": str(dashboard),
                "scope": "This client reuses the existing local service; it is not a standalone service installer."}
    (output / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copyfile(source / "使用说明.txt", output / "使用说明.txt")
    print(json.dumps({"executable": str(output / "率土战场.exe"), "electron_sha256": digest}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--electron-zip", type=Path, required=True)
    parser.add_argument("--checksums", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dashboard", type=Path, required=True)
    args = parser.parse_args()
    build(Path(__file__).resolve().parent, args.electron_zip, args.checksums, args.output.resolve(), args.dashboard.resolve())
