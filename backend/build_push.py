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
  --dry-run          只打印将要执行的 docker 命令，不真正执行
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
BUILD_CONFIG_PATH = ROOT / "config" / "build" / "config.json"
PUSH_RETRY_TIMES = 3
LOGIN_RETRY_TIMES = 3


@dataclass
class ProxyChoice:
    index: int
    url: str
    total: int


def make_datetime_tag(project_key: str) -> str:
    """生成镜像 Tag，格式：{项目名}_{yyMMdd_HHmmss}。"""
    return f"{project_key}_{datetime.now().strftime('%y%m%d_%H%M%S')}"


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


def svn_up_cmd_from_config(cfg: dict[str, Any]) -> str:
    """
    根据 config.build 中的 svn 配置生成 svn up 命令。
    若未配置账号密码，则返回普通 svn up。
    """
    svn_cfg = cfg.get("svn")
    if not isinstance(svn_cfg, dict):
        return "svn up"
    username = svn_cfg.get("username")
    password = svn_cfg.get("password")
    if not isinstance(username, str) or not username.strip():
        return "svn up"
    if not isinstance(password, str) or not password.strip():
        return "svn up"
    u = shlex.quote(username.strip())
    p = shlex.quote(password.strip())
    return (
        "svn up "
        f"--username {u} "
        f"--password {p} "
        "--non-interactive "
        "--trust-server-cert"
    )


def is_aliyun_repo(repo_cfg: dict[str, Any]) -> bool:
    return all(
        isinstance(repo_cfg.get(k), str) and str(repo_cfg.get(k)).strip()
        for k in (
            "ALIYUN_PROJECT_NAME",
            "ALIYUN_ACCESS_KEY_ID",
            "ALIYUN_ACCESS_KEY_SECRET",
            "ALIYUN_REGION_ID",
            "ALIYUN_URL",
            "ALIYUN_KEY_STR",
        )
    )


def resolve_gs_restart_api_base(repo_cfg: dict[str, Any], cfg: dict[str, Any]) -> Optional[str]:
    b = repo_cfg.get("gs_restart_api_base")
    if isinstance(b, str) and b.strip():
        return b.strip().rstrip("/")
    root = cfg.get("gs_restart_api_base")
    if isinstance(root, str) and root.strip():
        return root.strip().rstrip("/")
    return None


def resolve_gs_restart_profile(repo_cfg: dict[str, Any]) -> Optional[str]:
    p = repo_cfg.get("gs_restart_profile")
    if isinstance(p, str) and p.strip():
        return p.strip()
    return None


def notify_gs_restart_api(
    api_base: str,
    profile: str,
    image_with_tag: str,
    *,
    dry_run: bool = False,
) -> None:
    """
    调用 185 游服 HTTP API，切换 GS 镜像并触发 apply（与 Invoke-RestMethod Put 等价）。
    """
    url = f"{api_base.rstrip('/')}/api/service/gs/image-version"
    payload = {"profile": profile, "image": image_with_tag, "apply": True}
    if dry_run:
        print(f"[状态] (dry-run) 跳过游服重启 API: PUT {url} body={json.dumps(payload, ensure_ascii=False)}")
        return
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"请求失败: {e.reason}") from e


