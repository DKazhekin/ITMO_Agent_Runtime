"""Build the Mac application from a clean client-only source tree."""

import configparser
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import PySide6


def main():
    if sys.platform != "darwin":
        raise SystemExit("Build the macOS application on a Mac")
    root = Path(__file__).resolve().parents[1]
    output = root / "dist"
    output.mkdir(exist_ok=True)
    tool = Path(sys.executable).with_name("pyside6-deploy")
    with tempfile.TemporaryDirectory(prefix="itmo-desktop-") as tmp:
        project = Path(tmp)
        source = project / "client"
        source.mkdir()
        for path in (root / "client").iterdir():
            if path.suffix in {".py", ".swift"}:
                shutil.copy2(path, source / path.name)
        entry = project / "Workspace.py"
        entry.write_text("from client.desktop import main\nraise SystemExit(main())\n")
        spec = configparser.ConfigParser(interpolation=None)
        spec.read(Path(PySide6.__file__).parent / "scripts/deploy_lib/default.spec")
        spec["app"].update({"title": "Workspace", "project_dir": str(project),
                            "input_file": str(entry), "exec_directory": str(output)})
        spec["python"]["python_path"] = sys.executable
        spec["qt"]["modules"] = "Core,Gui,Widgets"
        spec["nuitka"]["mode"] = "standalone"
        spec["nuitka"]["extra_args"] = (
            "--jobs=4 --noinclude-qt-translations --static-libpython=no "
            "--include-data-files=client/mount_coordinate.swift=client/mount_coordinate.swift")
        config = project / "pysidedeploy.spec"
        with config.open("w") as stream:
            spec.write(stream)
        subprocess.run([str(tool), "-f", "-c", str(config)], cwd=project, check=True)
    app = output / "Workspace.app"
    if not app.is_dir():
        raise SystemExit(f"Build did not produce {app}")
    print(app)


if __name__ == "__main__":
    main()
