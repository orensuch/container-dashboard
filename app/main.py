from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import docker
from docker.errors import APIError, DockerException, NotFound
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Compose Stack Dashboard")

# Serve frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root():
    return FileResponse("static/index.html")


def _docker_client() -> docker.DockerClient:
    # Uses DOCKER_HOST env if set (we set it in compose), otherwise default.
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
    # Docker stats formula (similar to CLI): delta_cpu/delta_system * online_cpus * 100
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
    # usage, limit (bytes)
    try:
        mem = stats.get("memory_stats", {})
        usage = int(mem.get("usage", 0))
        limit = int(mem.get("limit", 0))
        return usage, limit
    except Exception:
        return 0, 0


def _net_io(stats: Dict[str, Any]) -> Tuple[int, int]:
    # rx, tx bytes (sum all interfaces)
    try:
        networks = stats.get("networks") or {}
        rx = 0
        tx = 0
        for _, v in networks.items():
            rx += int(v.get("rx_bytes", 0))
            tx += int(v.get("tx_bytes", 0))
        return rx, tx
    except Exception:
        return 0, 0


def _blk_io(stats: Dict[str, Any]) -> Tuple[int, int]:
    # read, write bytes (best-effort)
    try:
        blk = stats.get("blkio_stats", {}) or {}
        read_b = 0
        write_b = 0
        for entry in blk.get("io_service_bytes_recursive") or []:
            op = (entry.get("op") or "").lower()
            val = int(entry.get("value", 0))
            if op == "read":
                read_b += val
            elif op == "write":
                write_b += val
        return read_b, write_b
    except Exception:
        return 0, 0


def _format_bytes(n: int) -> int:
    # keep raw in API, format in UI; this exists if you later want server formatting
    return int(n)


@app.get("/api/stacks")
def list_stacks() -> Dict[str, Any]:
    """
    Returns stacks grouped by Docker Compose project, with aggregated resource usage.
    """
    try:
        client = _docker_client()
        containers = client.containers.list(all=True)
    except DockerException as e:
        raise HTTPException(status_code=500, detail=f"Docker not reachable: {e}")

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

        # Basic container info
        info = {
            "id": c.id[:12],
            "name": c.name,
            "image": (c.image.tags[0] if c.image and c.image.tags else (c.image.short_id if c.image else "")),
            "status": c.status,
            "service": (c.labels or {}).get("com.docker.compose.service"),
        }

        # Try to get live stats (non-streaming)
        try:
            s = c.stats(stream=False)
            cpu = _cpu_percent(s)
            mem_u, mem_l = _mem_usage(s)
            rx, tx = _net_io(s)
            br, bw = _blk_io(s)

            info["stats"] = {
                "cpu_percent": cpu,
                "mem_usage": _format_bytes(mem_u),
                "mem_limit": _format_bytes(mem_l),
                "net_rx": _format_bytes(rx),
                "net_tx": _format_bytes(tx),
                "blk_read": _format_bytes(br),
                "blk_write": _format_bytes(bw),
            }

            t = stacks[project]["totals"]
            t["cpu_percent"] += float(cpu)  # sum across containers
            t["mem_usage"] += int(mem_u)
            t["mem_limit"] += int(mem_l)    # not perfect, but OK for a quick aggregate
            t["net_rx"] += int(rx)
            t["net_tx"] += int(tx)
            t["blk_read"] += int(br)
            t["blk_write"] += int(bw)

        except (APIError, Exception):
            info["stats"] = None

        stacks[project]["containers"].append(info)

    # Sort projects and containers for stable UI
    ordered = [stacks[k] for k in sorted(stacks.keys())]
    for st in ordered:
        st["containers"] = sorted(st["containers"], key=lambda x: (x.get("service") or "", x["name"]))

    return {"stacks": ordered, "updated_at": now}


@app.post("/api/stacks/{project}/restart")
def restart_stack(project: str) -> Dict[str, Any]:
    """
    Restarts all containers belonging to a Compose project.
    """
    try:
        client = _docker_client()
        containers = client.containers.list(all=True)
    except DockerException as e:
        raise HTTPException(status_code=500, detail=f"Docker not reachable: {e}")

    target = [c for c in containers if _is_compose_container(c) and _compose_project(c) == project]
    if not target:
        raise HTTPException(status_code=404, detail=f"No containers found for project '{project}'")

    restarted: List[str] = []
    errors: List[str] = []

    # Restart in a deterministic order (service + name)
    def _sort_key(c):
        labels = c.labels or {}
        return (labels.get("com.docker.compose.service") or "", c.name)

    for c in sorted(target, key=_sort_key):
        try:
            c.restart(timeout=10)
            restarted.append(c.name)
        except (APIError, NotFound, Exception) as e:
            errors.append(f"{c.name}: {e}")

    return {"project": project, "restarted": restarted, "errors": errors}
