"""Package boundary checks shared by CI and the embedding platform."""

from importlib import resources
from importlib.metadata import version
import json
from pathlib import Path
import sysconfig


def build_info():
    target = resources.files("jonex_wiki").joinpath("_build.json")
    if not target.is_file():
        return {
            "schema": "wiki.build.v1",
            "version": version("jonex-wiki"),
            "source_commit": "editable",
            "source_dirty": True,
        }
    return json.loads(target.read_text())


def verify_install(expected):
    import jonex_wiki

    info = build_info()
    for field in ("version", "source_commit", "dependency_lock_sha256","build_constraints_sha256"):
        if info.get(field) != expected.get(field):
            raise RuntimeError("WIKI_RELEASE_IDENTITY_MISMATCH: " + field)
    if info.get("source_dirty") is not False:
        raise RuntimeError("WIKI_RELEASE_DIRTY_SOURCE")
    package = Path(jonex_wiki.__file__).resolve()
    if not package.is_relative_to(Path(sysconfig.get_paths()["purelib"]).resolve()):
        raise RuntimeError("WIKI_RELEASE_SOURCE_SHADOWED: " + str(package))
    return {"package": str(package), **info}
