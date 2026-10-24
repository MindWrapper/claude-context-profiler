---
paths:
  - "**/*.py"
---

# Python

The base is [PEP 8](https://peps.python.org/pep-0008/) and [PEP 257](https://peps.python.org/pep-0257/),
as the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html) applies them.
This project adds:

- **Standard library only.** A dependency changes how the tool installs (`uvx`, a plain clone), so
  ask the owner before adding one. Tests use `unittest`, and talk to nothing beyond `127.0.0.1`.
- **Python 3.9**, the `python3` that macOS ships. A module with type hints starts with
  `from __future__ import annotations`; then `str | None` and `list[str]` work in annotations. Use
  `X | Y` only inside annotations, and `if`/`elif` where 3.10 code would use `match`.
- **`encoding="utf-8"` on every `open`, `read_text` and `write_text`.** Windows defaults to the
  system code page, and recordings hold arbitrary Unicode.
- **Paths are `pathlib.Path`.** Child processes start as `[sys.executable, "-u", "-m", module]`, so
  they run on the same Python as the launcher.
- **Errors the user sees are one line on stderr**, prefixed with the part's name (`ccprof:`,
  `proxy:`, `viewer:`). Catch a broad `Exception` only at a process boundary, with a
  `# noqa: BLE001 - <reason>` comment that says why.
- **Comments say why.** A module docstring states the module's job in a sentence or two. Type hints
  go on function signatures.
- **Line length:** code may run to about 120 characters; wrap prose in comments and docstrings at
  about 100.
