#!/usr/bin/env python3
from __future__ import annotations

"""
游服镜像打包推送脚本

根据 config/build/config.json 中的配置，为指定项目构建并推送镜像。

镜像完整名称规则：
  - 仓库地址：从 repositories[*] 中解析
      * 本地仓库：使用 "ip" 字段，例如 192.168.1.185:5005/dragon
      * 阿里云仓库：使用 "ALIYUN_URL" + "ALIYUN_PROJECT_NAME"，例如
          g123-jp-stg-registry...aliyuncs.com/dragon
  - 镜像名部分：
      * 优先使用仓库配置中的组件名：
          - gs_image_name
          - logic_image_name
          - gate_image_name
      * 否则退回项目级的 image_name
  - Tag 规则：
      * 使用当前日期时间：YYYYMMDDHHMMSS

最终镜像示例：
  192.168.1.185:5005/dragon/lnp_trunk:20260331153045
  g123-jp-stg-registry.ap-northeast-1.cr.aliyuncs.com/dragon/maiddragon:20260331153045

使用方式（在项目根目录）：
  python -m backend.build_push --project lnp_trunk

可选参数：
  --only-repo NAME   只针对某个仓库键执行（例如 185_lnp_trunk）
  --build-script     容器内编译脚本（默认 ./run_build.sh）
  --dry-run          只打印将要执行的 docker 命令，不真正执行
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
BUILD_CONFIG_PATH = ROOT / "config" / "build" / "config.json"


def make_datetime_tag() -> str:
    """生成基于当前日期时间的镜像 Tag，格式：YYYYMMDDHHMMSS。"""
    return datetime.now().strftime("%Y%m%d%H%M%S")


def load_build_config(path: Path = BUILD_CONFIG_PATH) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"配置文件不存在: {path}")
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("配置文件根节点必须是对象")
        return data
    except Exception as e:
        raise SystemExit(f"读取配置失败: {path} ({type(e).__name__}: {e})")


@dataclass
class RepoTarget:
    key: str
    full_name: str  # 不含 tag 的完整镜像名，例如 192.168.1.185:5005/dragon/lnp_trunk
    component: str  # 组件类型：gs / logic / gate / main


def _iter_components(
    repo_cfg: dict[str, Any],
    project_image_name: str,
    *,
    svr_type: Optional[str] = None,
) -> Iterable[tuple[str, str]]:
    """
    返回 (component, image_name) 列表：
      - 如果存在 gs/logic/gate 对应的 image_name，则分别生成多个目标
      - 否则只返回一个 main 组件，使用项目级 image_name
    """
    key_to_comp = {
        "gs_image_name": "gs",
        "logic_image_name": "logic",
        "gate_image_name": "gate",
    }
    mapping = {k: repo_cfg.get(k) for k in key_to_comp.keys()}

    # 配置了 svr_type 时，三选一：仅使用指定类型
    if svr_type:
        if svr_type not in key_to_comp:
            raise SystemExit(f"svr_type 配置错误: {svr_type}（仅支持 gs_image_name/logic_image_name/gate_image_name）")
        name = mapping.get(svr_type)
        if not name:
            raise SystemExit(f"仓库缺少 {svr_type} 配置")
        yield key_to_comp[svr_type], str(name)
        return

    # 未配置 svr_type 时兼容旧行为：如果仓库里配了多个组件则都推，否则走项目级 image_name
    has_any_component = any(bool(v) for v in mapping.values())
    if has_any_component:
        for key, comp in key_to_comp.items():
            name = mapping.get(key)
            if not name:
                continue
            yield comp, str(name)
        return

    yield "main", project_image_name


def build_repo_targets(
    project_key: str,
    cfg: dict[str, Any],
    *,
    only_repo: Optional[str] = None,
) -> List[RepoTarget]:
    projects = cfg.get("projects") or {}
    if not isinstance(projects, dict) or project_key not in projects:
        raise SystemExit(f"未知项目: {project_key}")

    project_cfg = projects[project_key]
    if not isinstance(project_cfg, dict):
        raise SystemExit(f"项目配置格式错误: {project_key}")

    image_name = str(project_cfg.get("image_name") or project_key)
    svr_type_raw = project_cfg.get("svr_type")
    svr_type = str(svr_type_raw).strip() if isinstance(svr_type_raw, str) and svr_type_raw.strip() else None
    repo_keys = project_cfg.get("repositories") or []
    if not isinstance(repo_keys, list) or not repo_keys:
        raise SystemExit(f"项目未配置 repositories: {project_key}")

    repos = cfg.get("repositories") or {}
    if not isinstance(repos, dict):
        raise SystemExit("repositories 配置格式错误")

    targets: List[RepoTarget] = []

    for repo_key in repo_keys:
        if not isinstance(repo_key, str):
            continue
        if only_repo and repo_key != only_repo:
            continue
        repo_cfg = repos.get(repo_key)
        if not isinstance(repo_cfg, dict):
            raise SystemExit(f"仓库配置不存在或格式错误: {repo_key}")

        # 1) 解析仓库地址前缀
        prefix: Optional[str] = None

        ip = repo_cfg.get("ip")
        if isinstance(ip, str) and ip.strip():
            prefix = ip.strip()

        if not prefix:
            # 尝试按阿里云仓库解析
            aliyun_url = repo_cfg.get("ALIYUN_URL")
            aliyun_project = repo_cfg.get("ALIYUN_PROJECT_NAME")
            if isinstance(aliyun_url, str) and isinstance(aliyun_project, str):
                prefix = f"{aliyun_url.strip().rstrip('/')}/{aliyun_project.strip().strip('/')}"

        if not prefix:
            raise SystemExit(f"仓库未配置有效地址(ip 或 ALIYUN_URL/ALIYUN_PROJECT_NAME): {repo_key}")

        # 2) 解析组件镜像名
        for component, name in _iter_components(repo_cfg, image_name, svr_type=svr_type):
            full_name = f"{prefix.rstrip('/')}/{name}"
            targets.append(RepoTarget(key=repo_key, full_name=full_name, component=component))

    if not targets:
        raise SystemExit("未解析出任何镜像目标，请检查配置。")

    return targets


def run_cmd(cmd: list[str], *, dry_run: bool, cwd: Optional[Path] = None) -> None:
    display = " ".join(cmd)
    if cwd:
        print(f"$ (cd {cwd} && {display})")
    else:
        print(f"$ {display}")
    if dry_run:
        return
    subprocess.run(cmd, check=True, cwd=str(cwd) if cwd else None)


def release_dir_for_project(project_key: str) -> Path:
    return Path.home() / f"{project_key}_home" / project_key / "x64_Env" / "LinuxRelease"


def compile_project_in_container(
    project_key: str,
    *,
    build_script: str,
    dry_run: bool,
) -> None:
    # 对齐人工流程：
    # docker exec -it {项目名} /bin/bash
    # cd {项目名}
    # ./run_build.sh
    cmd = [
        "docker",
        "exec",
        project_key,
        "/bin/bash",
        "-lc",
        f"cd {project_key} && {build_script}",
    ]
    print("[状态] 正在编译（容器内）")
    run_cmd(cmd, dry_run=dry_run)


def build_and_push(
    project_key: str,
    *,
    only_repo: Optional[str] = None,
    build_script: str = "./run_build.sh",
    dry_run: bool = False,
) -> None:
    cfg = load_build_config()
    tag = make_datetime_tag()
    targets = build_repo_targets(project_key, cfg, only_repo=only_repo)
    release_dir = release_dir_for_project(project_key)
    if not release_dir.is_dir():
        raise SystemExit(f"构建目录不存在: {release_dir}")

    print(f"[信息] 项目: {project_key}")
    print(f"[信息] 发布目录: {release_dir}")
    print(f"[信息] 本次 Tag: {tag}")
    print(f"[信息] 目标仓库: {', '.join(sorted({t.key for t in targets}))}")

    compile_project_in_container(
        project_key,
        build_script=build_script,
        dry_run=dry_run,
    )

    local_image = f"{project_key}:{tag}"
    print(f"[状态] 正在打包本地镜像 — {local_image}")
    run_cmd(
        [
            "docker",
            "build",
            "-t",
            local_image,
            ".",
        ],
        dry_run=dry_run,
        cwd=release_dir,
    )

    for t in targets:
        image_with_tag = f"{t.full_name}:{tag}"
        print(f"[状态] 正在打标签 — 仓库: {t.key}, 组件: {t.component}")
        run_cmd(
            [
                "docker",
                "tag",
                local_image,
                image_with_tag,
            ],
            dry_run=dry_run,
        )

        print(f"[状态] 正在推送镜像 — {image_with_tag}")
        run_cmd(
            [
                "docker",
                "push",
                image_with_tag,
            ],
            dry_run=dry_run,
        )

    print("[状态] 所有镜像处理完成")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="游服镜像打包与推送脚本")
    p.add_argument(
        "--project",
        required=True,
        help="项目键（config/build/config.json 中的 projects.*）",
    )
    p.add_argument(
        "--only-repo",
        default=None,
        help="只处理指定仓库键（可选），例如 185_lnp_trunk",
    )
    p.add_argument(
        "--build-script",
        default="./run_build.sh",
        help="容器内编译脚本，默认 ./run_build.sh",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印 docker 命令，不实际执行",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        build_and_push(
            project_key=args.project,
            only_repo=args.only_repo,
            build_script=args.build_script,
            dry_run=args.dry_run,
        )
        return 0
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] 命令执行失败，退出码: {e.returncode}")
        return e.returncode or 1
    except SystemExit as e:
        print(f"[ERROR] {e}")
        return int(e.code) if isinstance(e.code, int) else 1
    except Exception as e:
        print(f"[ERROR] 未预期异常: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

