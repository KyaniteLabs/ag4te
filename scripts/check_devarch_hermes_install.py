"""Exercise DevArch's declared dependencies in a disposable Hermes profile."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from packaging.requirements import Requirement
from pm.environments import activation_environment, committed_venv, project_python
from ruamel.yaml import YAML

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "devarch-hermes"


def main() -> None:
    # Invoke with the runtime checkout's .venv/bin/python, from its root.
    runtime = Path.cwd()
    bin_dir = Path(sys.executable).parent
    with tempfile.TemporaryDirectory(prefix="devarch-hermes-install-") as profile:
        os.environ["HERMES_HOME"] = profile
        os.environ["VIRTUAL_ENV"] = str(bin_dir.parent)
        os.environ["PATH"] = os.pathsep.join((str(bin_dir), os.environ["PATH"]))
        result = subprocess.run(
            [
                str(bin_dir / "hermes"),
                "plugins",
                "validate",
                str(PLUGIN),
                "--install-deps",
                "--json",
            ],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        print(result.stdout, flush=True)
        validation = json.loads(result.stdout)
        if not validation["ok"]:
            raise SystemExit("Hermes validation failed")
        # The CLI may fall back to a static validation after dependency resolution fails.
        if committed_venv(runtime) is None:
            raise SystemExit(
                "Hermes did not install the plugin dependencies into the fresh profile"
            )

        manifest = YAML(typ="safe").load(PLUGIN / "plugin.yaml")
        requirements = [Requirement(value) for value in manifest["pip_dependencies"]]
        python = str(project_python(runtime))
        env = activation_environment(runtime)
        versions = json.loads(
            subprocess.check_output(
                [
                    python,
                    "-c",
                    "import json, sys; from importlib.metadata import version; "
                    "print(json.dumps({name: version(name) for name in sys.argv[1:]}))",
                    *(requirement.name for requirement in requirements),
                ],
                env=env,
                text=True,
            )
        )
        for requirement in requirements:
            installed = versions[requirement.name]
            if installed not in requirement.specifier:
                raise SystemExit(f"{requirement.name} {installed} does not satisfy {requirement}")
        print(f"Installed manifest dependencies: {versions}", flush=True)
        subprocess.run(
            [python, str(PLUGIN / "submission" / "demo_under_90s.py")], env=env, check=True
        )


if __name__ == "__main__":
    main()
