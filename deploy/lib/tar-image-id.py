#!/usr/bin/env python3
"""读取 docker save 产物（tar / tar.gz / tar.zst）里记录的镜像信息。

用法: python3 tar-image-id.py <bundle.tar[.gz|.zst]>
输出: <镜像 ID>|<RepoTags 第一项，可能为空>

兼容两种布局：
- 经典 docker save：Config 是 "<id>.json"，带 RepoTags
- OCI 布局（新版 buildx 产出）：Config 是 "blobs/sha256/<hex>"
"""
import json
import subprocess
import sys
import tarfile


def open_stream(path):
    if path.endswith(".zst"):
        proc = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)
        return tarfile.open(fileobj=proc.stdout, mode="r|"), proc
    return tarfile.open(path, "r:*"), None


def normalize(cfg):
    """从 Config 路径里抽出 64 位镜像 ID。"""
    name = cfg.rsplit("/", 1)[-1]
    if name.endswith(".json"):
        name = name[: -len(".json")]
    return name


def main():
    if len(sys.argv) != 2:
        print("用法: python3 tar-image-id.py <bundle.tar[.gz|.zst]>", file=sys.stderr)
        return 2
    tf, proc = open_stream(sys.argv[1])
    try:
        manifest = json.load(tf.extractfile("manifest.json"))
    finally:
        tf.close()
        if proc is not None:
            proc.wait()
    if not manifest:
        print("manifest.json 为空", file=sys.stderr)
        return 1
    entry = manifest[0]
    tags = entry.get("RepoTags") or []
    print("%s|%s" % (normalize(entry["Config"]), tags[0] if tags else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
