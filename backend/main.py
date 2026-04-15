from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
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
DEFAULT_SITE_CATEGORIES_PATH = runtime_root() / "config" / "sites" / "categories.json"
BUILD_LOG_PATH = runtime_root() / "logs" / "build.log"
BUILD_HISTORY_PATH = runtime_root() / "logs" / "build_history.json"
BUILD_HISTORY_LOG_PATH = runtime_root() / "logs" / "build_history.log"

app = FastAPI(title="Docker Build Publisher")
BUILD_LOCK = asyncio.Lock()
BUILD_CANCEL_EVENT = asyncio.Event()
RUNNING_PROCS: set[asyncio.subprocess.Process] = set()


def ensure_runtime_artifacts() -> None:
    """
    启动时确保日志目录与文件存在，避免首屏读取为空时被误判为“未保存”。
    """
    BUILD_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not BUILD_LOG_PATH.exists():
        BUILD_LOG_PATH.touch()
    if not BUILD_HISTORY_PATH.exists():
        with BUILD_HISTORY_PATH.open("w", encoding="utf-8", newline="\n") as f:
            f.write("[]\n")
    if not BUILD_HISTORY_LOG_PATH.exists():
        BUILD_HISTORY_LOG_PATH.touch()


ensure_runtime_artifacts()


def _parse_proxy_index_override(query_params: Any, key: str) -> Optional[int]:
    raw = query_params.get(key)
    if raw is None or raw == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _parse_use_proxy_flag(query_params: Any, key: str) -> Optional[bool]:
    """
    WebSocket 查询参数：1/true 表示页面勾选使用代理；0/false 表示不用。
    未传则 None，后端按各阶段默认策略处理。
    """
    raw = query_params.get(key)
    if raw is None or raw == "":
        return None
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


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


def append_build_log(line: str) -> None:
    BUILD_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with BUILD_LOG_PATH.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"[{stamp}] {line}\n")


def read_build_log_lines(limit: int = 10) -> list[str]:
    if not BUILD_LOG_PATH.is_file():
        return []
    try:
        with BUILD_LOG_PATH.open(encoding="utf-8") as f:
            lines = [x.rstrip("\n\r") for x in f.readlines()]
        return lines[-limit:]
    except OSError:
        return []


def read_build_history(limit: int = 200) -> list[dict[str, str]]:
    data = load_json_file(BUILD_HISTORY_PATH, [])
    if not isinstance(data, list):
        return []
    items = [x for x in data if isinstance(x, dict)]
    return items[-limit:][::-1]


def append_build_history(entry: dict[str, str]) -> None:
    data = load_json_file(BUILD_HISTORY_PATH, [])
    if not isinstance(data, list):
        data = []
    data.append(entry)
    save_json_file(BUILD_HISTORY_PATH, data[-500:])
    # 兼容人工排查习惯：同步追加一份可读 log 文本
    with BUILD_HISTORY_LOG_PATH.open("a", encoding="utf-8", newline="\n") as f:
        f.write(
            f"[{entry.get('time','')}] repo={entry.get('repository','')} "
            f"ip={entry.get('ip','')} image={entry.get('image','')}\n"
        )


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


def site_categories_path() -> Path:
    return DEFAULT_SITE_CATEGORIES_PATH


def load_site_categories() -> list[dict[str, Any]]:
    data = load_json_file(site_categories_path(), [])
    if not isinstance(data, list):
        return []
    result: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            result.append(item)
    return result


def save_site_categories(categories: list[dict[str, Any]]) -> None:
    save_json_file(site_categories_path(), categories)


