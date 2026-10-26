# appshelf

AltStore / SideStore 软件源生成器。从公开 AList 接口读取文件列表，远程解析 IPA 元数据，生成标准 `source.json`。

## 源地址

```text
https://honlam6.github.io/appshelf/source.json
```

AltStore / SideStore → Sources → `+` → 粘贴地址。

网页预览：

```text
https://honlam6.github.io/appshelf/
```

## 结构

```text
source.json                生成的软件源
index.html                 网页预览（目录浏览 / 搜索 / 下载）
alist_source.py            生成器
.cache/ipa.json            IPA 元数据缓存，加速增量更新
.github/workflows/update.yml  每天自动重扫
```

## 本地生成

```bash
export ALIST_BASE="https://your-alist-host"
python3 alist_source.py \
  --path "/苹果应用 - Appstore apps" \
  --path "/巨魔商店 - TrollStore" \
  --depth 2 --limit 300 --workers 6 \
  --out source.json --cache .cache/ipa.json
```

## 自动更新

GitHub Actions 每天 03:00 UTC 运行，只解析新增 IPA（缓存命中则跳过），提交新的 `source.json`。仓库 Secrets 里需要 `ALIST_BASE`。

## 说明

- `downloadURL` 指向源站的 `/d/<path>`，安装时由源站返回 302 到对象存储签名地址，文件不经过本仓库。
- 本仓库只托管 JSON 和预览页，不存 IPA、不中转流量。
- 源依赖源站可用性；源站改路径或关站时源会失效。
