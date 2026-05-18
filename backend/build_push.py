#!/usr/bin/env python3
from __future__ import annotations

"""
游服镜像打包推送脚本

根据构建配置文件（默认同根目录 config.json 中 config_files.build，否则为 config/build/config.json）
为指定项目构建并推送镜像。根级 proxy 仅为 URL 字符串数组或带 url/index 的对象数组。

镜像完整名称规则：
  - 仓库地址：从 repositories[*] 中解析
      * 本地仓库：使用 "ip" 字段，例如 192.168.1.185:5005/dragon
      * 阿里云仓库：使用 "ALIYUN_URL" + "ALIYUN_PROJECT_NAME"，例如
          g123-jp-stg-registry...aliyuncs.com/dragon
      * 腾讯云仓库（国内个人版 CCR）：使用 "TENCENT_HOST" + "TENCENT_REPO"（或分设的 GS/CS）
          ccr.ccs.tencentyun.com/globalalert/gs
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
import hashlib
import hmac
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import timezone
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Optional

PUSH_RETRY_TIMES = 3
LOGIN_RETRY_TIMES = 3
API_RETRY_TIMES = 3


def runtime_root() -> Path:
    """与 main.run_server 一致：源码目录为仓库根；打包后为 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resolved_build_config_path() -> Path:
    """
    实际使用的构建配置路径：优先读 CONFIG_PATH 指向的根 config.json 里的 config_files.build，
    否则为 <runtime_root>/config/build/config.json。
    """
    default = runtime_root() / "config" / "build" / "config.json"
    raw = os.environ.get("CONFIG_PATH", "").strip()
    root_path = Path(raw) if raw else runtime_root() / "config.json"
    if not root_path.is_file():
        return default
    try:
        with root_path.open(encoding="utf-8") as f:
            root_cfg = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return default
    if not isinstance(root_cfg, dict):
        return default
    refs = root_cfg.get("config_files")
    if not isinstance(refs, dict):
        return default
    ref = refs.get("build")
    if not isinstance(ref, str) or not ref.strip():
        return default
    p = Path(ref.strip())
    if p.is_absolute():
        return p
    return (runtime_root() / p).resolve()


@dataclass
class ProxyChoice:
    index: int
    url: str
    total: int


def make_datetime_tag(project_key: str) -> str:
    """生成镜像 Tag，格式：{项目名}_{yyMMdd_HHmmss}。"""
    return f"{project_key}_{datetime.now().strftime('%y%m%d_%H%M%S')}"


def try_load_build_config() -> Optional[dict[str, Any]]:
    """供 HTTP 接口使用：读失败返回 None，不抛 SystemExit。"""
    path = _resolved_build_config_path()
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def load_build_config(path: Optional[Path] = None) -> dict[str, Any]:
    if path is None:
        path = _resolved_build_config_path()
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


def _str_field(repo_cfg: dict[str, Any], key: str) -> Optional[str]:
    raw = repo_cfg.get(key)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def is_tencent_repo(repo_cfg: dict[str, Any]) -> bool:
    """国内腾讯云个人版 CCR（ccr.ccs.tencentyun.com）。"""
    if not all(
        _str_field(repo_cfg, k)
        for k in ("TENCENT_HOST", "TENCENT_USERNAME", "TENCENT_REGION")
    ):
        return False
    if not (
        _str_field(repo_cfg, "TENCENT_REPO")
        or _str_field(repo_cfg, "TENCENT_GS_REPO")
        or _str_field(repo_cfg, "TENCENT_CS_REPO")
    ):
        return False
    has_password = bool(_str_field(repo_cfg, "TENCENT_PASSWORD"))
    has_ak = bool(_str_field(repo_cfg, "TENCENT_SECRET_ID") and _str_field(repo_cfg, "TENCENT_SECRET_KEY"))
    return has_password or has_ak


def is_registry_repo(repo_cfg: dict[str, Any]) -> bool:
    return is_aliyun_repo(repo_cfg) or is_tencent_repo(repo_cfg)


def registry_kind(repo_cfg: dict[str, Any]) -> Optional[str]:
    if is_aliyun_repo(repo_cfg):
        return "aliyun"
    if is_tencent_repo(repo_cfg):
        return "tencent"
    return None


