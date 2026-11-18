"""Check the running Wiki container with disposable, synthetic files."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from jonex_wiki.adapters.agent_compile import _link_publication_roots
from jonex_wiki.adapters.agent_direct import _preflight, _profile
from jonex_wiki.adapters.agent_find import install_command


with tempfile.TemporaryDirectory(prefix="wiki-root-") as temp_root, tempfile.TemporaryDirectory(
    prefix="wiki-bundle-"
) as temp_bundle:
    root, bundle = Path(temp_root).resolve(), Path(temp_bundle).resolve()
    work, private = root / "work", root / "private"
    package, output = work / "package", work / "articles/authored"
    for path in (work, package, private / "agent-home", output, bundle / "indexes", bundle / "sources/articles"):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    (bundle / "indexes/manifest.json").write_text("{}")
    source = bundle / "sources/articles/sample.md"
    source.write_text("original")
    _link_publication_roots(bundle, work)
    cli = shutil.which("codex")
    profile = _profile(root, bundle, work, package, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), cli)
    command = install_command(package, bundle)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(private),
           "CODEX_HOME": str(private / "agent-home"), "TMPDIR": str(work),
           "NO_PROXY": "127.0.0.1,localhost"}
    proof = _preflight(cli, profile, bundle, root, work, env, installed_command=command)
    if not proof["passed"]:
        raise SystemExit("Wiki Agent 沙箱预检未通过")
    code = ("from pathlib import Path\n"
            "p=Path('../../sources/articles/sample.md')\n"
            "assert p.read_text() == 'original'\n"
            "try:\n p.write_text('changed')\n"
            "except OSError: print('write=DENIED')\n"
            "else: raise SystemExit('write unexpectedly allowed')\n")
    result = subprocess.run([cli, *profile, "sandbox", str(Path(sys.executable).resolve()), "-I", "-c", code],
                            cwd=output, env=env, text=True, capture_output=True, timeout=30)
    if result.returncode or "write=DENIED" not in result.stdout or source.read_text() != "original":
        raise SystemExit("Wiki Agent 来源只读预检未通过")
    print("Wiki Agent 沙箱预检通过；来源文件只读")
