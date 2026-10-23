# Steam 壁纸设置

这份说明写给拿到安装包、想用 Steam 壁纸的人。

## 先说清楚三件事

1. **安装包里没有 Steam，也没有 Wallpaper Engine。** 每日壁纸只负责每天挑一张、自动换上，Steam 和壁纸引擎要你自己装。
2. **Wallpaper Engine 是付费软件，约 ¥19，必须用自己的 Steam 账号购买。** 不能把别人的安装目录拷过来用，那是盗版，也违反 Steam 用户协议。
3. 程序会自动读取你电脑上 Steam 的安装位置和已订阅的创意工坊内容，通常**不需要你手填任何路径**。

---

## 需要准备

- 一个 Steam 账号（注册免费）
- Wallpaper Engine（Steam 商店应用 ID `431960`，约 ¥19）
- 一个能让 Steam 商店和创意工坊打开的加速工具（中国大陆需要）

---

## 第一步：让 Steam 商店和创意工坊能打开

国内直连时，Steam 商店、社区、创意工坊经常打不开。下面两种任选一种，**不需要都装**：

- **Watt Toolkit（原名 Steam++）**：免费开源，装好后勾选“商店加速 / 社区加速”，保持它后台运行即可。
- **游戏加速器**（UU、雷神等）：在加速列表里选 Steam 商店或 Steam 社区。

确认标准：用浏览器能打开下面两个网址，就算通了。

- `https://store.steampowered.com/`
- `https://steamcommunity.com/`

> 注意：普通代理（Clash、Pigeon 等）对 Steam 登录和商店经常无效，需要的是上面这种专门的 Steam 加速，或者 TUN 全局模式。

---

## 第二步：购买并安装 Wallpaper Engine

1. 打开 Steam → 商店 → 搜索 **Wallpaper Engine**
2. 购买（约 ¥19）并安装
3. 启动一次，确认桌面上出现了动态壁纸
4. 之后可以关掉窗口，程序能自己调起它

---

## 第三步：订阅创意工坊壁纸

1. Steam 库 → Wallpaper Engine → **创意工坊**
2. 挑喜欢的壁纸，点 **订阅**
3. 等 Steam 右下角的下载队列走完
4. 订阅内容会保存在 `steamapps\workshop\content\431960`

订阅得越多，每日壁纸的候选越丰富。没订阅也能用，但只能换静态图。

---

## 第四步：安装每日壁纸

1. 双击 `DailyWallpaper-Setup-x.x.x.exe`，**不需要管理员权限**
2. 首次启动填智谱 API key（`open.bigmodel.cn` 免费申请）
3. 首次运行的环境检查会直接告诉你：
   - 有没有找到 Wallpaper Engine
   - Steam 商店和社区能不能打开
   - 创意工坊订阅了几项
4. 正常情况下路径已经自动填好，直接用即可

---

## 没自动识别怎么办

打开程序的 **设置页 → 检测 Steam 环境**：

- 显示“已找到 Wallpaper Engine” → 点 **保存** 写入配置
- 仍然找不到 → 先在 Steam 里启动一次 Wallpaper Engine，再点一次检测
- 还不行 → 手动编辑配置（`%LOCALAPPDATA%\DailyWallpaper\config.toml`）：

```toml
[wallpaper_engine]
install_dir = "D:/games/steam/steamapps/common/wallpaper_engine"
config_path = "D:/games/steam/steamapps/common/wallpaper_engine/config.json"
```

路径换成你自己 Steam 库里的实际位置。

> 只想自己指定路径、完全不让程序自动探测，可以在 `[wallpaper_engine]` 里加
> `auto_detect = false`。关闭后程序不会改动任何 Steam 相关配置，本地创意工坊源
> 也要你手动在 `[sources]` 里设 `we_workshop_enabled = true`。

---

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 商店打不开 | 加速工具没开，或开了但没勾选 Steam 商店 |
| 创意工坊能看、下载很慢 | 加速器里选“创意工坊下载”线路，或换个时段 |
| 提示“没找到 Wallpaper Engine” | 先在 Steam 启动一次 WE，再重启本程序 |
| 动态壁纸不换 | 默认只有插电时才换（电池保护）；要改就编辑 config.toml 的 `dynamic_requires_ac = false` |
| 动态壁纸没声音 | 默认静音；托盘菜单“壁纸声音”打开 |
| 换了壁纸但评分对不上 | 程序读的是 WE 当前实际状态，以桌面上的为准 |

---

## 不要做的事

- 不要把别人的 Wallpaper Engine 安装目录或工坊内容打包分享，那属于盗版分发。
- 不要把 Steam 账号密码交给任何人或填进任何“加速工具”的第三方页面。
- 不要因为某工具说“一键解锁全部付费壁纸”就去下载，几乎都是盗号或木马。