def _extract_auth_token(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("AuthorizationToken", "authorizationToken"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            token = _extract_auth_token(value)
            if token:
                return token
    elif isinstance(payload, list):
        for item in payload:
            token = _extract_auth_token(item)
            if token:
                return token
    return None


def _global_proxy_rows(cfg: Optional[dict[str, Any]]) -> list[tuple[int, str, str]]:
    """
    读取根配置 proxy，返回未排序的 (sort_key, url, stable_id) 列表。

    支持三种写法（可混用迁移期，建议新配置用数组）：
      1) 数组：["http://a:1080", "http://b:1080"] — 顺序即优先级
      2) 数组对象：[{ "url": "...", "index": 0 }, ...] — 按 index 升序，缺 index 时用数组下标
      3) 旧版对象：{ "proxy1": { "url": "...", "index": 0 }, ... }
    """
    if not cfg:
        return []
    px = cfg.get("proxy")
    if px is None:
        return []
    rows: list[tuple[int, str, str]] = []

    if isinstance(px, list):
        for i, item in enumerate(px):
            if isinstance(item, str):
                u = item.strip()
                if not u:
                    continue
                rows.append((i, u, f"#{i}"))
            elif isinstance(item, dict):
                url = item.get("url")
                if not isinstance(url, str) or not url.strip():
                    continue
                u = url.strip()
                idx_raw = item.get("index")
                if idx_raw is None:
                    sort_key = i
                else:
                    try:
                        sort_key = int(idx_raw)
                    except (TypeError, ValueError):
                        sort_key = i
                sid = item.get("id") or item.get("name") or f"#{i}"
                rows.append((sort_key, u, str(sid)))
        return rows

    if isinstance(px, dict):
        for name, node in px.items():
            if not isinstance(node, dict):
                continue
            url = node.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            idx_raw = node.get("index")
            if idx_raw is None:
                sort_key = 1_000_000
            else:
                try:
                    sort_key = int(idx_raw)
                except (TypeError, ValueError):
                    sort_key = 1_000_000
            rows.append((sort_key, url.strip(), str(name)))
        return rows

    return []


def _global_proxy_urls(cfg: Optional[dict[str, Any]]) -> list[str]:
    """
    排序规则：有 index 的按 index 升序；旧版对象里缺 index 的排在后面；纯字符串数组按书写顺序。
    """
    rows = _global_proxy_rows(cfg)
    if not rows:
        return []
    rows.sort(key=lambda x: x[0])
    return [u for _, u, _ in rows]


def list_global_proxy_options(cfg: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    供前端展示：与 _global_proxy_urls 同序；list_index 为下拉选用的下标；key 仅作内部区分可忽略展示。
    """
    rows = _global_proxy_rows(cfg)
    if not rows:
        return []
    rows.sort(key=lambda x: x[0])
    return [
        {"key": k, "url": u, "list_index": i}
        for i, (_sk, u, k) in enumerate(rows)
    ]


def _proxy_enabled(repo_cfg: dict[str, Any], cfg: Optional[dict[str, Any]]) -> bool:
    """命令行等未传 WebSocket 参数时，是否走代理（看仓库/根配置 aliyun_proxy_enabled）。"""
    if "aliyun_proxy_enabled" in repo_cfg:
        return bool(repo_cfg["aliyun_proxy_enabled"])
    if cfg and isinstance(cfg, dict):
        return bool(cfg.get("aliyun_proxy_enabled", False))
    return False


def _proxy_base_index(repo_cfg: dict[str, Any], cfg: Optional[dict[str, Any]]) -> int:
    raw = repo_cfg.get("aliyun_proxy_index")
    if raw is None and cfg and isinstance(cfg, dict):
        raw = cfg.get("aliyun_proxy_index", 0)
    if raw is None:
        raw = 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _proxy_auto_switch(repo_cfg: dict[str, Any], cfg: Optional[dict[str, Any]]) -> bool:
    raw = repo_cfg.get("aliyun_proxy_auto_switch")
    if raw is None and cfg and isinstance(cfg, dict):
        raw = cfg.get("aliyun_proxy_auto_switch", False)
    if raw is None:
        return False
    return bool(raw)


def resolve_proxy_choice(
    repo_cfg: dict[str, Any],
    *,
    cfg: Optional[dict[str, Any]] = None,
    attempt: int = 1,
    proxy_index_override: Optional[int] = None,
    client_use_proxy: Optional[bool] = None,
) -> Optional[ProxyChoice]:
    urls = _global_proxy_urls(cfg)
    if not urls:
        return None
    if client_use_proxy is False:
        return None
    if client_use_proxy is None:
        if not _proxy_enabled(repo_cfg, cfg):
            return None
    if proxy_index_override is not None:
        base_index = int(proxy_index_override)
    else:
        base_index = _proxy_base_index(repo_cfg, cfg)
    total = len(urls)
    auto_switch = _proxy_auto_switch(repo_cfg, cfg)
    offset = max(0, attempt - 1) if auto_switch else 0
    index = (base_index + offset) % total
    return ProxyChoice(index=index, url=urls[index], total=total)


def build_proxy_env(
    base_env: Optional[dict[str, str]],
    choice: Optional[ProxyChoice],
) -> Optional[dict[str, str]]:
    if choice is None:
        return None
    env = dict(os.environ)
    if base_env:
        env.update(base_env)
    proxy = choice.url
    no_proxy = str(env.get("NO_PROXY", env.get("no_proxy", ""))).strip()
    env["HTTP_PROXY"] = proxy
    env["HTTPS_PROXY"] = proxy
    env["ALL_PROXY"] = proxy
    env["http_proxy"] = proxy
    env["https_proxy"] = proxy
    env["all_proxy"] = proxy
    if no_proxy:
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
    return env


def ensure_aliyun_login(
    repo_cfg: dict[str, Any],
    *,
    cfg: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
    attempt: int = 1,
    proxy_index_override: Optional[int] = None,
    client_use_proxy: Optional[bool] = None,
) -> None:
    """
    参考旧工具流程：
      1) aliyun configure set
      2) aliyun cr GetAuthorizationToken 拿临时密码
      3) docker login 到 ALIYUN_URL
    """
    if not is_aliyun_repo(repo_cfg):
        return
    proxy_choice = resolve_proxy_choice(
        repo_cfg,
        cfg=cfg,
        attempt=attempt,
        proxy_index_override=proxy_index_override,
        client_use_proxy=client_use_proxy,
    )
    if dry_run:
        if proxy_choice:
            print(
                f"[状态] (dry-run) 跳过阿里云登录: {repo_cfg['ALIYUN_URL']} "
                f"(代理 {proxy_choice.index + 1}/{proxy_choice.total}: {proxy_choice.url})"
            )
        else:
            print(f"[状态] (dry-run) 跳过阿里云登录: {repo_cfg['ALIYUN_URL']}")
        return
    if not shutil.which("aliyun"):
        raise SystemExit("未检测到 aliyun 命令，请先安装阿里云 CLI 并确保在 PATH 中")

    profile = str(repo_cfg["ALIYUN_PROJECT_NAME"]).strip()
    region = str(repo_cfg["ALIYUN_REGION_ID"]).strip()
    ak = str(repo_cfg["ALIYUN_ACCESS_KEY_ID"]).strip()
    sk = str(repo_cfg["ALIYUN_ACCESS_KEY_SECRET"]).strip()
    instance_id = str(repo_cfg["ALIYUN_KEY_STR"]).strip()
    registry = str(repo_cfg["ALIYUN_URL"]).strip()

    if proxy_choice:
        print(
            f"[信息] 已启用代理: {proxy_choice.index + 1}/{proxy_choice.total} => "
            f"{proxy_choice.url}"
        )
    print(f"[信息] 阿里云登录准备: profile={profile}, region={region}, registry={registry}")
    cmd_env = build_proxy_env(dict(), proxy_choice)

    cfg_cmd = [
        "aliyun",
        "configure",
        "set",
        "--profile",
        profile,
        "--mode",
        "AK",
        "--region",
        region,
        "--access-key-id",
        ak,
        "--access-key-secret",
        sk,
    ]
    print("[状态] 正在配置阿里云 CLI")
    subprocess.run(
        cfg_cmd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=cmd_env,
    )

    token: Optional[str] = None
    for idx in range(3):
        print(f"[状态] 正在获取阿里云授权 token（尝试 {idx + 1}/3）")
        token_cmd = [
            "aliyun",
            "cr",
            "GetAuthorizationToken",
            "--region",
            region,
            "--InstanceId",
            instance_id,
            "--version",
            "2018-12-01",
            "--force",
        ]
        result = subprocess.run(
            token_cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=cmd_env,
        )
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
                token = _extract_auth_token(payload)
            except json.JSONDecodeError:
                token = None
        if token:
            break
        time.sleep(1)
    if not token:
        raise SystemExit("获取阿里云镜像仓库授权 token 失败")

    for login_attempt in range(1, LOGIN_RETRY_TIMES + 1):
        print(
            f"[状态] 正在登录 Docker 仓库: {registry}（尝试 {login_attempt}/{LOGIN_RETRY_TIMES}）"
        )
        subprocess.run(
            ["docker", "logout", registry],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=cmd_env,
        )
        login_res = subprocess.run(
            ["docker", "login", "--username=cr_temp_user", f"--password={token}", registry],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=cmd_env,
        )
        if login_res.returncode == 0:
            print(f"[信息] 阿里云仓库登录成功: {registry}")
            return
        if login_attempt < LOGIN_RETRY_TIMES:
            print("[WARN] Docker 登录失败，准备重试...")
            time.sleep(login_attempt * 2)
    raise SystemExit(f"Docker 登录失败: {registry}")


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


def gs_restart_should_run(
    repo_cfg: Optional[dict[str, Any]],
    cfg: dict[str, Any],
    target: RepoTarget,
) -> bool:
    if target.component != "gs":
        return False
    if not isinstance(repo_cfg, dict):
        return False
    if resolve_gs_restart_profile(repo_cfg) is None:
        return False
    if resolve_gs_restart_api_base(repo_cfg, cfg) is None:
        return False
    return True


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
    cfg: dict[str, Any],
    dry_run: bool,
) -> None:
    # 对齐人工流程：
    # docker exec -it {项目名} /bin/bash
    # cd {项目名}
    # svn up
    # python script/py/init.py
    # ./vs.sh
    # ./BuildRelease.sh
    svn_up_cmd = svn_up_cmd_from_config(cfg)
    compile_steps = f"{svn_up_cmd} && python script/py/init.py && ./vs.sh && ./BuildRelease.sh"
    cmd = [
        "docker",
        "exec",
        project_key,
        "/bin/bash",
        "-lc",
        f"cd {project_key} && {compile_steps}",
    ]
    print("[状态] 正在编译（容器内）")
    run_cmd(cmd, dry_run=dry_run)


def build_and_push(
    project_key: str,
    *,
    only_repo: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    cfg = load_build_config()
    tag = make_datetime_tag(project_key)
    targets = build_repo_targets(project_key, cfg, only_repo=only_repo)
    repos_cfg = cfg.get("repositories") if isinstance(cfg.get("repositories"), dict) else {}
    release_dir = release_dir_for_project(project_key)
    if not release_dir.is_dir():
        raise SystemExit(f"构建目录不存在: {release_dir}")

    print(f"[信息] 项目: {project_key}")
    print(f"[信息] 发布目录: {release_dir}")
    print(f"[信息] 本次 Tag: {tag}")
    print(f"[信息] 目标仓库: {', '.join(sorted({t.key for t in targets}))}")

    compile_project_in_container(
        project_key,
        cfg=cfg,
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

    logged_in_repos: set[str] = set()
    for t in targets:
        repo_cfg = repos_cfg.get(t.key) if isinstance(repos_cfg, dict) else None
        if isinstance(repo_cfg, dict) and t.key not in logged_in_repos and is_aliyun_repo(repo_cfg):
            print(f"[状态] 正在登录阿里云仓库 — {t.key}")
            ensure_aliyun_login(repo_cfg, cfg=cfg, dry_run=dry_run, attempt=1)
            logged_in_repos.add(t.key)

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
        push_cmd = ["docker", "push", image_with_tag]
        if dry_run:
            run_cmd(push_cmd, dry_run=True)
            continue

        push_ok = False
        for attempt in range(1, PUSH_RETRY_TIMES + 1):
            print(f"[状态] 推送尝试 {attempt}/{PUSH_RETRY_TIMES} — {image_with_tag}")
            proxy_choice = (
                resolve_proxy_choice(repo_cfg, cfg=cfg, attempt=attempt)
                if isinstance(repo_cfg, dict) and is_aliyun_repo(repo_cfg)
                else None
            )
            if proxy_choice:
                print(
                    f"[信息] 本次推送代理: {proxy_choice.index + 1}/{proxy_choice.total} => "
                    f"{proxy_choice.url}"
                )
            res = subprocess.run(
                push_cmd,
                check=False,
                env=build_proxy_env(dict(), proxy_choice),
            )
            if res.returncode == 0:
                push_ok = True
                break
            if attempt < PUSH_RETRY_TIMES:
                print(f"[WARN] docker push 失败，退出码: {res.returncode}，准备重试...")
                if isinstance(repo_cfg, dict) and is_aliyun_repo(repo_cfg):
                    print(f"[状态] 推送失败后重新登录阿里云仓库 — {t.key}")
                    ensure_aliyun_login(
                        repo_cfg,
                        cfg=cfg,
                        dry_run=False,
                        attempt=attempt + 1,
                    )
                time.sleep(attempt * 2)
        if not push_ok:
            raise SystemExit(f"docker push 最终失败: {image_with_tag}")

        if gs_restart_should_run(
            repo_cfg if isinstance(repo_cfg, dict) else None,
            cfg,
            t,
        ):
            base = resolve_gs_restart_api_base(repo_cfg, cfg)
            profile = resolve_gs_restart_profile(repo_cfg)
            if base and profile:
                print(f"[状态] 正在通知游服切换 GS 镜像 — {t.key}, profile={profile}")
                try:
                    notify_gs_restart_api(base, profile, image_with_tag, dry_run=dry_run)
                    print(f"[信息] 游服重启 API 已完成 — profile={profile}")
                except Exception as exc:
                    print(f"[WARN] 游服重启 API 失败（镜像已成功推送）: {exc}")

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

