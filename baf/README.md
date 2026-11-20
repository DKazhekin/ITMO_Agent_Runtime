# Basic agent functions

The executable catalog covers 35 individual tasks: the course's B1–B8 tasks, one original variant per course function, and three tasks for the original workbook-formula function B9. The nine `.1` tasks are the standard replay smoke suite. C1–C3 are listed for a later evaluation.

Each task contains `task.yaml`, `fixture/`, and `check.py`. B1.1 preserves the original course files, including `messages`, `timeout_s` and its checker JSON contract. Checkers print a JSON verdict with `pass` and `reason`. Evaluate both the verdict and the process status.

Only fixture contents and the prompt are available inside the session. Expectations, hidden cases and validation examples remain outside it. Real attempts use a fresh client-mounted directory. The runner reads the resulting local files after the session closes. Mock attempts default to snapshot workspaces and can also use the mount.

The code-generation checker runs submitted Python with the local `itmo-sandbox:0.1` image. The container has no network or host mounts and receives the submission and input cases through standard input. Expected answers stay on the verifier. The host runner needs access to the Docker CLI and image when running B6 or the full smoke suite.

See [commands and evidence format](../docs/baf.md).
