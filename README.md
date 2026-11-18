# 机场3（FreeProxyHub）

基于 FreeProxyHub 的 GitHub Actions 订阅聚合项目：从 GitHub 搜索公开代理源，解析并验证后生成 Clash/Mihomo 等订阅。

> 这是 GitHub 云端 runner 检测版，不包含上游作者 G2532 设备的境内本地验证，也不代表你的手机网络可用。免费节点的运营者和流量处理方式未知，请勿用于银行、邮箱等敏感登录。

## 订阅

| 格式 | 精选节点（最多 30 个） | 全量节点 |
|---|---|---|
| Clash / Mihomo | [clash-selected.yaml](https://chaoslex.github.io/airport3/clash-selected.yaml) | [clash.yaml](https://chaoslex.github.io/airport3/clash.yaml) |
| Shadowrocket 等 | [sub-selected.b64](https://chaoslex.github.io/airport3/sub-selected.b64) | [sub.b64](https://chaoslex.github.io/airport3/sub.b64) |
| 文本 | [sub.txt](https://chaoslex.github.io/airport3/sub.txt) | [sub.json](https://chaoslex.github.io/airport3/sub.json) |

订阅每次 Actions 成功部署后更新；GitHub Pages 或节点上游不可用时可能暂时无法刷新。导入后仍请在自己的客户端检查实际连通性。

## 自动运行

- `机场3 Full Update (Crawl + Validate)`：每 6 小时全量发现、验证、生成并部署；也可在 GitHub Actions 中手动运行。
- `机场3 Quick Validate (精选更新)`：每小时复测已有候选并刷新订阅。
- 全量验证要求 mihomo 真实协议检查通过；若 runner 无法安装 mihomo 或没有合格节点，本轮可能失败，不应把旧文件当作本轮新结果。

## 手动运行

进入仓库的 Actions，选择 `机场3 Full Update (Crawl + Validate)`，点击 `Run workflow`。订阅地址在该次运行和 Pages 部署成功后可用。

## 本地运行

```bash
pip install -r requirements.txt
python cli.py --require-real-check
```

仅用于个人试验；运行结果受测试出口网络影响。项目代码及其许可证以仓库内文件和上游仓库说明为准。
