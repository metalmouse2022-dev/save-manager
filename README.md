# 存档管家 (Save Manager)

轻量级 Windows 桌面工具，用于发现、归类和打开 PC 游戏存档目录。适用于 Windows 11，基于 Python 标准库 Tkinter 构建。

**当前版本：1.0.0** · **平台：Windows 11 x64**

## 下载与启动

从 GitHub Releases 下载 `存档管家-v1.0.0.exe`，并与 `存档管家.ini`、`存档管家图标.png` 放在同一目录，然后双击运行。exe 为单文件应用，无需安装 Python；首次联网检索游戏封面时需要网络连接。游戏封面缓存保存在旁边的 `icon` 文件夹。

也可下载源码运行：安装 Python 3.10 或更新版本（需包含 Tcl/Tk），在此目录执行：

```powershell
py 存档管家.py
```

## 功能

- 扫描常见 Windows 游戏存档位置，包括 Roaming、Local、LocalLow、Documents、Saved Games、Steam userdata、Steam/RUNE、GSE Saves 和自选目录。
- 根据目录名、上层目录名和 Steam AppID 识别游戏；合并同一游戏的多个存档位置。
- 尝试通过 Steam 商店或 Wikipedia 匹配名称与封面；无法确定的条目归入“未知”。
- 按最近修改时间排序，查看运行时间、存档目录，并通过“打开”跳转到对应位置。
- 将选中的存档目录移动到 Windows 回收站；扫描整理期间显示进度并锁定操作。
- 可调整字号、拖动列表与详情栏分隔线；界面状态保存在程序旁的 `存档管家.ini`。

## 数据与隐私

扫描仅用于本机展示。运行时间从首次运行本软件后按检测到的游戏进程开始累计，数据保存在 `%LOCALAPPDATA%\SaveLibrary\library.json`。名称/封面联网匹配会向 Steam 或 Wikipedia 查询目录名或 AppID；匹配得到的图片缓存于程序目录的 `icon` 文件夹。

扫描范围采用常见目录规则，可能漏掉特殊安装位置。可通过“添加存档目录”补充路径；删除操作会显示确认并调用 Windows 回收站。

## 发布文件

- `存档管家-v1.0.0.exe`：Windows 11 x64 单文件程序
- `存档管家.py`：Python 源码
- `存档管家图标.png`、`存档管家.ico`：应用图标
- `使用说明.md`：中文详细说明

应用与界面状态文件请放在同一目录。不要将个人的 `存档管家.ini` 或下载后的 `icon/` 游戏封面缓存提交到仓库。

