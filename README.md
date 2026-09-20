# China Editor v2.1.3

一个现代化的文本编辑器，支持 Windows、Linux 和 macOS。

**开发者：** CaoYiLi-China

## 功能特性

- 轻量快速
- 语法高亮
- 跨平台支持（Windows / Linux / macOS）
- 简单的安装和卸载

## 下载

### Windows
- [install.exe](https://github.com/CaoYiLi-China/China-Editor-2/releases/latest/download/install.exe) - 安装程序
- [Uninstall.exe](https://github.com/CaoYiLi-China/China-Editor-2/releases/latest/download/Uninstall.exe) - 卸载程序

### Linux
```bash
# 下载并安装
tar -xzf China-Editor-2-linux.tar.gz
cd China-Editor-2
sudo ./installer.sh

# 卸载
sudo ./uninstaller.sh
```

### macOS
```bash
# 下载并安装
tar -xzf China-Editor-2-macos.tar.gz
cd China-Editor-2
./installer.sh

# 卸载
./uninstaller.sh
```

## 从源码构建

### Windows
```bash
# 需要 MinGW-w64
build-all.bat
```

### Linux
```bash
# 需要 gcc
cd packages/linux
sudo ./build.sh
```

### macOS
```bash
# 需要 Xcode Command Line Tools
cd packages/macos
./build.sh
```

## 项目结构

```
China Editor 2/
├── Source_code/        # 应用程序源代码
├── bin/                # 编译后的程序
├── install/            # Windows 安装程序源码
├── Uninstall/          # Windows 卸载程序源码
├── packages/           # 跨平台包
│   ├── linux/
│   └── macos/
└── dist/               # 分发文件
```

## 许可证

MIT 许可证

## 联系方式

caoyili2028@outlook.com

China团队@2026

