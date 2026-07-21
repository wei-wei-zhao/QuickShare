# QuickShare（Phone → PC Sync）

局域网扫码传文件：手机/电脑同一 WiFi，扫码即可把截图、图片、文件传到电脑。

## 分支说明

| 分支 | 内容 | 给谁用 |
|------|------|--------|
| **master** | 安装包 / 便携版（可直接下载） | 普通用户 |
| **dev** | 源代码 | 开发者 |

> 普通用户请到 **master** 下载，不要拉 `dev`。

## 开发（dev）

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

依赖默认走清华 PyPI 镜像。产物在 `release/`，请推到 **master**。

## 功能概览

- 扫码上传、异步多文件
- 电脑面板预览、自选保存目录
- WiFi 一键连网二维码（可选）
- 电脑互传、局域网扫描
- Windows 安装包（可选安装路径）

## License

自用 / 按仓库所有者约定。