def normalize_categories(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(categories):
        cid = str(item.get("id", "")).strip()
        if not cid:
            continue
        pid_raw = item.get("parent_id")
        pid = str(pid_raw).strip() if isinstance(pid_raw, str) else None
        if not pid:
            pid = None
        name = str(item.get("name", "")).strip()
        if not name:
            name = "未命名类型"
        sort = item.get("sort", idx)
        if not isinstance(sort, int):
            sort = idx
        normalized.append(
            {
                "id": cid,
                "name": name,
                "parent_id": pid,
                "sort": sort,
                "created_at": str(item.get("created_at", "")),
            }
        )
    return normalized


def build_category_tree(flat_categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for item in flat_categories:
        nodes[item["id"]] = {
            "id": item["id"],
            "name": item["name"],
            "parent_id": item["parent_id"],
            "sort": item["sort"],
            "created_at": item.get("created_at", ""),
            "children": [],
        }

    roots: list[dict[str, Any]] = []
    for node in nodes.values():
        parent_id = node.get("parent_id")
        if parent_id and parent_id in nodes and parent_id != node["id"]:
            nodes[parent_id]["children"].append(node)
        else:
            roots.append(node)

    def sort_nodes(items: list[dict[str, Any]]) -> None:
        items.sort(key=lambda x: (x.get("sort", 0), x.get("created_at", ""), x.get("name", "")))
        for it in items:
            sort_nodes(it["children"])

    sort_nodes(roots)
    return roots


def flatten_category_tree(
    tree_nodes: list[dict[str, Any]], parent_id: Optional[str] = None
) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for idx, node in enumerate(tree_nodes):
        node_id = str(node.get("id", "")).strip()
        if not node_id:
            continue
        flat.append({"id": node_id, "parent_id": parent_id, "sort": idx})
        children = node.get("children")
        if isinstance(children, list) and children:
            flat.extend(flatten_category_tree(children, node_id))
    return flat


class SiteCreate(BaseModel):
    name: str = ""
    url: str = Field(min_length=1)
    username: str = ""
    password: str = ""
    category_id: str = ""

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

    @field_validator("username", "password", "category_id")
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
    category_id: str = ""
    created_at: str


class SiteCategoryCreate(BaseModel):
    name: str = Field(min_length=1)
    parent_id: Optional[str] = None

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("name 不能为空")
        return text

    @field_validator("parent_id")
    @classmethod
    def trim_parent_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        return text or None


class SiteCategoryReorder(BaseModel):
    tree: list[dict[str, Any]]


class SiteCategoryAssign(BaseModel):
    category_id: str = ""

    @field_validator("category_id")
    @classmethod
    def trim_category_id(cls, value: str) -> str:
        return value.strip()


async def stream_command(
    websocket: WebSocket,
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    prefix: str = "",
    compact_docker_push: bool = False,
    persist_log: bool = True,
) -> int:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=env,
    )
    assert proc.stdout is not None
    RUNNING_PROCS.add(proc)
    waiting_layers: set[str] = set()
    pushed_layers: set[str] = set()
    mounted_layers: set[str] = set()
    active_layers: set[str] = set()
    known_layers: set[str] = set()
    last_progress_text = ""
    push_line_pattern = re.compile(r"^([0-9a-f]{6,64}):\s*(.+)$")

    websocket_broken = False
    try:
        while True:
            if BUILD_CANCEL_EVENT.is_set():
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                break
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace")
            raw = text.rstrip("\n\r")

            # 压缩 docker push 刷屏，改成可感知的进度摘要日志
            if compact_docker_push:
                matched = push_line_pattern.match(raw)
                if matched:
                    layer_id = matched.group(1).strip()
                    status = matched.group(2).strip()
                    if layer_id:
                        known_layers.add(layer_id)

                    if status == "Waiting":
                        waiting_layers.add(layer_id)
                        active_layers.discard(layer_id)
                    elif status.startswith("Pushing "):
                        waiting_layers.discard(layer_id)
                        active_layers.add(layer_id)
                    elif status.startswith("Pushed"):
                        waiting_layers.discard(layer_id)
                        active_layers.discard(layer_id)
                        pushed_layers.add(layer_id)
                    elif status.startswith("Layer already exists") or status.startswith(
                        "Mounted from "
                    ):
                        waiting_layers.discard(layer_id)
                        active_layers.discard(layer_id)
                        mounted_layers.add(layer_id)
                    elif status.startswith("Preparing"):
                        waiting_layers.add(layer_id)
                        active_layers.discard(layer_id)

                    done_layers = pushed_layers | mounted_layers
                    total_layers = max(len(known_layers), len(done_layers))
                    done_count = len(done_layers)
                    percent = 100.0 if total_layers == 0 else (done_count * 100.0 / total_layers)
                    progress = (
                        f"[进度] docker push: 已完成 {done_count}/{total_layers} "
                        f"({percent:.0f}%) | 上传中 {len(active_layers)} 层 | 等待 {len(waiting_layers)} 层"
                    )
                    if progress != last_progress_text:
                        if not websocket_broken:
                            try:
                                await websocket.send_text(progress)
                            except Exception:
                                websocket_broken = True
                        if persist_log:
                            append_build_log(progress)
                        last_progress_text = progress
                    continue

            if prefix:
                raw = f"{prefix}{raw}"
            if not websocket_broken:
                try:
                    await websocket.send_text(raw)
                except Exception:
                    websocket_broken = True
            if persist_log:
                append_build_log(raw)
    finally:
        await proc.wait()
        RUNNING_PROCS.discard(proc)
    if BUILD_CANCEL_EVENT.is_set():
        return 130
    return proc.returncode or 0


@app.post("/api/build/cancel")
async def cancel_build():
    running = BUILD_LOCK.locked() or any(p.returncode is None for p in RUNNING_PROCS)
    if not running:
        return {"ok": True, "message": "当前没有运行中的构建任务"}
    BUILD_CANCEL_EVENT.set()
    for p in list(RUNNING_PROCS):
        try:
            p.terminate()
        except ProcessLookupError:
            continue
    return {"ok": True, "message": "已发送终止信号"}


@app.get("/api/build/cancel")
async def cancel_build_get():
    # 兼容旧前端或缓存资源触发的 GET 请求
    return await cancel_build()


@app.get("/api/build/status")
async def get_build_status():
    running = BUILD_LOCK.locked() or any(p.returncode is None for p in RUNNING_PROCS)
    return {"running": running, "cancel_requested": BUILD_CANCEL_EVENT.is_set()}


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
    返回某项目可用的仓库键列表，供前端勾选（仅 key，是否走代理由页面勾选决定）。
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


@app.get("/api/build/log")
async def get_build_log(
    limit: int = Query(default=2000, ge=1, le=20000, description="返回最近若干行"),
):
    return read_build_log_lines(limit=limit)


@app.delete("/api/build/log")
async def clear_build_log():
    try:
        if BUILD_LOG_PATH.exists():
            BUILD_LOG_PATH.unlink()
        return {"ok": True, "message": "构建日志已删除"}
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"删除日志失败: {exc}")


