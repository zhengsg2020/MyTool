from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

try:
    # 作为包导入时（例如 backend.main）
    from . import build_push  # type: ignore
except ImportError:  # pragma: no cover
    # 作为脚本模块导入时（例如直接 main）
    import build_push  # type: ignore


def runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def frontend_dist_dir() -> Path:
    override = os.environ.get("FRONTEND_DIST")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / "frontend" / "dist"
    return Path(__file__).resolve().parent.parent / "frontend" / "dist"


CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", runtime_root() / "config.json"))
DEFAULT_BUILD_CONFIG_PATH = runtime_root() / "config" / "build" / "config.json"
DEFAULT_SITES_CONFIG_PATH = runtime_root() / "config" / "sites" / "sites.json"

app = FastAPI(title="Docker Build Publisher")

_cors = os.environ.get("CORS_ORIGINS", "").strip()
if _cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _cors.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # 本机 + 常见内网网段（便于局域网用 IP 打开页面 / 另一台机子跑 npm run dev 连这台 API）
    _cors_lan = (
        r"^https?://("
        r"localhost|127\.0\.0\.1|"
        r"192\.168\.\d{1,3}\.\d{1,3}|"
        r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        r"172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}"
        r")(:\d+)?$"
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=_cors_lan,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        cfg = {}

    build_cfg_path, sites_cfg_path = get_config_file_paths(cfg)

    if "common_template" not in cfg or "projects" not in cfg:
        build_cfg = load_json_file(build_cfg_path, {})
        if not isinstance(build_cfg, dict):
            build_cfg = {}
        if "common_template" not in cfg:
            cfg["common_template"] = build_cfg.get("common_template", {})
        if "projects" not in cfg:
            cfg["projects"] = build_cfg.get("projects", [])

    if "sites" not in cfg:
        sites_cfg = load_json_file(sites_cfg_path, [])
        cfg["sites"] = sites_cfg if isinstance(sites_cfg, list) else []

    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)
        f.write("\n")
    os.replace(tmp_path, CONFIG_PATH)


