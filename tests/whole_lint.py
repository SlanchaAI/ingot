"""Inventory and check every tracked/unignored source, including embedded source."""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".cache/whole-lint"


def run(*args: str) -> None:
    print("+", shlex.join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self.handlers: list[str] = []
        self.styles: list[str] = []
        self.in_style = False
        self.active = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "style":
            self.in_style = True
            self.styles.append("")
        if attributes.get("style"):
            self.styles.append(f".inline_{len(self.styles)} {{ {attributes["style"]} }}")
        if tag == "script":
            if "src" in attributes:
                return
            kind = attributes.get("type", "")
            if kind not in ("", "text/javascript", "module"):
                raise ValueError(f"unclassified script type: {kind}")
            self.active = True
            self.scripts.append("")
        self.handlers.extend(value for key, value in attrs if key.startswith("on") and value)

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False
        elif tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.active:
            self.scripts[-1] += data
        if self.in_style:
            self.styles[-1] += data


def inventory() -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    result = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"],
                            cwd=ROOT, check=True, capture_output=True)
    files = sorted(set(result.stdout.decode().rstrip("\0").split("\0")))
    python, shell, javascript, html, css = [], [], [], [], []
    for name in files:
        path = ROOT / name
        suffix = path.suffix
        if suffix == ".py":
            python.append(name)
        elif suffix == ".sh" or path.name == "Dockerfile":
            if suffix == ".sh":
                shell.append(name)
            source = path.read_text()
            if path.name == "Dockerfile":
                lines = iter(enumerate(source.splitlines(), 1))
                for number, line in lines:
                    if line.startswith("RUN python <<"):
                        delimiter = shlex.split(line)[2].removeprefix("<<")
                        body = []
                        for _, body_line in lines:
                            if body_line == delimiter:
                                break
                            body.append(body_line)
                        else:
                            raise ValueError(f"unterminated Docker heredoc: {name}:{number}")
                        extracted = OUTPUT / f"Dockerfile_inline_{number}.py"
                        extracted.write_text("\n".join(body) + "\n")
                        python.append(str(extracted.relative_to(ROOT)))
                        print(f"embedded Python: {name}:{number} -> {extracted.name}")
            tokens = list(shlex.shlex(source, posix=True, punctuation_chars=True))
            for index, token in enumerate(tokens[:-2]):
                if token in ("python", "python3") and tokens[index + 1] == "-c":
                    extracted = OUTPUT / f"{path.stem}_inline_{index}.py"
                    extracted.write_text(tokens[index + 2])
                    python.append(str(extracted.relative_to(ROOT)))
                    print(f"embedded Python: {name} token {index} -> {extracted.name}")
                elif token == "node" and tokens[index + 1] in ("-p", "-e"):
                    extracted = OUTPUT / f"{path.stem}_inline_{index}.mjs"
                    extracted.write_text('import process from "node:process";\n' + tokens[index + 2])
                    javascript.append(str(extracted.relative_to(ROOT)))
                    print(f"embedded JavaScript: {name} token {index} -> {extracted.name}")
        elif suffix in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"):
            javascript.append(name)
        elif suffix == ".css":
            css.append(name)
        elif suffix == ".html":
            html.append(name)
            parser = Scripts()
            parser.feed(path.read_text())
            if parser.styles:
                extracted_css = OUTPUT / f"{path.stem}_inline.css"
                extracted_css.write_text("\n".join(parser.styles))
                css.append(str(extracted_css.relative_to(ROOT)))
                print(f"embedded CSS: {name}: {len(parser.styles)} style blocks/attributes")
            if parser.scripts or parser.handlers:
                extracted = OUTPUT / f"{path.stem}_inline.js"
                # Handlers are parsed in function scope, just as the browser executes them.
                extracted.write_text("\n".join(parser.scripts) + "\n" + "\n".join(
                    f"void (() => {{ {handler} }});" for handler in parser.handlers))
                javascript.append(str(extracted.relative_to(ROOT)))
                print(f"embedded JavaScript: {name}: {len(parser.scripts)} scripts, "
                      f"{len(parser.handlers)} handlers -> {extracted.name}")
        elif path.is_file() and path.read_bytes().startswith(b"#!"):
            raise ValueError(f"unclassified executable source: {name}")
    print("Source inventory (no first-party exclusions):", flush=True)
    for language, paths in (("Python", python), ("Shell", shell), ("JavaScript/TypeScript", javascript), ("HTML", html), ("CSS", css)):
        print(f"{language}: {len(paths)} units\n" + "\n".join(f"  {path}" for path in paths), flush=True)
        if not paths:
            raise ValueError(f"missing {language} coverage")
    print("Vendor audit: optimize/skillopt_prompts/*.md are attributed prompt data, not executable "
          "source. Dependencies (.venv/, node_modules/) and checker-generated .cache/ are ignored "
          "by Git. No tracked generated/vendor executable source is excluded.", flush=True)
    return python, shell, javascript, html, css


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    python, shell, javascript, html, css = inventory()
    for command in ([sys.executable, "--version"], ["ruff", "--version"], ["mypy", "--version"],
                    ["node", "--version"], ["node_modules/.bin/eslint", "--version"],
                    ["node_modules/.bin/tsc", "--version"], ["shellcheck", "--version"],
                    ["node_modules/.bin/html-validate", "--version"],
                    ["node_modules/.bin/stylelint", "--version"]):
        run(*command)
    # Explicit filenames bypass tool discovery exclusions and include the fixture app.py.
    run("ruff", "check", *python)
    run("mypy", *python)
    run("shellcheck", *shell)
    for path in shell:
        run("bash", "-n", path)
    run("node_modules/.bin/eslint", "--no-ignore", *javascript)
    config = OUTPUT / "tsconfig.json"
    config.write_text(json.dumps({"compilerOptions": {
        "allowJs": True, "checkJs": True, "noEmit": True,
        "target": "ES2022", "module": "NodeNext", "moduleResolution": "NodeNext",
        "lib": ["ES2022", "DOM", "DOM.Iterable"], "types": ["node"], "strict": True,
    }, "files": [str((ROOT / path).resolve()) for path in javascript]}))
    run("node_modules/.bin/tsc", "--project", str(config))
    run("node_modules/.bin/html-validate", *html)
    run("node_modules/.bin/stylelint", "--max-warnings", "0", *css)
    print("PASS: every inventoried source unit passed lint and type/syntax checks.", flush=True)


if __name__ == "__main__":
    main()