@app.get("/api/build/history")
async def get_build_history(
    limit: int = Query(default=200, ge=1, le=500, description="最多返回条数"),
):
    return read_build_history(limit=limit)


@app.get("/api/build/proxy-options")
async def get_build_proxy_options():
    """根配置 proxy 列表（含 url），供前端下拉展示。"""
    cfg = build_push.try_load_build_config()
    if cfg is None:
        return []
    return build_push.list_global_proxy_options(cfg)


@app.get("/api/build/debug-paths")
async def get_build_debug_paths():
    root = runtime_root()
    log_exists = BUILD_LOG_PATH.is_file()
    history_exists = BUILD_HISTORY_PATH.is_file()
    return {
        "runtime_root": str(root.resolve()),
        "logs_dir": str(BUILD_LOG_PATH.parent.resolve()),
        "build_log_path": str(BUILD_LOG_PATH.resolve()),
        "build_history_path": str(BUILD_HISTORY_PATH.resolve()),
        "build_history_log_path": str(BUILD_HISTORY_LOG_PATH.resolve()),
        "build_log_exists": log_exists,
        "build_history_exists": history_exists,
        "build_log_size": BUILD_LOG_PATH.stat().st_size if log_exists else 0,
        "build_history_size": BUILD_HISTORY_PATH.stat().st_size if history_exists else 0,
        "build_history_log_exists": BUILD_HISTORY_LOG_PATH.is_file(),
        "build_history_log_size": BUILD_HISTORY_LOG_PATH.stat().st_size
        if BUILD_HISTORY_LOG_PATH.is_file()
        else 0,
    }


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
    if payload.category_id:
        categories = normalize_categories(load_site_categories())
        valid_ids = {x["id"] for x in categories}
        if payload.category_id not in valid_ids:
            raise HTTPException(status_code=400, detail="category_id 不存在")
    site = SiteOut(
        id=str(uuid.uuid4()),
        name=payload.name,
        url=payload.url,
        username=payload.username,
        password=payload.password,
        category_id=payload.category_id,
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


@app.put("/api/sites/{site_id}/category")
async def assign_site_category(site_id: str, payload: SiteCategoryAssign):
    sites = load_sites()
    if not sites:
        raise HTTPException(status_code=404, detail="site not found")

    category_id = payload.category_id
    if category_id:
        categories = normalize_categories(load_site_categories())
        valid_ids = {x["id"] for x in categories}
        if category_id not in valid_ids:
            raise HTTPException(status_code=400, detail="category_id 不存在")

    hit = False
    updated_sites: list[dict[str, Any]] = []
    for item in sites:
        if not isinstance(item, dict):
            continue
        if str(item.get("id", "")).strip() == site_id:
            item["category_id"] = category_id
            hit = True
        updated_sites.append(item)

    if not hit:
        raise HTTPException(status_code=404, detail="site not found")
    save_sites(updated_sites)
    return {"ok": True}


@app.get("/api/site-categories")
async def list_site_categories():
    categories = normalize_categories(load_site_categories())
    return build_category_tree(categories)


@app.post("/api/site-categories")
async def create_site_category(payload: SiteCategoryCreate):
    categories = normalize_categories(load_site_categories())
    if payload.parent_id:
        ids = {x["id"] for x in categories}
        if payload.parent_id not in ids:
            raise HTTPException(status_code=400, detail="parent_id 不存在")

    new_category = {
        "id": str(uuid.uuid4()),
        "name": payload.name,
        "parent_id": payload.parent_id,
        "sort": 999999,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    categories.append(new_category)
    save_site_categories(categories)
    return new_category


@app.put("/api/site-categories/reorder")
async def reorder_site_categories(payload: SiteCategoryReorder):
    categories = normalize_categories(load_site_categories())
    categories_by_id = {x["id"]: x for x in categories}
    flat = flatten_category_tree(payload.tree)
    if not flat:
        raise HTTPException(status_code=400, detail="tree 不能为空")

    incoming_ids = [x["id"] for x in flat]
    saved_ids = set(categories_by_id.keys())
    if set(incoming_ids) != saved_ids:
        raise HTTPException(status_code=400, detail="tree 节点与已保存类型不一致")

    for item in flat:
        cur = categories_by_id[item["id"]]
        cur["parent_id"] = item["parent_id"]
        cur["sort"] = item["sort"]
    save_site_categories(list(categories_by_id.values()))
    return {"ok": True}


@app.delete("/api/site-categories/{category_id}")
async def delete_site_category(category_id: str):
    categories = normalize_categories(load_site_categories())
    if not categories:
        raise HTTPException(status_code=404, detail="category not found")
    if any(x.get("parent_id") == category_id for x in categories):
        raise HTTPException(status_code=400, detail="请先删除子类型")
    sites = load_sites()
    if any(str(x.get("category_id", "")).strip() == category_id for x in sites if isinstance(x, dict)):
        raise HTTPException(status_code=400, detail="该类型下仍有网站")
    new_categories = [x for x in categories if x["id"] != category_id]
    if len(new_categories) == len(categories):
        raise HTTPException(status_code=404, detail="category not found")
    save_site_categories(new_categories)
    return {"ok": True}


@app.websocket("/ws/build/{project_name}")
async def ws_build(websocket: WebSocket, project_name: str):
    await websocket.accept()
    if BUILD_LOCK.locked():
        await websocket.send_text("[ERROR] 当前已有构建任务在执行，请稍后再试。")
        await websocket.send_text("FAILED")
        return

    await BUILD_LOCK.acquire()
    BUILD_CANCEL_EVENT.clear()
    try:
        client_ip = websocket.client.host if websocket.client else ""

        async def emit(text: str) -> None:
            await websocket.send_text(text)
            if text not in ("SUCCESS", "FAILED"):
                append_build_log(text)

        # 解析前端勾选的仓库键（逗号分隔）
        raw = websocket.query_params.get("repos", "")
        selected_repos = [x for x in raw.split(",") if x.strip()]
        selected_set = set(selected_repos)

        cfg = build_push.load_build_config()
        projects = cfg.get("projects") or {}
        if not isinstance(projects, dict) or project_name not in projects:
            await emit(f"[ERROR] 未知项目: {project_name}")
            await emit("FAILED")
            return

        # 计算本次所有目标仓库 / 组件
        all_targets = build_push.build_repo_targets(project_name, cfg)
        repos_cfg = cfg.get("repositories") if isinstance(cfg.get("repositories"), dict) else {}

        if selected_set:
            targets = [t for t in all_targets if t.key in selected_set]
        else:
            targets = all_targets

        if not targets:
            await emit("[ERROR] 未选择任何有效仓库")
            await emit("FAILED")
            return

        # 推送代理（兼容旧参数 use_proxy/proxy_index）
        push_proxy_index_override = _parse_proxy_index_override(
            websocket.query_params, "push_proxy_index"
        )
        if push_proxy_index_override is None:
            push_proxy_index_override = _parse_proxy_index_override(
                websocket.query_params, "proxy_index"
            )
        client_use_push_proxy = _parse_use_proxy_flag(
            websocket.query_params, "use_push_proxy"
        )
        if client_use_push_proxy is None:
            client_use_push_proxy = _parse_use_proxy_flag(
                websocket.query_params, "use_proxy"
            )
        # 构建代理（docker build）
        build_proxy_index_override = _parse_proxy_index_override(
            websocket.query_params, "build_proxy_index"
        )
        client_use_build_proxy = _parse_use_proxy_flag(
            websocket.query_params, "use_build_proxy"
        )

        # 构建目录：~/{项目名}_home/{项目名}/x64_Env/LinuxRelease
        release_dir = build_push.release_dir_for_project(project_name)
        await emit(f"[状态] 准备中 — 项目: {project_name}")
        await emit(f"[信息] 发布目录: {release_dir}")
        await emit(f"[信息] 目标仓库: {', '.join(sorted({t.key for t in targets}))}")

        if not release_dir.is_dir():
            await emit(f"[ERROR] 构建目录不存在: {release_dir}")
            await emit("FAILED")
            return

        await emit("[状态] 正在编译（容器内）")
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
        await emit(f"$ {' '.join(compile_cmd)}")
        rc = await stream_command(websocket, compile_cmd)
        if rc != 0:
            if rc == 130:
                await emit("[WARN] 构建已被手动终止。")
                await emit("FAILED")
                return
            await emit(f"[ERROR] 编译失败，退出码: {rc}")
            await emit("FAILED")
            return

        tag = build_push.make_datetime_tag(project_name)
        await emit("[状态] 正在生成版本号")
        await emit(f"[信息] 本次 Tag: {tag}")

        local_image = f"{project_name}:{tag}"
        await emit(f"[状态] 正在打包本地镜像 — {local_image}")
        local_build_cmd = ["docker", "build", "-t", local_image, "."]
        build_proxy_choice = build_push.resolve_global_proxy_choice(
            cfg=cfg,
            proxy_index_override=build_proxy_index_override,
            client_use_proxy=client_use_build_proxy,
        )
        if build_proxy_choice:
            await emit(
                f"[信息] 本次构建代理: {build_proxy_choice.index + 1}/{build_proxy_choice.total} => "
                f"{build_proxy_choice.url}"
            )
        await emit(f"$ cd {release_dir} && {' '.join(local_build_cmd)}")
        rc = await stream_command(
            websocket,
            local_build_cmd,
            cwd=str(release_dir),
            env=build_push.build_proxy_env(dict(), build_proxy_choice),
        )
        if rc != 0:
            if rc == 130:
                await emit("[WARN] 构建已被手动终止。")
                await emit("FAILED")
                return
            await emit(f"[ERROR] 本地镜像打包失败，退出码: {rc}")
            await emit("FAILED")
            return

        logged_in_repos: set[str] = set()
        for t in targets:
            repo_cfg = repos_cfg.get(t.key) if isinstance(repos_cfg, dict) else None
            if (
                isinstance(repo_cfg, dict)
                and t.key not in logged_in_repos
                and build_push.is_aliyun_repo(repo_cfg)
            ):
                await emit(f"[状态] 正在登录阿里云仓库 — {t.key}")
                try:
                    build_push.ensure_aliyun_login(
                        repo_cfg,
                        cfg=cfg,
                        dry_run=False,
                        attempt=1,
                        proxy_index_override=push_proxy_index_override,
                        client_use_proxy=client_use_push_proxy,
                    )
                except Exception as exc:
                    if not hasattr(exc, "args") or not exc.args:
                        await emit("[ERROR] 阿里云登录失败：未知异常")
                    else:
                        await emit(f"[ERROR] 阿里云登录失败详情：{exc}")
                    await emit(f"[ERROR] 阿里云登录失败（仓库 {t.key}）：{exc}")
                    await emit("FAILED")
                    return
                logged_in_repos.add(t.key)

            image_with_tag = f"{t.full_name}:{tag}"
            await emit(
                f"[状态] 正在打标签 — 仓库: {t.key}, 组件: {t.component}"
            )
            tag_cmd = [
                "docker",
                "tag",
                local_image,
                image_with_tag,
            ]
            await emit(f"$ {' '.join(tag_cmd)}")
            rc = await stream_command(websocket, tag_cmd)
            if rc != 0:
                if rc == 130:
                    await emit("[WARN] 构建已被手动终止。")
                    await emit("FAILED")
                    return
                await emit(
                    f"[ERROR] docker tag 失败（仓库 {t.key} 组件 {t.component}），退出码: {rc}"
                )
                await emit("FAILED")
                return

            await emit(f"[状态] 正在推送镜像 — {image_with_tag}")
            push_cmd = ["docker", "push", image_with_tag]
            push_ok = False
            for attempt in range(1, 4):
                proxy_choice = (
                    build_push.resolve_proxy_choice(
                        repo_cfg,
                        cfg=cfg,
                        attempt=attempt,
                        proxy_index_override=push_proxy_index_override,
                        client_use_proxy=client_use_push_proxy,
                    )
                    if isinstance(repo_cfg, dict) and build_push.is_aliyun_repo(repo_cfg)
                    else None
                )
                if proxy_choice:
                    await emit(
                        f"[信息] 本次推送代理: {proxy_choice.index + 1}/{proxy_choice.total} => "
                        f"{proxy_choice.url}"
                    )
                await emit(f"[状态] 推送尝试 {attempt}/3")
                await emit(f"$ {' '.join(push_cmd)}")
                rc = await stream_command(
                    websocket,
                    push_cmd,
                    compact_docker_push=True,
                    env=build_push.build_proxy_env(dict(), proxy_choice),
                )
                if rc == 0:
                    push_ok = True
                    break
                if rc == 130:
                    await emit("[WARN] 构建已被手动终止。")
                    await emit("FAILED")
                    return
                if attempt < 3:
                    await emit(
                        f"[WARN] docker push 失败（退出码: {rc}），准备重试..."
                    )
                    if (
                        isinstance(repo_cfg, dict)
                        and build_push.is_aliyun_repo(repo_cfg)
                    ):
                        await emit(
                            f"[状态] 推送失败后重新登录阿里云仓库 — {t.key}"
                        )
                        try:
                            build_push.ensure_aliyun_login(
                                repo_cfg,
                                cfg=cfg,
                                dry_run=False,
                                attempt=attempt + 1,
                                proxy_index_override=push_proxy_index_override,
                                client_use_proxy=client_use_push_proxy,
                            )
                        except Exception as exc:
                            await emit(
                                f"[ERROR] 阿里云重登录失败（仓库 {t.key}）：{exc}"
                            )
                            await emit("FAILED")
                            return
                    await asyncio.sleep(attempt * 2)
            if not push_ok:
                await emit(
                    f"[ERROR] docker push 失败（仓库 {t.key} 组件 {t.component}），已重试 3 次"
                )
                await emit("FAILED")
                return
            append_build_history(
                {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "ip": client_ip,
                    "image": image_with_tag,
                    "repository": t.key,
                }
            )

            if build_push.gs_restart_should_run(
                repo_cfg if isinstance(repo_cfg, dict) else None,
                cfg,
                t,
            ):
                base = build_push.resolve_gs_restart_api_base(repo_cfg, cfg)
                profile = build_push.resolve_gs_restart_profile(repo_cfg)
                if base and profile:
                    await emit(
                        f"[状态] 正在通知游服切换 GS 镜像 — 仓库 {t.key}, profile={profile}"
                    )
                    try:
                        await asyncio.to_thread(
                            build_push.notify_gs_restart_api,
                            base,
                            profile,
                            image_with_tag,
                        )
                        await emit(f"[信息] 游服重启 API 已完成 — profile={profile}")
                    except Exception as exc:
                        await emit(
                            f"[WARN] 游服重启 API 失败（镜像已成功推送）: {exc}"
                        )

        await emit("[状态] 推送成功 — 所有选中仓库")
        await emit("SUCCESS")
    except WebSocketDisconnect:
        raise
    except Exception as e:
        await websocket.send_text(f"[ERROR] {type(e).__name__}: {e}")
        await websocket.send_text("FAILED")
    finally:
        if BUILD_LOCK.locked():
            BUILD_LOCK.release()


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