def load_json_file(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return default


def save_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.write("\n")
    os.replace(tmp_path, path)


def _resolve_config_ref(value: Any, default_path: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        return default_path
    ref = Path(value.strip())
    if ref.is_absolute():
        return ref
    return runtime_root() / ref


def get_config_file_paths(cfg: dict[str, Any]) -> tuple[Path, Path]:
    refs = cfg.get("config_files")
    if not isinstance(refs, dict):
        refs = {}
    build_path = _resolve_config_ref(refs.get("build"), DEFAULT_BUILD_CONFIG_PATH)
    sites_path = _resolve_config_ref(refs.get("sites"), DEFAULT_SITES_CONFIG_PATH)
    return build_path, sites_path


def load_sites() -> list[dict[str, Any]]:
    cfg = load_json_file(CONFIG_PATH, {})
    if not isinstance(cfg, dict):
        cfg = {}
    _, sites_path = get_config_file_paths(cfg)
    sites = load_json_file(sites_path, [])
    if isinstance(sites, list):
        return [s for s in sites if isinstance(s, dict)]
    return []


def save_sites(sites: list[dict[str, Any]]) -> None:
    cfg = load_json_file(CONFIG_PATH, {})
    if not isinstance(cfg, dict):
        cfg = {}
    _, sites_path = get_config_file_paths(cfg)
    save_json_file(sites_path, sites)


class SiteCreate(BaseModel):
    name: str = ""
    url: str = Field(min_length=1)
    username: str = ""
    password: str = ""

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("url")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("字段不能为空")
        return text

    @field_validator("username", "password")
    @classmethod
    def trim_optional_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("url 必须是合法的 http/https 地址")
        return value


class SiteOut(BaseModel):
    id: str
    name: str = ""
    url: str
    username: str
    password: str
    created_at: str


async def stream_command(
    websocket: WebSocket,
    cmd: list[str],
    *,
    cwd: str | None = None,
    prefix: str = "",
) -> int:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode(errors="replace")
        if prefix:
            text = f"{prefix}{text}"
        await websocket.send_text(text.rstrip("\n\r"))
    await proc.wait()
    return proc.returncode or 0


@app.get("/api/projects")
async def list_projects():
    # 返回项目 key 列表，保持与旧前端兼容
    cfg = build_push.load_build_config()
    projects = cfg.get("projects") or {}
    if not isinstance(projects, dict):
        return []
    return list(projects.keys())


@app.get("/api/projects/{project_name}/repositories")
async def list_project_repositories(project_name: str):
    """
    返回某项目可用的仓库键列表，供前端勾选。
    只返回诸如 "185_lnp_trunk"、"dragon_stg" 这样的 key，不暴露 ip 等信息。
    """
    cfg = build_push.load_build_config()
    projects = cfg.get("projects") or {}
    if not isinstance(projects, dict) or project_name not in projects:
        raise HTTPException(status_code=404, detail="未知项目")
    project = projects[project_name]
    if not isinstance(project, dict):
        raise HTTPException(status_code=500, detail="项目配置格式错误")
    repo_keys = project.get("repositories") or []
    if not isinstance(repo_keys, list):
        raise HTTPException(status_code=500, detail="项目 repositories 配置错误")

    result: list[str] = []
    for key in repo_keys:
        if isinstance(key, str) and key:
            result.append(key)
    return result


@app.get("/api/sites", response_model=list[SiteOut])
async def list_sites():
    raw_sites = load_sites()
    sites: list[SiteOut] = []
    for item in raw_sites:
        if isinstance(item, dict):
            try:
                sites.append(SiteOut(**item))
            except Exception:
                continue
    return sorted(sites, key=lambda x: x.created_at, reverse=True)


@app.post("/api/sites", response_model=SiteOut)
async def create_site(payload: SiteCreate):
    sites = load_sites()
    site = SiteOut(
        id=str(uuid.uuid4()),
        name=payload.name,
        url=payload.url,
        username=payload.username,
        password=payload.password,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    sites.append(site.model_dump())
    save_sites(sites)
    return site


@app.delete("/api/sites/{site_id}")
async def delete_site(site_id: str):
    sites = load_sites()
    if not sites:
        raise HTTPException(status_code=404, detail="site not found")
    new_sites = [s for s in sites if not (isinstance(s, dict) and s.get("id") == site_id)]
    if len(new_sites) == len(sites):
        raise HTTPException(status_code=404, detail="site not found")
    save_sites(new_sites)
    return {"ok": True}


@app.websocket("/ws/build/{project_name}")
async def ws_build(websocket: WebSocket, project_name: str):
    await websocket.accept()
    try:
        # 解析前端勾选的仓库键（逗号分隔）
        raw = websocket.query_params.get("repos", "")
        selected_repos = [x for x in raw.split(",") if x.strip()]
        selected_set = set(selected_repos)

        cfg = build_push.load_build_config()
        projects = cfg.get("projects") or {}
        if not isinstance(projects, dict) or project_name not in projects:
            await websocket.send_text(f"[ERROR] 未知项目: {project_name}")
            await websocket.send_text("FAILED")
            return

        # 计算本次所有目标仓库 / 组件
        all_targets = build_push.build_repo_targets(project_name, cfg)

        if selected_set:
            targets = [t for t in all_targets if t.key in selected_set]
        else:
            targets = all_targets

        if not targets:
            await websocket.send_text("[ERROR] 未选择任何有效仓库")
            await websocket.send_text("FAILED")
            return

        # 构建目录：~/{项目名}_home/{项目名}/x64_Env/LinuxRelease
        release_dir = build_push.release_dir_for_project(project_name)
        await websocket.send_text(f"[状态] 准备中 — 项目: {project_name}")
        await websocket.send_text(f"[信息] 发布目录: {release_dir}")
        await websocket.send_text(f"[信息] 目标仓库: {', '.join(sorted({t.key for t in targets}))}")

        if not release_dir.is_dir():
            await websocket.send_text(f"[ERROR] 构建目录不存在: {release_dir}")
            await websocket.send_text("FAILED")
            return

        await websocket.send_text("[状态] 正在编译（容器内）")
        compile_cmd = [
            "docker",
            "exec",
            project_name,
            "/bin/bash",
            "-lc",
            (
                f"cd {project_name} && "
                f"{build_push.svn_up_cmd_from_config(cfg)} && "
                "python script/py/init.py && "
                "./vs.sh && "
                "./BuildRelease.sh"
            ),
        ]
        await websocket.send_text(f"$ {' '.join(compile_cmd)}")
        rc = await stream_command(websocket, compile_cmd)
        if rc != 0:
            await websocket.send_text(f"[ERROR] 编译失败，退出码: {rc}")
            await websocket.send_text("FAILED")
            return

        tag = build_push.make_datetime_tag(project_name)
        await websocket.send_text("[状态] 正在生成版本号")
        await websocket.send_text(f"[信息] 本次 Tag: {tag}")

        local_image = f"{project_name}:{tag}"
        await websocket.send_text(f"[状态] 正在打包本地镜像 — {local_image}")
        local_build_cmd = ["docker", "build", "-t", local_image, "."]
        await websocket.send_text(f"$ cd {release_dir} && {' '.join(local_build_cmd)}")
        rc = await stream_command(websocket, local_build_cmd, cwd=str(release_dir))
        if rc != 0:
            await websocket.send_text(f"[ERROR] 本地镜像打包失败，退出码: {rc}")
            await websocket.send_text("FAILED")
            return

        for t in targets:
            image_with_tag = f"{t.full_name}:{tag}"
            await websocket.send_text(
                f"[状态] 正在打标签 — 仓库: {t.key}, 组件: {t.component}"
            )
            tag_cmd = [
                "docker",
                "tag",
                local_image,
                image_with_tag,
            ]
            await websocket.send_text(f"$ {' '.join(tag_cmd)}")
            rc = await stream_command(websocket, tag_cmd)
            if rc != 0:
                await websocket.send_text(
                    f"[ERROR] docker tag 失败（仓库 {t.key} 组件 {t.component}），退出码: {rc}"
                )
                await websocket.send_text("FAILED")
                return

            await websocket.send_text(f"[状态] 正在推送镜像 — {image_with_tag}")
            push_cmd = ["docker", "push", image_with_tag]
            await websocket.send_text(f"$ {' '.join(push_cmd)}")
            rc = await stream_command(websocket, push_cmd)
            if rc != 0:
                await websocket.send_text(
                    f"[ERROR] docker push 失败（仓库 {t.key} 组件 {t.component}），退出码: {rc}"
                )
                await websocket.send_text("FAILED")
                return

        await websocket.send_text("[状态] 推送成功 — 所有选中仓库")
        await websocket.send_text("SUCCESS")
    except WebSocketDisconnect:
        raise
    except Exception as e:
        await websocket.send_text(f"[ERROR] {type(e).__name__}: {e}")
        await websocket.send_text("FAILED")


_dist = frontend_dist_dir()
if _dist.is_dir() and (_dist / "index.html").is_file():
    from fastapi.staticfiles import StaticFiles

    _assets = _dist / "assets"
    if _assets.is_dir():
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/")
    async def spa_index():
        return FileResponse(_dist / "index.html")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        if full_path.startswith("api") or full_path.startswith("docs"):
            raise HTTPException(status_code=404)
        if full_path == "openapi.json" or full_path.startswith("redoc"):
            raise HTTPException(status_code=404)
        if full_path.startswith("ws"):
            raise HTTPException(status_code=404)
        target = (_dist / full_path).resolve()
        try:
            target.relative_to(_dist.resolve())
        except ValueError:
            raise HTTPException(status_code=404)
        if target.is_file():
            return FileResponse(target)
        return FileResponse(_dist / "index.html")
