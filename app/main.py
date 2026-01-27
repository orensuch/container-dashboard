from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Tuple

import docker
from docker.errors import APIError, DockerException
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Compose Stack Dashboard")
app.mount("/static", StaticFiles(directory="static"), name="static")

STATS_CACHE: Dict[str, Dict[str, Any]] = {}  # container_id -> stats dict
CACHE_META: Dict[str, Any] = {"updated_at": 0, "last_error": None}

REFRESH_SECONDS = 2
MAX_WORKERS = 16

_cache_lock = threading.Lock()
_stop_event = threading.Event()


@app.get("/")
def root():
    return FileResponse("static/index.html")


def _docker_client() -> docker.DockerClient:
    return docker.from_env()


def _is_compose_container(c) -> bool:
    labels = c.labels or {}
    return "com.docker.compose.project" in labels


def _compose_project(c) -> str:
    return (c.labels or {}).get("com.docker.compose.project", "unknown")


def _safe_float(x: Any) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def _cpu_percent(stats: Dict[str, Any]) -> float:
    try:
        cpu_stats = stats.get("cpu_stats", {})
        precpu_stats = stats.get("precpu_stats", {})

        cpu_total = _safe_float(cpu_stats["cpu_usage"]["total_usage"])
        precpu_total = _safe_float(precpu_stats["cpu_usage"]["total_usage"])

        system_total = _safe_float(cpu_stats["system_cpu_usage"])
        presystem_total = _safe_float(precpu_stats["system_cpu_usage"])

        cpu_delta = cpu_total - precpu_total
        system_delta = system_total - presystem_total

        online_cpus = cpu_stats.get("online_cpus")
        if online_cpus is None:
            online_cpus = len(cpu_stats.get("cpu_usage", {}).get("percpu_usage") or []) or 1

        if system_delta > 0 and cpu_delta > 0:
            return (cpu_delta / system_delta) * float(online_cpus) * 100.0
        return 0.0
    except Exception:
        return 0.0


def _mem_usage(stats: Dict[str, Any]) -> Tuple[int, int]:
    mem = stats.get("memory_stats", {}) or {}
    return int(mem.get("usage", 0)), int(mem.get("limit", 0))


def _net_io(stats: Dict[str, Any]) -> Tuple[int, int]:
    networks = stats.get("networks") or {}
    rx = tx = 0
    for v in networks.values():
        rx += int(v.get("rx_bytes", 0))
        tx += int(v.get("tx_bytes", 0))
    return rx, tx


def _blk_io(stats: Dict[str, Any]) -> Tuple[int, int]:
    blk = stats.get("blkio_stats", {}) or {}
    read_b = write_b = 0
    for entry in blk.get("io_service_bytes_recursive") or []:
        op = (entry.get("op") or "").lower()
        val = int(entry.get("value", 0))
        if op == "read":
            read_b += val
        elif op == "write":
            write_b += val
    return read_b, write_b


def _get_container_stats(client: docker.DockerClient, container_id: str) -> Dict[str, Any] | None:
    # Re-fetch container handle by id inside worker thread
    try:
        c = client.containers.get(container_id)
        s = c.stats(stream=False)
        cpu = _cpu_percent(s)
        mem_u, mem_l = _mem_usage(s)
        rx, tx = _net_io(s)
        br, bw = _blk_io(s)
        return {
            "cpu_percent": cpu,
            "mem_usage": mem_u,
            "mem_limit": mem_l,
            "net_rx": rx,
            "net_tx": tx,
            "blk_read": br,
            "blk_write": bw,
        }
    except Exception:
        return None


def _refresh_loop():
    while not _stop_event.is_set():
        try:
            client = _docker_client()
            containers = client.containers.list(all=False)  # only running -> faster
            compose_ids = [c.id for c in containers if _is_compose_container(c)]

            new_cache: Dict[str, Dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futures = {ex.submit(_get_container_stats, client, cid): cid for cid in compose_ids}
                for fut in as_completed(futures):
                    cid = futures[fut]
                    st = fut.result()
                    if st is not None:
                        new_cache[cid] = st

            with _cache_lock:
                STATS_CACHE.clear()
                STATS_CACHE.update(new_cache)
                CACHE_META["updated_at"] = int(time.time())
                CACHE_META["last_error"] = None

        except Exception as e:
            with _cache_lock:
                CACHE_META["last_error"] = str(e)

        _stop_event.wait(REFRESH_SECONDS)


@app.on_event("startup")
def on_startup():
    t = threading.Thread(target=_refresh_loop, daemon=True)
    t.start()


@app.on_event("shutdown")
def on_shutdown():
    _stop_event.set()


@app.get("/api/stacks")
def list_stacks() -> Dict[str, Any]:
    """
    Fast endpoint: uses cached stats; doesn't call Docker stats on-demand.
    """
    try:
        client = _docker_client()
        containers = client.containers.list(all=True)
    except DockerException as e:
        raise HTTPException(status_code=500, detail=f"Docker not reachable: {e}")

    with _cache_lock:
        cache = dict(STATS_CACHE)
        updated_at = CACHE_META["updated_at"]
        last_error = CACHE_META["last_error"]

    stacks: Dict[str, Dict[str, Any]] = {}
    now = int(time.time())

    for c in containers:
        if not _is_compose_container(c):
            continue

        project = _compose_project(c)
        stacks.setdefault(
            project,
            {
                "project": project,
                "containers": [],
                "totals": {
                    "cpu_percent": 0.0,
                    "mem_usage": 0,
                    "mem_limit": 0,
                    "net_rx": 0,
                    "net_tx": 0,
                    "blk_read": 0,
                    "blk_write": 0,
                },
                "updated_at": now,
            },
        )

        info = {
            "id": c.id[:12],
            "name": c.name,
            "status": c.status,
            "service": (c.labels or {}).get("com.docker.compose.service"),
        }

        st = cache.get(c.id)  # cached stats keyed by full id
        info["stats"] = st

        if st:
            t = stacks[project]["totals"]
            t["cpu_percent"] += float(st["cpu_percent"])
            t["mem_usage"] += int(st["mem_usage"])
            t["mem_limit"] += int(st["mem_limit"])
            t["net_rx"] += int(st["net_rx"])
            t["net_tx"] += int(st["net_tx"])
            t["blk_read"] += int(st["blk_read"])
            t["blk_write"] += int(st["blk_write"])

        stacks[project]["containers"].append(info)

    ordered = [stacks[k] for k in sorted(stacks.keys())]
    for st in ordered:
        st["containers"] = sorted(st["containers"], key=lambda x: (x.get("service") or "", x["name"]))

    return {"stacks": ordered, "updated_at": updated_at, "cache_error": last_error}
