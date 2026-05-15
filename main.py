import argparse
import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import cv2
except ImportError:
    cv2 = None

try:
    from prettytable import PrettyTable
except ImportError:
    PrettyTable = None


CLR_A = "\x1b[01;38;5;117m"
CLR_P = "\x1b[01;38;5;153m"
CLR_C = "\x1b[01;38;5;123m"
HEART = "\x1b[01;38;5;195m"
CLR_RST = "\x1b[0m"


JsonDict = Dict[str, Any]
Route = List[Tuple[float, float]]


def load_profile(path: Path) -> JsonDict:
    if not path.is_file():
        sys.exit(f"{CLR_A}profile not found: {path}{CLR_RST}")
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    return data


def get_section(profile: JsonDict, name: str) -> JsonDict:
    value = profile.get(name, {})
    if not isinstance(value, dict):
        sys.exit(f"{CLR_A}profile section must be an object: {name}{CLR_RST}")
    return value


def as_path(value: Optional[str]) -> Optional[Path]:
    if not value:
        return None
    return Path(value).expanduser()


def find_first(root: Path, names: List[str]) -> Optional[Path]:
    if not root.exists():
        return None
    for name in names:
        direct = root / name
        if direct.is_file():
            return direct
    try:
        for name in names:
            matches = list(root.rglob(name))
            if matches:
                return matches[0]
    except PermissionError:
        return None
    return None


def common_roots() -> List[Path]:
    roots: List[Path] = []
    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        roots.extend(
            [
                Path(f"{drive}:\\Program Files\\Netease"),
                Path(f"{drive}:\\Program Files\\NetEase"),
                Path(f"{drive}:\\Program Files (x86)\\Netease"),
                Path(f"{drive}:\\Program Files (x86)\\NetEase"),
            ]
        )
    return roots


