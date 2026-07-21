# QuickShare（Phone → PC Sync）

局域网扫码传文件：手机/电脑同一 WiFi，扫码即可把截图、图片、文件传到电脑。

## 分支说明（请按用途使用）

| 分支 | 内容 | 谁用 |
|------|------|------|
| **master** | 仅安装包下载（Setup / Portable） | **对外用户只看这个** |
| **main** | 开发源码 | 开发 |
| **dev** | 开发源码（与 main 同步） | 开发 |

> 普通用户请打开仓库默认页或 `master` 分支下载 zip，**不要**使用 `main` / `dev`。  
> `main` 与 `dev` 都是源码分支，保持同步；**不要**把它们合并进 `master`。

## 开发（main / dev）

```bash
cd receiver
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
python app.py
```

浏览器打开：`http://127.0.0.1:8765/pc`

### 重新打包安装包

```bash
cd receiver
打包成安装包.bat
```

产物在本地 `release/`，更新对外下载时请只推送到 **master**（不要把源码推进 master）。

## 功能概览

- 扫码上传、异步多文件
- 电脑面板预览、自选保存目录
- WiFi 一键连网二维码（可选）
- 电脑互传、局域网扫描
- Windows 安装包（可选安装路径）
