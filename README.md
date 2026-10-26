# qlam-app-source

基于 `pan.qlam.cc` 公开 AList 接口生成的 AltStore / SideStore 软件源。

## 使用

源地址：

```text
https://honlam6.github.io/qlam-app-source/source.json
```

AltStore / SideStore → Sources → `+` → 粘贴上面的地址。

网页预览：

```text
https://honlam6.github.io/qlam-app-source/
```

## 工作原理

- `source.json` 由 `alist_source.py` 生成，只包含元数据和下载路径。
- `downloadURL` 指向 `https://pan.qlam.cc/d/<path>`，安装时由 AList 返回 302 到对象存储签名地址，文件不经过本仓库。
- 本仓库只托管一个 JSON 和预览页，不存 IPA、不中转流量。

## 自动更新

`.github/workflows/update.yml` 每天 03:00（UTC）重新扫描并解析新增 IPA，提交新的 `source.json`。也可以在 Actions 页面手动触发。

## 本地生成

```bash
python3 alist_source.py \
  --path "/苹果应用 - Appstore apps" \
  --path "/巨魔商店 - TrollStore" \
  --depth 2 --limit 300 --workers 6 \
  --out source.json --cache .cache/ipa.json
```

## 说明

- 源依赖原站可用性；原站改路径或关站时源会失效。
- 图标目前统一使用站点 logo，替换真实图标需要从 IPA 提取 `AppIcon` 并自行托管。