def tencent_repo_path_for_component(repo_cfg: dict[str, Any], component: str) -> Optional[str]:
    """命名空间路径：优先 TENCENT_REPO；否则按组件读 TENCENT_GS_REPO / TENCENT_CS_REPO。"""
    common = _str_field(repo_cfg, "TENCENT_REPO")
    gs_repo = _str_field(repo_cfg, "TENCENT_GS_REPO") or common
    cs_repo = _str_field(repo_cfg, "TENCENT_CS_REPO") or common
    if component == "gs":
        return gs_repo
    if component in ("logic", "gate", "main"):
        return cs_repo or gs_repo
    return cs_repo or gs_repo


def _tc3_api_call(
    *,
    secret_id: str,
    secret_key: str,
    service: str,
    host: str,
    action: str,
    version: str,
    region: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """腾讯云 API 3.0（TC3-HMAC-SHA256），仅使用标准库。"""
    timestamp = int(datetime.now(timezone.utc).timestamp())
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    payload_str = json.dumps(payload, separators=(",", ":"))
    ct = "application/json; charset=utf-8"
    canonical_headers = f"content-type:{ct}\nhost:{host}\n"
    signed_headers = "content-type;host"
    hashed_request_payload = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
    canonical_request = (
        "POST\n/\n\n"
        f"{canonical_headers}\n{signed_headers}\n{hashed_request_payload}"
    )
    algorithm = "TC3-HMAC-SHA256"
    credential_scope = f"{date}/{service}/tc3_request"
    hashed_canonical_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = f"{algorithm}\n{timestamp}\n{credential_scope}\n{hashed_canonical_request}"

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    secret_date = _sign(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _sign(secret_date, service)
    secret_signing = _sign(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    authorization = (
        f"{algorithm} Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": ct,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Version": version,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Region": region,
    }
    req = urllib.request.Request(
        f"https://{host}",
        data=payload_str.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"请求失败: {e.reason}") from e
    if not isinstance(body, dict):
        raise RuntimeError("腾讯云 API 返回格式异常")
    if isinstance(body.get("Response"), dict) and body["Response"].get("Error"):
        err = body["Response"]["Error"]
        code = err.get("Code", "Unknown")
        msg = err.get("Message", str(err))
        raise RuntimeError(f"{code}: {msg}")
    return body


def _fetch_tencent_ccr_password(repo_cfg: dict[str, Any]) -> str:
    """通过个人版 CCR API 获取 docker login 密码（含网络波动重试）。"""
    secret_id = _str_field(repo_cfg, "TENCENT_SECRET_ID")
    secret_key = _str_field(repo_cfg, "TENCENT_SECRET_KEY")
    region = _str_field(repo_cfg, "TENCENT_REGION")
    if not secret_id or not secret_key or not region:
        raise RuntimeError("未配置 TENCENT_SECRET_ID / TENCENT_SECRET_KEY / TENCENT_REGION")

    last_err: Optional[Exception] = None
    for idx in range(API_RETRY_TIMES):
        print(f"[状态] 正在获取腾讯云 CCR 登录密码（尝试 {idx + 1}/{API_RETRY_TIMES}）")
        try:
            body = _tc3_api_call(
                secret_id=secret_id,
                secret_key=secret_key,
                service="ccr",
                host="ccr.tencentcloudapi.com",
                action="GetUserPassword",
                version="2018-06-27",
                region=region,
                payload={},
            )
            resp = body.get("Response") if isinstance(body.get("Response"), dict) else {}
            for key in ("Password", "password"):
                value = resp.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            last_err = RuntimeError("GetUserPassword 未返回有效密码")
        except Exception as exc:
            last_err = exc
            print(f"[WARN] 获取腾讯云密码失败: {exc}")
        if idx < API_RETRY_TIMES - 1:
            time.sleep(1)
    if last_err:
        raise RuntimeError(f"获取腾讯云 CCR 登录密码失败（已重试 {API_RETRY_TIMES} 次）: {last_err}") from last_err
    raise RuntimeError("获取腾讯云 CCR 登录密码失败")


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
    根配置 proxy 仅支持数组：
      - ["http://a:1080", "http://b:1080"] — 书写顺序即下拉顺序
      - [{ "url": "...", "index": 0 }, ...] — 按 index 升序；缺 index 时用数组下标
    """
    if not cfg:
        return []
    px = cfg.get("proxy")
    if not isinstance(px, list):
        return []
    rows: list[tuple[int, str, str]] = []
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


def _global_proxy_urls(cfg: Optional[dict[str, Any]]) -> list[str]:
    """纯字符串项按数组顺序；带 index 的对象项按 index 升序。"""
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
    """命令行等未传 WebSocket 参数时，是否走代理。"""
    if is_tencent_repo(repo_cfg):
        key = "tencent_proxy_enabled"
        default = False
    elif is_aliyun_repo(repo_cfg):
        key = "aliyun_proxy_enabled"
        default = False
    else:
        return False
    if key in repo_cfg:
        return bool(repo_cfg[key])
    if cfg and isinstance(cfg, dict):
        return bool(cfg.get(key, default))
    return default


def _proxy_base_index(repo_cfg: dict[str, Any], cfg: Optional[dict[str, Any]]) -> int:
    if is_tencent_repo(repo_cfg):
        raw = repo_cfg.get("tencent_proxy_index")
        if raw is None and cfg and isinstance(cfg, dict):
            raw = cfg.get("tencent_proxy_index", 0)
    else:
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
    if is_tencent_repo(repo_cfg):
        raw = repo_cfg.get("tencent_proxy_auto_switch")
        if raw is None and cfg and isinstance(cfg, dict):
            raw = cfg.get("tencent_proxy_auto_switch", False)
    else:
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


def resolve_global_proxy_choice(
    *,
    cfg: Optional[dict[str, Any]] = None,
    proxy_index_override: Optional[int] = None,
    client_use_proxy: Optional[bool] = None,
) -> Optional[ProxyChoice]:
    """
    构建阶段（docker build）使用：仅看根 proxy 列表与页面勾选，不依赖仓库配置。
    """
    urls = _global_proxy_urls(cfg)
    if not urls:
        return None
    if client_use_proxy is False:
        return None
    if client_use_proxy is None:
        return None
    base_index = int(proxy_index_override) if proxy_index_override is not None else 0
    total = len(urls)
    index = base_index % total
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


def _docker_registry_login(
    registry: str,
    username: str,
    password: str,
    *,
    env: Optional[dict[str, str]] = None,
    dry_run: bool = False,
) -> None:
    if dry_run:
        print(f"[状态] (dry-run) 跳过 Docker 登录: {registry}")
        return
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
            env=env,
        )
        login_res = subprocess.run(
            [
                "docker",
                "login",
                f"--username={username}",
                f"--password={password}",
                registry,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        if login_res.returncode == 0:
            print(f"[信息] 仓库登录成功: {registry}")
            return
        if login_attempt < LOGIN_RETRY_TIMES:
            print("[WARN] Docker 登录失败，准备重试...")
            time.sleep(login_attempt * 2)
    raise SystemExit(f"Docker 登录失败: {registry}")


def ensure_tencent_login(
    repo_cfg: dict[str, Any],
    *,
    cfg: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
    attempt: int = 1,
    proxy_index_override: Optional[int] = None,
    client_use_proxy: Optional[bool] = None,
) -> None:
    """
    国内腾讯云个人版 CCR 登录：
      1) 优先使用 TENCENT_PASSWORD
      2) 否则通过 TENCENT_SECRET_ID/KEY 调用 GetUserPassword 获取临时密码
      3) docker login 到 TENCENT_HOST
    """
    if not is_tencent_repo(repo_cfg):
        return
    proxy_choice = resolve_proxy_choice(
        repo_cfg,
        cfg=cfg,
        attempt=attempt,
        proxy_index_override=proxy_index_override,
        client_use_proxy=client_use_proxy,
    )
    registry = _str_field(repo_cfg, "TENCENT_HOST")
    username = _str_field(repo_cfg, "TENCENT_USERNAME")
    if not registry or not username:
        raise SystemExit("腾讯云仓库缺少 TENCENT_HOST 或 TENCENT_USERNAME")
    if dry_run:
        if proxy_choice:
            print(
                f"[状态] (dry-run) 跳过腾讯云登录: {registry} "
                f"(代理 {proxy_choice.index + 1}/{proxy_choice.total}: {proxy_choice.url})"
            )
        else:
            print(f"[状态] (dry-run) 跳过腾讯云登录: {registry}")
        return
    if proxy_choice:
        print(
            f"[信息] 已启用代理: {proxy_choice.index + 1}/{proxy_choice.total} => "
            f"{proxy_choice.url}"
        )
    cmd_env = build_proxy_env(dict(), proxy_choice)
    password = _str_field(repo_cfg, "TENCENT_PASSWORD")
    if not password:
        print("[状态] 正在通过腾讯云 API 获取 CCR 登录密码")
        password = _fetch_tencent_ccr_password(repo_cfg)
    print(f"[信息] 腾讯云登录准备: registry={registry}, username={username}")
    _docker_registry_login(registry, username, password, env=cmd_env, dry_run=False)


def ensure_registry_login(
    repo_cfg: dict[str, Any],
    *,
    cfg: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
    attempt: int = 1,
    proxy_index_override: Optional[int] = None,
    client_use_proxy: Optional[bool] = None,
) -> None:
    kind = registry_kind(repo_cfg)
    if kind == "aliyun":
        ensure_aliyun_login(
            repo_cfg,
            cfg=cfg,
            dry_run=dry_run,
            attempt=attempt,
            proxy_index_override=proxy_index_override,
            client_use_proxy=client_use_proxy,
        )
    elif kind == "tencent":
        ensure_tencent_login(
            repo_cfg,
            cfg=cfg,
            dry_run=dry_run,
            attempt=attempt,
            proxy_index_override=proxy_index_override,
            client_use_proxy=client_use_proxy,
        )


def ensure_registry_login_with_retry(
    repo_cfg: dict[str, Any],
    *,
    cfg: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
    attempt: int = 1,
    proxy_index_override: Optional[int] = None,
    client_use_proxy: Optional[bool] = None,
    times: int = LOGIN_RETRY_TIMES,
) -> None:
    """仓库登录（含整流程重试，应对网络波动）。"""
    if dry_run or not is_registry_repo(repo_cfg):
        ensure_registry_login(
            repo_cfg,
            cfg=cfg,
            dry_run=dry_run,
            attempt=attempt,
            proxy_index_override=proxy_index_override,
            client_use_proxy=client_use_proxy,
        )
        return
    last_exc: Optional[BaseException] = None
    for login_round in range(1, times + 1):
        try:
            ensure_registry_login(
                repo_cfg,
                cfg=cfg,
                dry_run=False,
                attempt=attempt,
                proxy_index_override=proxy_index_override,
                client_use_proxy=client_use_proxy,
            )
            return
        except (SystemExit, RuntimeError, OSError) as exc:
            last_exc = exc
            if login_round < times:
                print(
                    f"[WARN] 仓库登录失败，准备重试 "
                    f"({login_round}/{times}): {exc}"
                )
                time.sleep(login_round * 2)
    if isinstance(last_exc, SystemExit):
        raise last_exc
    if last_exc:
        raise SystemExit(f"仓库登录失败（已重试 {times} 次）: {last_exc}") from last_exc
    raise SystemExit(f"仓库登录失败（已重试 {times} 次）")


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

        tencent_host = _str_field(repo_cfg, "TENCENT_HOST") if is_tencent_repo(repo_cfg) else None

        if not prefix and not tencent_host:
            raise SystemExit(
                f"仓库未配置有效地址(ip、ALIYUN_URL/ALIYUN_PROJECT_NAME 或腾讯云 TENCENT_*): {repo_key}"
            )

        # 2) 解析组件镜像名
        for component, name in _iter_components(repo_cfg, image_name, svr_type=svr_type):
            if tencent_host:
                repo_path = tencent_repo_path_for_component(repo_cfg, component)
                if not repo_path:
                    raise SystemExit(
                        f"腾讯云仓库 {repo_key} 未配置组件 {component} 对应的 "
                        f"TENCENT_REPO 或 TENCENT_GS_REPO/TENCENT_CS_REPO"
                    )
                comp_prefix = f"{tencent_host.rstrip('/')}/{repo_path.strip().strip('/')}"
            else:
                comp_prefix = prefix or ""
            full_name = f"{comp_prefix.rstrip('/')}/{name}"
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
        if isinstance(repo_cfg, dict) and t.key not in logged_in_repos and is_registry_repo(repo_cfg):
            kind = registry_kind(repo_cfg)
            label = "阿里云" if kind == "aliyun" else "腾讯云"
            print(f"[状态] 正在登录{label}仓库 — {t.key}")
            ensure_registry_login_with_retry(repo_cfg, cfg=cfg, dry_run=dry_run, attempt=1)
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
                if isinstance(repo_cfg, dict) and is_registry_repo(repo_cfg)
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
                if isinstance(repo_cfg, dict) and is_registry_repo(repo_cfg):
                    kind = registry_kind(repo_cfg)
                    label = "阿里云" if kind == "aliyun" else "腾讯云"
                    print(f"[状态] 推送失败后重新登录{label}仓库 — {t.key}")
                    ensure_registry_login_with_retry(
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