def resolve_mumu_paths(profile: JsonDict, profile_dir: Path) -> Tuple[Path, Path, Optional[Path], str]:
    mumu = get_section(profile, "mumu")
    cache_path = Path(mumu.get("cache_path", ".mumu_paths.json"))
    if not cache_path.is_absolute():
        cache_path = profile_dir / cache_path
    cache: JsonDict = {}
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    manager_path = as_path(mumu.get("manager_path")) or as_path(cache.get("manager_path"))
    adb_path = as_path(mumu.get("adb_path")) or as_path(cache.get("adb_path"))
    player_path = as_path(mumu.get("player_path")) or as_path(cache.get("player_path"))

    if not manager_path or not manager_path.is_file():
        manager_path = None
        for root in common_roots():
            manager_path = find_first(root, ["MuMuManager.exe"])
            if manager_path:
                break
    if not manager_path or not manager_path.is_file():
        sys.exit(f"{CLR_A}MuMuManager.exe not found. Set mumu.manager_path in profile.{CLR_RST}")

    if not adb_path or not adb_path.is_file():
        candidates = [
            manager_path.parent / "adb.exe",
            manager_path.parent.parent / "shell" / "adb.exe",
            manager_path.parent.parent / "nx_device" / "12.0" / "shell" / "adb.exe",
        ]
        adb_path = next((p for p in candidates if p.is_file()), None)
    if not adb_path or not adb_path.is_file():
        root = manager_path.parents[1] if len(manager_path.parents) > 1 else manager_path.parent
        adb_path = find_first(root, ["adb.exe"])
    if not adb_path or not adb_path.is_file():
        sys.exit(f"{CLR_A}adb.exe not found. Set mumu.adb_path in profile.{CLR_RST}")

    if player_path and not player_path.is_file():
        player_path = None
    if not player_path:
        root = manager_path.parents[1] if len(manager_path.parents) > 1 else manager_path.parent
        player_path = find_first(root, ["MuMuPlayer.exe", "MuMuNxMain.exe"])

    instance = str(mumu.get("instance", "0"))
    cache_path.write_text(
        json.dumps(
            {
                "manager_path": str(manager_path),
                "adb_path": str(adb_path),
                "player_path": str(player_path) if player_path else "",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return manager_path, adb_path, player_path, instance


def adb_cmd(adb: Path, serial: Optional[str], args: List[str]) -> List[str]:
    cmd = [str(adb)]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return cmd


def debug_log(ctx: JsonDict, message: str) -> None:
    if ctx.get("debug", False):
        print(f"{CLR_C}[debug] {message}{CLR_RST}")


def read_current_focus(adb: Path, serial: Optional[str]) -> str:
    try:
        out = subprocess.check_output(
            adb_cmd(adb, serial, ["shell", "dumpsys", "window"]),
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
    except Exception as exc:
        return f"failed to read current focus: {exc}"

    lines = []
    for line in out.splitlines():
        if "mCurrentFocus=" in line or "mFocusedApp=" in line:
            lines.append(line.strip())
    return "\n".join(lines) if lines else "current focus not found"


def run_mumu_json(mgr: Path, args: List[str], timeout: float = 20.0) -> JsonDict:
    result = subprocess.run(
        [str(mgr), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        details = output or (result.stderr or "").strip() or f"exit code {result.returncode}"
        raise RuntimeError(details)
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid MuMuManager JSON output: {output}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"unexpected MuMuManager output: {output}")
    return value


def wait_for_mumu_process(mgr: Path, instance: str, timeout_sec: float, poll_sec: float, debug: bool) -> None:
    deadline = time.time() + timeout_sec
    last_error = ""
    while True:
        try:
            info = run_mumu_json(mgr, ["info", "-v", instance], timeout=10.0)
            if debug:
                print(f"{CLR_C}[debug] instance info={json.dumps(info, ensure_ascii=False)}{CLR_RST}")
            if info.get("is_process_started") or info.get("player_state") == "start_finished":
                return
        except Exception as exc:
            last_error = str(exc)
        if time.time() >= deadline:
            suffix = f" Last error: {last_error}" if last_error else ""
            sys.exit(f"{CLR_A}MuMu instance {instance} did not start within {timeout_sec:.0f}s.{suffix}{CLR_RST}")
        time.sleep(poll_sec)


def read_installed_packages(mgr: Path, instance: str) -> JsonDict:
    return run_mumu_json(mgr, ["control", "-v", instance, "app", "info", "-i"], timeout=20.0)


def find_instances_with_packages(mgr: Path, packages: set) -> List[str]:
    matches: List[str] = []
    if not packages:
        return matches
    try:
        all_info = run_mumu_json(mgr, ["info", "-v", "all"], timeout=20.0)
    except Exception:
        return matches
    for index in sorted(str(key) for key in all_info.keys()):
        try:
            app_info = read_installed_packages(mgr, index)
        except Exception:
            continue
        installed = set(app_info.keys()) - {"active"}
        if packages.issubset(installed):
            matches.append(index)
    return matches


def sort_instance_ids(ids: List[str]) -> List[str]:
    return sorted(ids, key=lambda item: (not item.isdigit(), int(item) if item.isdigit() else item))


def list_mumu_instances(mgr: Path) -> List[JsonDict]:
    try:
        all_info = run_mumu_json(mgr, ["info", "-v", "all"], timeout=20.0)
    except Exception as exc:
        sys.exit(f"{CLR_A}failed to list MuMu instances: {exc}{CLR_RST}")
    instances: List[JsonDict] = []
    for index in sort_instance_ids([str(key) for key in all_info.keys()]):
        info = all_info.get(index, {})
        if isinstance(info, dict):
            item = dict(info)
            item["index"] = str(item.get("index", index))
            instances.append(item)
    if not instances:
        sys.exit(f"{CLR_A}no MuMu instances found.{CLR_RST}")
    return instances


def package_state_for_instance(mgr: Path, instance: str, packages: set) -> str:
    if not packages:
        return "-"
    try:
        app_info = read_installed_packages(mgr, instance)
    except Exception:
        return "unknown"
    installed = set(app_info.keys()) - {"active"}
    missing = sorted(packages - installed)
    return "installed" if not missing else "missing " + ",".join(missing)


def choose_mumu_instance(
    mgr: Path,
    profile: JsonDict,
    cli_instance: Optional[str] = None,
) -> str:
    if cli_instance:
        return str(cli_instance)

    mumu = get_section(profile, "mumu")
    apps = get_section(profile, "apps")
    configured = str(mumu.get("instance", "")).strip()
    prompt_instance = bool(mumu.get("prompt_instance", True))
    if not prompt_instance:
        return configured or "0"

    required_packages = set(apps.get("required_packages", []))
    instances = list_mumu_instances(mgr)
    preferred = ""

    print(f"{CLR_C}MuMu instances:{CLR_RST}")
    for item in instances:
        index = str(item.get("index", ""))
        name = str(item.get("name", ""))
        process_state = "started" if item.get("is_process_started") else "stopped"
        android_state = "android" if item.get("is_android_started") else "booting/off"
        package_state = package_state_for_instance(mgr, index, required_packages)
        if not preferred and package_state == "installed":
            preferred = index
        marker = "*" if configured and index == configured else " "
        print(f" {marker} [{index}] {name} | {process_state}, {android_state} | app: {package_state}")

    available_ids = {str(item.get("index", "")) for item in instances}
    if not preferred and configured in available_ids:
        preferred = configured
    if not preferred:
        preferred = str(instances[0].get("index", "0"))

    while True:
        try:
            answer = input(f"Select MuMu instance [{preferred}]: ").strip()
        except EOFError:
            answer = ""
        selected = answer or preferred
        if selected in available_ids:
            print(f"{CLR_C}selected MuMu instance: {selected}{CLR_RST}")
            return selected
        print(f"{CLR_A}unknown MuMu instance: {selected}{CLR_RST}")


def meter_to_deg(lat: float, dx: float, dy: float) -> Tuple[float, float]:
    d_lat = dy / 111_320
    d_lon = dx / (111_320 * math.cos(math.radians(lat)))
    return d_lat, d_lon


def offset_lon_lat(lon: float, lat: float, east_m: float, north_m: float) -> Tuple[float, float]:
    d_lat, d_lon = meter_to_deg(lat, east_m, north_m)
    return lon + d_lon, lat + d_lat


def random_range(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, list) and len(value) == 2:
        low, high = float(value[0]), float(value[1])
        if low > high:
            low, high = high, low
        return random.uniform(low, high)
    return float(value)


def random_int_range(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, list) and len(value) == 2:
        low, high = int(float(value[0])), int(float(value[1]))
        if low > high:
            low, high = high, low
        return random.randint(low, high)
    return int(float(value))


def max_int_value(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, list) and len(value) == 2:
        return max(int(float(value[0])), int(float(value[1])))
    return int(float(value))


def random_disk_offset(radius_m: float) -> Tuple[float, float]:
    if radius_m <= 0:
        return 0.0, 0.0
    angle = random.uniform(0.0, math.tau)
    radius = radius_m * math.sqrt(random.random())
    return math.cos(angle) * radius, math.sin(angle) * radius


def clamp(value: float, low: Optional[float], high: Optional[float]) -> float:
    if low is not None:
        value = max(value, low)
    if high is not None:
        value = min(value, high)
    return value


def segment_perpendicular_m(lon1: float, lat1: float, lon2: float, lat2: float) -> Tuple[float, float]:
    mid_lat = (lat1 + lat2) / 2
    east_m = (lon2 - lon1) * 111_320 * math.cos(math.radians(mid_lat))
    north_m = (lat2 - lat1) * 111_320
    length = math.hypot(east_m, north_m)
    if length <= 0:
        return 0.0, 0.0
    return -north_m / length, east_m / length


def weighted_choice(items: List[JsonDict]) -> Optional[JsonDict]:
    if not items:
        return None

    total = sum(max(0.0, float(item.get("weight", 0.0))) for item in items)
    if total <= 0:
        return random.choice(items)

    marker = random.uniform(0.0, total)
    upto = 0.0
    for item in items:
        upto += max(0.0, float(item.get("weight", 0.0)))
        if upto >= marker:
            return item
    return items[-1]


def build_runtime_route(route: Route, motion: JsonDict) -> Route:
    radius_m = float(motion.get("route_variation_radius_m", 0.0))
    preserve_first = bool(motion.get("preserve_first_route_point", True))
    varied_route: Route = []
    for index, (lon, lat) in enumerate(route):
        if preserve_first and index == 0:
            varied_route.append((lon, lat))
            continue

        east_m, north_m = random_disk_offset(radius_m)
        varied_route.append(offset_lon_lat(lon, lat, east_m, north_m))

    subdivide_points_value = motion.get("route_subdivide_points", 0)
    midpoint_radius_m = float(motion.get("route_midpoint_variation_radius_m", radius_m))
    max_subdivide_points = max(0, max_int_value(subdivide_points_value, 0))
    if max_subdivide_points <= 0:
        return varied_route

    runtime_route: Route = []
    for index, (lon1, lat1) in enumerate(varied_route):
        lon2, lat2 = varied_route[(index + 1) % len(varied_route)]
        runtime_route.append((lon1, lat1))
        subdivide_points = max(0, random_int_range(subdivide_points_value, max_subdivide_points))
        for step in range(1, subdivide_points + 1):
            ratio = step / (subdivide_points + 1)
            lon = lon1 + (lon2 - lon1) * ratio
            lat = lat1 + (lat2 - lat1) * ratio
            east_m, north_m = random_disk_offset(midpoint_radius_m)
            runtime_route.append(offset_lon_lat(lon, lat, east_m, north_m))
    return runtime_route


def set_location(
    mgr_path: Path,
    instance: str,
    lon: float,
    lat: float,
    jitter_m: float,
    drift_m: Tuple[float, float] = (0.0, 0.0),
) -> None:
    jitter_east_m, jitter_north_m = random_disk_offset(jitter_m)
    dx = drift_m[0] + jitter_east_m
    dy = drift_m[1] + jitter_north_m
    d_lat, d_lon = meter_to_deg(lat, dx, dy)
    result = subprocess.run(
        [
            str(mgr_path),
            "control",
            "-v",
            instance,
            "tool",
            "location",
            "-lon",
            f"{lon + d_lon:.6f}",
            "-lat",
            f"{lat + d_lat:.6f}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        print(f"{CLR_A}set_location failed, returncode={result.returncode}{CLR_RST}")


def click_icon(
    adb: Path,
    serial: Optional[str],
    image_dir: Path,
    screenshot_path: Path,
    image_name: str,
    threshold: float,
    offset: Tuple[int, int],
    long_press: bool,
    press_ms: int,
    debug: bool = False,
    log_misses: bool = False,
    do_click: bool = True,
) -> bool:
    if cv2 is None:
        sys.exit(f"{CLR_A}opencv-python is required. Install it with: python -m pip install opencv-python{CLR_RST}")

    with screenshot_path.open("wb") as fp:
        result = subprocess.run(adb_cmd(adb, serial, ["exec-out", "screencap", "-p"]), stdout=fp)
    if result.returncode != 0:
        print(f"{CLR_A}screencap failed, returncode={result.returncode}, serial={serial}{CLR_RST}")
        return False

    icon_path = image_dir / image_name
    screen = cv2.imread(str(screenshot_path))
    icon = cv2.imread(str(icon_path))
    if screen is None or icon is None:
        print(
            f"{CLR_A}image load failed: icon={icon_path.resolve()}, "
            f"icon_exists={icon_path.is_file()}, screenshot={screenshot_path.resolve()}, "
            f"screenshot_exists={screenshot_path.is_file()}{CLR_RST}"
        )
        return False

    res = cv2.matchTemplate(screen, icon, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if debug or log_misses:
        print(
            f"{CLR_C}[match] image={image_name} score={score:.3f} threshold={threshold:.3f} "
            f"loc=({loc[0]},{loc[1]}) screen={screen.shape[1]}x{screen.shape[0]} "
            f"icon={icon.shape[1]}x{icon.shape[0]}{CLR_RST}"
        )
    if score < threshold:
        return False

    x = loc[0] + icon.shape[1] // 2 + offset[0]
    y = loc[1] + icon.shape[0] // 2 + offset[1]
    if not do_click:
        print(f"{HEART}detected {icon_path} @ ({x},{y}), score={score:.3f}{CLR_RST}")
        return True

    input_args = (
        ["swipe", str(x), str(y), str(x), str(y), str(press_ms)]
        if long_press
        else ["tap", str(x), str(y)]
    )
    result = subprocess.run(adb_cmd(adb, serial, ["shell", "input"] + input_args))
    if result.returncode != 0:
        print(f"{CLR_A}input command failed, returncode={result.returncode}, image={image_name}{CLR_RST}")
        return False
    print(f"{HEART}clicked {icon_path} @ ({x},{y}), score={score:.3f}{CLR_RST}")
    return True


def print_status(elapsed: float, speed: float, total_dist: float, frame: int) -> None:
    if PrettyTable is None:
        avg_speed = total_dist / elapsed
        tick_hz = frame / elapsed
        print(f"time={elapsed:7.2f}s speed={speed:7.2f}m/s distance={total_dist:8.2f}m avg={avg_speed:7.2f}m/s tick={tick_hz:7.2f}Hz")
        return

    tbl = PrettyTable(["time", "speed", "distance", "avg_speed", "tick_hz"])
    tbl.add_row(
        [
            f"{CLR_P}{elapsed:7.2f}{CLR_RST}s",
            f"{CLR_P}{speed:7.2f}{CLR_RST}m/s",
            f"{CLR_P}{total_dist:8.2f}{CLR_RST}m",
            f"{CLR_P}{total_dist/elapsed:7.2f}{CLR_RST}m/s",
            f"{CLR_P}{frame/elapsed:7.2f}{CLR_RST}Hz",
        ]
    )
    print(tbl)


def geo_dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    mean_lat_rad = math.radians((lat1 + lat2) / 2.0)
    meters_per_lat_deg = 111_320.0
    meters_per_lon_deg = 111_320.0 * math.cos(mean_lat_rad)
    dx = (lon2 - lon1) * meters_per_lon_deg
    dy = (lat2 - lat1) * meters_per_lat_deg
    return math.hypot(dx, dy)


def route_length_m(route: Route) -> float:
    total = 0.0
    for index, (lon1, lat1) in enumerate(route):
        lon2, lat2 = route[(index + 1) % len(route)]
        total += geo_dist_m(lat1, lon1, lat2, lon2)
    return total


def read_route(profile: JsonDict) -> Route:
    motion = get_section(profile, "motion")
    route_raw = motion.get("route", [])
    if not isinstance(route_raw, list) or len(route_raw) < 2:
        sys.exit(f"{CLR_A}motion.route must contain at least two [lon, lat] points.{CLR_RST}")
    route: Route = []
    for item in route_raw:
        if not isinstance(item, list) or len(item) != 2:
            sys.exit(f"{CLR_A}each route point must be [lon, lat].{CLR_RST}")
        route.append((float(item[0]), float(item[1])))
    return route


def launch_emulator_and_app(
    mgr: Path,
    adb: Path,
    player: Optional[Path],
    instance: str,
    profile: JsonDict,
) -> Optional[str]:
    mumu = get_section(profile, "mumu")
    apps = get_section(profile, "apps")
    ui = get_section(profile, "ui")
    debug = bool(ui.get("debug", False))

    if debug:
        print(f"{CLR_C}[debug] manager={mgr}{CLR_RST}")
        print(f"{CLR_C}[debug] adb={adb}{CLR_RST}")
        print(f"{CLR_C}[debug] player={player if player else ''}{CLR_RST}")
        print(f"{CLR_C}[debug] instance={instance}{CLR_RST}")

    if bool(mumu.get("launch_player", True)):
        try:
            launch_result = run_mumu_json(mgr, ["control", "-v", instance, "launch"], timeout=20.0)
            if launch_result.get("errcode", 0) != 0:
                raise RuntimeError(json.dumps(launch_result, ensure_ascii=False))
            print(f"{CLR_P}starting MuMu instance {instance}: {mgr}{CLR_RST}")
        except Exception as exc:
            if player and player.is_file():
                subprocess.Popen([str(player)])
                print(f"{CLR_P}starting MuMu fallback: {player}{CLR_RST}")
                if debug:
                    print(f"{CLR_C}[debug] failed to launch instance with MuMuManager: {exc}{CLR_RST}")
            else:
                sys.exit(f"{CLR_A}failed to launch MuMu instance {instance}: {exc}{CLR_RST}")
        wait_for_mumu_process(
            mgr,
            instance,
            float(mumu.get("startup_wait_sec", 30.0)),
            float(mumu.get("startup_poll_sec", 1.0)),
            debug,
        )

    required_packages = set(apps.get("required_packages", []))
    poll_sec = float(apps.get("install_poll_sec", 2.0))
    wait_timeout = float(apps.get("install_wait_timeout_sec", 120.0))
    deadline = time.time() + wait_timeout
    next_notice = 0.0
    last_error = ""
    installed = set()

    while required_packages:
        try:
            app_info = read_installed_packages(mgr, instance)
            installed = set(app_info.keys()) - {"active"}
            if debug:
                active = app_info.get("active", "")
                print(f"{CLR_C}[debug] installed packages={sorted(installed)}, active={active}{CLR_RST}")
            if required_packages.issubset(installed):
                break
        except Exception as exc:
            last_error = str(exc)
            if debug:
                print(f"{CLR_C}[debug] app info failed for instance {instance}: {last_error}{CLR_RST}")
        if time.time() >= deadline:
            missing = ", ".join(sorted(required_packages - installed))
            matches = find_instances_with_packages(mgr, required_packages)
            hint = f" Found in MuMu instance(s): {', '.join(matches)}." if matches else ""
            suffix = f" Last error: {last_error}" if last_error else ""
            sys.exit(
                f"{CLR_A}required packages not found in MuMu instance {instance}: {missing}."
                f"{hint} Re-run and select the correct instance, or pass --instance <index>.{suffix}{CLR_RST}"
            )
        now = time.time()
        if now >= next_notice:
            missing = ", ".join(sorted(required_packages - installed))
            print(f"{CLR_C}waiting for packages in MuMu instance {instance}: {missing}{CLR_RST}")
            next_notice = now + max(10.0, poll_sec)
        time.sleep(poll_sec)

    info = run_mumu_json(mgr, ["info", "-v", instance], timeout=20.0)
    serial = f"{info['adb_host_ip']}:{info['adb_port']}"
    subprocess.run([str(adb), "connect", serial], stdout=subprocess.DEVNULL)
    print(f"{CLR_C}ADB connected: {serial}{CLR_RST}")
    if debug:
        print(f"{CLR_C}[debug] mumu info={json.dumps(info, ensure_ascii=False)}{CLR_RST}")
        print(f"{CLR_C}[debug] focus before launch:\n{read_current_focus(adb, serial)}{CLR_RST}")

    for package_name in apps.get("launch_packages", []):
        if debug:
            print(f"{CLR_C}[debug] MuMuManager launch package={package_name}{CLR_RST}")
        subprocess.Popen(
            [str(mgr), "control", "-v", instance, "app", "launch", "-pkg", str(package_name)]
        )
        time.sleep(float(apps.get("launch_wait_sec", 10.0)))

    launch_package = apps.get("launch_package")
    if launch_package:
        if debug:
            print(f"{CLR_C}[debug] adb monkey launch package={launch_package}{CLR_RST}")
        result = subprocess.run(
            adb_cmd(
                adb,
                serial,
                [
                    "shell",
                    "monkey",
                    "-p",
                    str(launch_package),
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                ],
            )
        )
        if debug:
            print(f"{CLR_C}[debug] monkey returncode={result.returncode}{CLR_RST}")
            print(f"{CLR_C}[debug] focus after launch:\n{read_current_focus(adb, serial)}{CLR_RST}")
    return serial


def action_offset(action: JsonDict) -> Tuple[int, int]:
    offset = action.get("offset", [0, 0])
    if not isinstance(offset, list) or len(offset) != 2:
        return (0, 0)
    return (int(offset[0]), int(offset[1]))


def run_action(action: JsonDict, ctx: JsonDict) -> bool:
    action_type = action.get("type", "click" if "image" in action else "")
    debug_log(ctx, f"run action type={action_type}, action={json.dumps(action, ensure_ascii=False)}")
    if action_type == "click":
        return click_icon(
            ctx["adb"],
            ctx["serial"],
            ctx["image_dir"],
            ctx["screenshot_path"],
            str(action["image"]),
            float(action.get("threshold", ctx["default_threshold"])),
            action_offset(action),
            bool(action.get("long_press", False)),
            int(action.get("press_ms", 2000)),
            bool(ctx.get("debug", False)),
            bool(ctx.get("log_misses", False)),
        )

    if action_type == "detect":
        return click_icon(
            ctx["adb"],
            ctx["serial"],
            ctx["image_dir"],
            ctx["screenshot_path"],
            str(action["image"]),
            float(action.get("threshold", ctx["default_threshold"])),
            action_offset(action),
            False,
            0,
            bool(ctx.get("debug", False)),
            bool(ctx.get("log_misses", False)),
            False,
        )

    if action_type == "loop_until":
        until = dict(action["until"])
        actions = action.get("actions", [])
        max_attempts = int(action.get("max_attempts", 0))
        attempts = 0
        debug_log(ctx, f"loop_until start until={until}, max_attempts={max_attempts}")
        while True:
            debug_log(ctx, f"loop_until attempt={attempts + 1}")
            if run_action(until, ctx):
                debug_log(ctx, f"loop_until matched after attempts={attempts + 1}")
                return True
            for item in actions:
                run_action(item, ctx)
            attempts += 1
            if max_attempts and attempts >= max_attempts:
                print(f"{CLR_A}loop_until failed after {attempts} attempts: {until}{CLR_RST}")
                return False
            time.sleep(float(action.get("delay_sec", ctx["tap_delay_sec"])))

    if action_type == "set_location":
        route = ctx["route"]
        index = int(action.get("route_index", 0))
        lon, lat = route[index % len(route)]
        repeat = int(action.get("repeat", 1))
        interval_sec = float(action.get("interval_sec", 0.2))
        debug_log(ctx, f"set_location route_index={index}, lon={lon}, lat={lat}, repeat={repeat}")
        for attempt in range(repeat):
            set_location(ctx["mgr"], ctx["instance"], lon, lat, ctx["jitter_m"])
            if attempt + 1 < repeat:
                time.sleep(interval_sec)
        return True

    if action_type == "sleep":
        debug_log(ctx, f"sleep seconds={action.get('seconds', 1.0)}")
        time.sleep(float(action.get("seconds", 1.0)))
        return True

    if action_type == "launch_app":
        package_name = str(action.get("package", ctx["launch_package"]))
        debug_log(ctx, f"launch_app package={package_name}")
        result = subprocess.run(
            adb_cmd(
                ctx["adb"],
                ctx["serial"],
                [
                    "shell",
                    "monkey",
                    "-p",
                    package_name,
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                ],
            )
        )
        debug_log(ctx, f"launch_app returncode={result.returncode}")
        return True

    print(f"{CLR_A}unknown action type: {action_type}{CLR_RST}")
    return False


def run_actions(actions: List[JsonDict], ctx: JsonDict) -> bool:
    for index, action in enumerate(actions, start=1):
        debug_log(ctx, f"action sequence index={index}/{len(actions)}")
        ok = run_action(action, ctx)
        if not ok and bool(action.get("required", True)):
            print(f"{CLR_A}required action failed at index={index}: {action}{CLR_RST}")
            return False
    return True


def simulate_walk(ctx: JsonDict) -> None:
    motion = get_section(ctx["profile"], "motion")
    route = build_runtime_route(ctx["route"], motion)
    speed_jitter_ratio = float(motion.get("speed_jitter_ratio", 0.20))
    speed_smoothing_ratio = float(motion.get("speed_smoothing_ratio", 0.25))
    speed_micro_jitter_ratio = float(motion.get("speed_micro_jitter_ratio", 0.0))
    speed_micro_jitter_smoothing_ratio = float(motion.get("speed_micro_jitter_smoothing_ratio", 0.35))
    speed_pause_chance_per_min = float(motion.get("speed_pause_chance_per_min", 0.0))
    speed_pause_duration = motion.get("speed_pause_duration_sec", [0.8, 2.0])
    tick_interval_sec = float(motion.get("tick_interval_sec", 0.40))
    tick_interval_jitter_ratio = float(motion.get("tick_interval_jitter_ratio", 0.0))
    distance_limit_m = float(motion.get("distance_limit_m", 16000))
    distance_scale = float(motion.get("distance_scale", 1.0))
    speed_update_interval = motion.get("speed_update_interval_sec", [2.5, 7.0])
    speed_modes = motion.get("speed_modes", [])
    min_speed_value = motion.get("min_speed_mps")
    max_speed_value = motion.get("max_speed_mps")
    min_speed = float(min_speed_value) if min_speed_value is not None else None
    max_speed = float(max_speed_value) if max_speed_value is not None else None
    route_drift_radius_m = float(motion.get("route_drift_radius_m", 0.0))
    route_drift_update_interval = motion.get("route_drift_update_interval_sec", [3.0, 8.0])
    route_drift_smoothing_ratio = float(motion.get("route_drift_smoothing_ratio", 0.25))
    gps_drift_radius_m = float(motion.get("gps_drift_radius_m", 0.0))
    gps_drift_update_interval = motion.get("gps_drift_update_interval_sec", [20.0, 60.0])
    gps_drift_smoothing_ratio = float(motion.get("gps_drift_smoothing_ratio", 0.06))

    def new_target_speed() -> Tuple[float, str, float]:
        base_speed_mps = random_range(motion.get("base_speed_mps", 4.5), 4.5)
        mode = weighted_choice(speed_modes) if isinstance(speed_modes, list) else None
        mode_name = "normal"
        multiplier = 1.0
        duration = random_range(speed_update_interval, 4.0)
        if mode:
            mode_name = str(mode.get("name", mode_name))
            multiplier = random_range(mode.get("multiplier", 1.0), 1.0)
            duration = random_range(mode.get("duration_sec", speed_update_interval), duration)
        speed = base_speed_mps * multiplier * random.uniform(1 - speed_jitter_ratio, 1 + speed_jitter_ratio)
        return clamp(speed, min_speed, max_speed), mode_name, duration

    idx, seg_dist, total_dist = 0, 0.0, 0.0
    t_start = t_prev = time.perf_counter()
    speed, speed_mode, speed_duration = new_target_speed()
    target_speed = speed
    next_speed_update = t_prev + speed_duration
    next_tick = t_prev + tick_interval_sec
    micro_speed_factor = 1.0
    route_drift_m = 0.0
    target_route_drift_m = random.uniform(-route_drift_radius_m, route_drift_radius_m) if route_drift_radius_m > 0 else 0.0
    next_route_drift_update = t_prev + random_range(route_drift_update_interval, 5.0)
    gps_drift_east_m, gps_drift_north_m = 0.0, 0.0
    target_gps_drift_east_m, target_gps_drift_north_m = random_disk_offset(gps_drift_radius_m)
    next_gps_drift_update = t_prev + random_range(gps_drift_update_interval, 40.0)
    pause_until = 0.0
    frame = 0

    debug_log(
        ctx,
        f"runtime_route_length={route_length_m(route):.2f}m, "
        f"route_points={len(route)}, route_variation_radius={motion.get('route_variation_radius_m', 0)}m, "
        f"route_drift_radius={route_drift_radius_m}m, "
        f"gps_drift_radius={gps_drift_radius_m}m, "
        f"initial_speed={speed:.2f}m/s, speed_mode={speed_mode}, speed_duration={speed_duration:.1f}s",
    )

    while True:
        now = time.perf_counter()
        if now < next_tick:
            time.sleep(next_tick - now)
            now = next_tick
        tick_factor = random.uniform(1 - tick_interval_jitter_ratio, 1 + tick_interval_jitter_ratio)
        next_tick += max(0.05, tick_interval_sec * tick_factor)

        dt = now - t_prev
        t_prev = now
        lon1, lat1 = route[idx]
        lon2, lat2 = route[(idx + 1) % len(route)]
        seg_len = geo_dist_m(lat1, lon1, lat2, lon2)

        if now >= next_speed_update:
            target_speed, speed_mode, speed_duration = new_target_speed()
            next_speed_update = now + speed_duration
            debug_log(ctx, f"new target_speed={target_speed:.2f}m/s, mode={speed_mode}, duration={speed_duration:.1f}s")
        speed += (target_speed - speed) * speed_smoothing_ratio
        if speed_micro_jitter_ratio > 0:
            micro_target = random.uniform(1 - speed_micro_jitter_ratio, 1 + speed_micro_jitter_ratio)
            micro_speed_factor += (micro_target - micro_speed_factor) * speed_micro_jitter_smoothing_ratio
        else:
            micro_speed_factor = 1.0
        move_speed = clamp(speed * micro_speed_factor, min_speed, max_speed)

        if pause_until <= now and speed_pause_chance_per_min > 0 and random.random() < speed_pause_chance_per_min * dt / 60.0:
            pause_until = now + max(0.0, random_range(speed_pause_duration, 1.0))
            debug_log(ctx, f"pause movement for {max(0.0, pause_until - now):.1f}s")
        if now < pause_until:
            move_speed = 0.0

        move = move_speed * dt * distance_scale
        seg_dist += move
        total_dist += move

        while seg_dist >= seg_len:
            seg_dist -= seg_len
            idx = (idx + 1) % len(route)
            lon1, lat1 = route[idx]
            lon2, lat2 = route[(idx + 1) % len(route)]
            seg_len = geo_dist_m(lat1, lon1, lat2, lon2)

        ratio = seg_dist / seg_len
        lon = lon1 + (lon2 - lon1) * ratio
        lat = lat1 + (lat2 - lat1) * ratio
        if route_drift_radius_m > 0:
            if now >= next_route_drift_update:
                target_route_drift_m = random.uniform(-route_drift_radius_m, route_drift_radius_m)
                next_route_drift_update = now + random_range(route_drift_update_interval, 5.0)
                debug_log(ctx, f"new route_drift={target_route_drift_m:.2f}m")
            route_drift_m += (target_route_drift_m - route_drift_m) * route_drift_smoothing_ratio
            east_unit, north_unit = segment_perpendicular_m(lon1, lat1, lon2, lat2)
            lon, lat = offset_lon_lat(lon, lat, east_unit * route_drift_m, north_unit * route_drift_m)
        if gps_drift_radius_m > 0:
            if now >= next_gps_drift_update:
                target_gps_drift_east_m, target_gps_drift_north_m = random_disk_offset(gps_drift_radius_m)
                next_gps_drift_update = now + random_range(gps_drift_update_interval, 40.0)
                debug_log(
                    ctx,
                    f"new gps_drift=({target_gps_drift_east_m:.2f}m, {target_gps_drift_north_m:.2f}m)",
                )
            gps_drift_east_m += (target_gps_drift_east_m - gps_drift_east_m) * gps_drift_smoothing_ratio
            gps_drift_north_m += (target_gps_drift_north_m - gps_drift_north_m) * gps_drift_smoothing_ratio
        set_location(ctx["mgr"], ctx["instance"], lon, lat, ctx["jitter_m"], (gps_drift_east_m, gps_drift_north_m))

        frame += 1
        elapsed = now - t_start
        os.system("cls" if os.name == "nt" else "clear")
        print(f"{HEART}running configured route{CLR_RST}")
        print_status(elapsed, move_speed, total_dist, frame)

        if total_dist >= distance_limit_m:
            print(f"{CLR_A}distance limit reached{CLR_RST}")
            break


def build_context(profile: JsonDict, profile_path: Path, cli_instance: Optional[str] = None) -> JsonDict:
    ui = get_section(profile, "ui")
    apps = get_section(profile, "apps")
    motion = get_section(profile, "motion")
    base_dir = profile_path.parent
    mgr, adb, player, _ = resolve_mumu_paths(profile, profile_path.parent)
    instance = choose_mumu_instance(mgr, profile, cli_instance)
    serial = launch_emulator_and_app(mgr, adb, player, instance, profile)
    image_dir = Path(ui.get("image_dir", "img"))
    if not image_dir.is_absolute():
        image_dir = base_dir / image_dir
    screenshot_path = Path(ui.get("screenshot_path", "screen.png"))
    if not screenshot_path.is_absolute():
        screenshot_path = base_dir / screenshot_path

    ctx = {
        "profile": profile,
        "mgr": mgr,
        "adb": adb,
        "serial": serial,
        "instance": instance,
        "route": read_route(profile),
        "image_dir": image_dir,
        "screenshot_path": screenshot_path,
        "tap_delay_sec": float(ui.get("tap_delay_sec", 1.0)),
        "default_threshold": float(ui.get("default_threshold", 0.75)),
        "jitter_m": float(motion.get("jitter_radius_m", 2.0)),
        "launch_package": apps.get("launch_package", ""),
        "debug": bool(ui.get("debug", False)),
        "log_misses": bool(ui.get("log_misses", ui.get("debug", False))),
    }
    debug_log(ctx, f"profile_path={profile_path}")
    debug_log(ctx, f"image_dir={image_dir.resolve()}, exists={image_dir.is_dir()}")
    debug_log(ctx, f"screenshot_path={screenshot_path.resolve()}")
    debug_log(
        ctx,
        f"route_points={len(ctx['route'])}, route_length={route_length_m(ctx['route']):.2f}m, "
        f"distance_limit={motion.get('distance_limit_m')}",
    )
    return ctx


def main() -> None:
    parser = argparse.ArgumentParser(description="Configurable MuMu location and UI automation runner.")
    parser.add_argument("profile", nargs="?", default=None, help="Path to profile JSON.")
    parser.add_argument("--instance", default=None, help="MuMu instance index. Skips interactive selection.")
    args = parser.parse_args()

    profile_path = Path(args.profile).resolve() if args.profile else Path(__file__).with_name("profile.example.json")
    profile = load_profile(profile_path)
    ctx = build_context(profile, profile_path, args.instance)

    ui = get_section(profile, "ui")
    if not run_actions(ui.get("pre_actions", []), ctx):
        sys.exit(f"{CLR_A}pre_actions failed; stop before simulate_walk.{CLR_RST}")
    simulate_walk(ctx)
    run_actions(ui.get("post_actions", []), ctx)


if __name__ == "__main__":
    main()
