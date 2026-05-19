# HustRunner

## 项目简介

HustRunner 是一个基于 MuMu 模拟器的校园跑自动化脚本。脚本通过 MuMuManager 向模拟器持续写入虚拟定位，并结合 ADB 截图和 OpenCV 模板匹配完成 App 内的开始、暂停检测、结束等按钮操作。

主要能力：

- 按配置路线循环模拟定位，支持速度随机、路径轻微扰动和定位抖动。
- 通过图片模板自动点击跑步流程中的关键按钮。
- 达到配置距离后执行结束流程。

> 仅建议在已获授权的测试、调试或自动化验证环境中使用。使用者需要自行确认符合学校、平台和软件服务条款。

## 快速开始

参阅QUICKSTART.md

## 项目依赖

### 运行环境

- Windows
- Python 3.8 或更高版本
- MuMu Player 12
- 目标 Android App 已安装到 MuMu 实例中

### Python 依赖

记录在 `requirements.txt`：

### 工程文件

```text
.
├── main.py              # 主程序入口
├── profile.json         # 默认使用的运行配置
├── requirements.txt     # Python 依赖
├── .mumu_paths.json     # MuMu 路径缓存，首次发现后生成或更新
└── img/                 # App 按钮截图模板
    ├── pre1.png
    ├── pre2.png
    ├── pre3.png
    ├── pre4.png
    ├── pause.png
    ├── stop.png
    └── end.png
```

## 配置说明

### MuMu 配置

`mumu` 段控制模拟器实例、路径发现和启动等待：

```json
{
  "mumu": {
    "instance": "",
    "prompt_instance": true,
    "manager_path": "",
    "adb_path": "",
    "player_path": "",
    "cache_path": ".mumu_paths.json",
    "launch_player": true,
    "startup_wait_sec": 60
  }
}
```

当 `manager_path`、`adb_path`、`player_path` 为空时，脚本会在常见安装目录中查找，并把结果缓存到 `.mumu_paths.json`。

### 路线与运动参数

当前默认使用 `track_400m` 跑道模式。`motion.track_points` 需要按顺时针跑法给出 6 个控制点，坐标顺序固定为 `[经度, 纬度]`：

1. 直道起点
2. 直道中点，也就是第一段弯道起点
3. 第一段弯道中点
4. 第一段弯道终点，也就是第二段直道起点
5. 第二段直道终点，也就是第二段弯道起点
6. 第二段弯道中点

脚本会把这 6 个点生成两段直道和两段圆弧弯道，最后自动从第 6 个点所在弯道回到第 1 个点。

```json
{
  "motion": {
    "route_mode": "track_400m",
    "track_straight_sample_points": 10,
    "track_curve_sample_points": 18,
    "track_points": [
      [114.437553, 30.519142],
      [114.437414, 30.518185],
      [114.437742, 30.517800],
      [114.438213, 30.518108],
      [114.438339, 30.519045],
      [114.437984, 30.519442]
    ]
  }
}
```

如果需要回到旧的折线路线模式，可以把 `route_mode` 改成 `route`，并继续使用 `motion.route`。

常用参数：

- `base_speed_mps`：基础速度，支持固定值或 `[最小值, 最大值]`。
- `speed_modes`：慢速、正常、快速等配速模式及权重。
- `min_speed_mps` / `max_speed_mps`：速度上下限。
- `speed_micro_jitter_ratio`：每次定位周期内的细微速度波动。
- `speed_pause_chance_per_min` / `speed_pause_duration_sec`：低概率短暂停顿。
- `tick_interval_sec`：定位广播间隔。
- `jitter_radius_m`：每次下发定位时的小幅独立抖动半径，不宜过大。
- `track_straight_sample_points`：每段直道内部插入的采样点数量。
- `track_curve_sample_points`：每段弯道内部插入的采样点数量，越大弯道越平滑。
- `route_variation_radius_m`：运行前对路线点做轻微偏移。跑道模式下建议保持 `0`，避免圆弧被打散。
- `route_subdivide_points`：在路线点之间插入随机中间点，支持固定值或 `[最小值, 最大值]`。跑道模式下建议保持 `0`。
- `route_drift_radius_m`：行进时沿当前路段法线方向产生平滑横向漂移。
- `gps_drift_radius_m`：模拟 GPS 信号的慢速整体漂移，比高频抖动更接近真实轨迹。
- `distance_scale`：距离倍率；如果 App 记录距离偏大，可略低于 `1.0`。
- `distance_limit_m`：模拟距离达到该值后结束。

### UI 自动化配置

`ui.image_dir` 指向按钮模板目录。截图模板需要尽量与运行时的模拟器分辨率、缩放比例、App 主题颜色保持一致。

支持的动作类型：

- `click`：查找图片并点击。
- `detect`：只检测图片，不点击。
- `loop_until`：循环执行动作，直到目标动作成功。
- `set_location`：把定位设置到路线中的某个点。
- `sleep`：等待指定秒数。
- `launch_app`：通过 ADB monkey 启动指定包名。

点击动作示例：

```json
{
  "type": "click",
  "image": "pre1.png",
  "threshold": 0.75,
  "offset": [0, 0],
  "long_press": false
}
```

循环等待示例：

```json
{
  "type": "loop_until",
  "until": {
    "type": "detect",
    "image": "pause.png",
    "threshold": 0.7
  },
  "actions": [
    {
      "type": "set_location",
      "route_index": 0,
      "repeat": 3,
      "interval_sec": 0.4
    }
  ],
  "delay_sec": 2,
  "max_attempts": 8
}
```

## 调试方式

如果脚本无法点击按钮或无法进入跑步状态，可以先打开调试输出：

```json
{
  "ui": {
    "debug": true,
    "log_misses": true
  }
}
```

调试模式会输出 MuMu/ADB 路径、ADB serial、前台窗口、图片路径、模板匹配分数和动作循环次数。

常见排查方向：

- `required packages not found`：目标 App 未安装，或包名配置不正确。
- `Found in MuMu instance(s): ...`：App 装在其他模拟器实例中，重新运行时选择提示的编号，或用 `--instance` 指定。
- 图片匹配失败：重新截取按钮模板，确认分辨率、主题和缩放一致。
- ADB 连接异常：确认 MuMu 已启动，或手动填写 `mumu.adb_path`。
- 定位未生效：增加 `set_location.repeat`，或延长 App 启动后的等待时间。
- 距离偏差：优先检查路线点和 App 采样频率，必要时再微调 `distance_scale`。

## 注意事项

- 不要在多个模拟器实例或实体设备同时连接时混用 ADB；脚本会优先使用 MuMuManager 返回的 `adb_host_ip:adb_port`。
- `pre_actions` 中默认动作是必需动作，失败后脚本会停止，不会继续模拟跑步。
- `post_actions` 用于结束流程，当前顺序是点击 `stop.png` 后再点击 `end.png`。
- 路线点建议覆盖完整闭环，避免长时间在单一路段来回跳动。
- 所有坐标和速度配置都应根据实际测试结果逐步调整。
- 如果发生 ** 数据异常 ** 情况，请尝试让AI帮助你增加路径的随机性即可
