# 文件版本与配置恢复需求

本文件保留自 `Ezio2000/file-version-manager` 的需求与结构草案，供 hm 后续评估配置快照、差异查看和恢复能力。这里只记录需求，不表示 hm 已实现这些功能。

来源：`Ezio2000/file-version-manager`，原默认分支 `master`。保留文件：`README.md`、`STRUCTURE.md`。

## 原 README

# file-version-manager
A project for managing versions of JSON and Markdown files with diff, restore, and history tracking capabilities

## 原结构草案

以下目录仅是原项目设计；迁移时原仓库没有对应源码或测试。

# DV Project Structure

## Directories

- `/bin` - CLI executable
- `/src` - Source code
- `/tests` - Test files
- `/docs` - Documentation

## Files

- `README.md` - Project overview
- `package.json` - Project metadata and dependencies
- `.gitignore` - Git ignore rules
