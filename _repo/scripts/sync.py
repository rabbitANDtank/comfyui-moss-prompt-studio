#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本仓库同步到 WorkBuddy 用户级 skills 目录，并逐文件校验。

用户级目录：~/.workbuddy/skills/<skill-name>/（默认读取 SKILL.md 的 name 字段）

用法
  python scripts/sync.py            # 同步 + 校验
  python scripts/sync.py --check    # 只校验，不改动
  python scripts/sync.py --dest D:/somewhere

只同步 skill 运行需要的东西：SKILL.md、scripts/*.py、references/*.md、examples/*.json。
docs/（截图与大图）、.git、__pycache__ 不参与同步。
"""
import argparse
import hashlib
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

INCLUDE = [
    ("SKILL.md", "*"),
    ("scripts", "*.py"),
    ("references", "*.md"),
    ("examples", "*.json"),
]


def skill_name():
    path = os.path.join(ROOT, "SKILL.md")
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("name:"):
                return line.split(":", 1)[1].strip().strip('"')
    sys.exit("SKILL.md 里没找到 name 字段")


def files():
    out = []
    for rel, pattern in INCLUDE:
        p = os.path.join(ROOT, rel)
        if os.path.isfile(p):
            out.append(rel.replace("\\", "/"))
            continue
        if not os.path.isdir(p):
            continue
        for name in sorted(os.listdir(p)):
            if name.startswith("__"):
                continue
            if pattern == "*.py" and name.endswith(".py"):
                out.append("%s/%s" % (rel, name))
            elif pattern == "*.md" and name.endswith(".md"):
                out.append("%s/%s" % (rel, name))
            elif pattern == "*.json" and name.endswith(".json"):
                out.append("%s/%s" % (rel, name))
    return out


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", help="目标目录（默认 ~/.workbuddy/skills/<name>）")
    ap.add_argument("--check", action="store_true", help="只校验，不写入")
    a = ap.parse_args()

    name = skill_name()
    dest = a.dest or os.path.join(os.path.expanduser("~"), ".workbuddy", "skills", name)
    print("仓库  :", ROOT)
    print("目标  :", dest)
    print("文件  :", len(files()), "个")

    if not a.check:
        for rel in files():
            src = os.path.join(ROOT, rel)
            dst = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

    bad = []
    for rel in files():
        src = os.path.join(ROOT, rel)
        dst = os.path.join(dest, rel)
        if not os.path.exists(dst):
            bad.append((rel, "missing"))
        elif digest(src) != digest(dst):
            bad.append((rel, "differs"))

    if bad:
        print("\n✗ 不一致：")
        for rel, why in bad:
            print("   %-34s %s" % (rel, why))
        sys.exit(1)
    print("\n✓ 同步完成，%d 个文件逐字节一致" % len(files()))


if __name__ == "__main__":
    main()
