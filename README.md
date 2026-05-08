# 妈妈的每日理财资讯早报

每天早上围绕 `watchlist.yaml` 里的 A 股关注清单，生成一份长辈友好的网页早报。页面优先展示最近交易日行情、重点留意和大盘参考，新闻作为补充；可以部署成固定链接，并添加到妈妈手机桌面，看起来像一个小 App。

## 快速开始

1. 修改 `watchlist.yaml`，把示例标的换成妈妈实际关注的股票或基金。
2. 本地预览：

```bash
python3 digest.py --preview
```

如果希望看到真实行情，先安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

3. 生成网页：

```bash
python3 digest.py --site
```

生成结果在 `site/index.html`，同时会生成 `manifest.webmanifest` 和 `icon.svg`，用于手机桌面图标。

4. 可选：配置微信推送：

```bash
cp .env.example .env
```

然后在 `.env` 里填入 `PUSHPLUS_TOKEN`。

5. 发送测试：

```bash
python3 digest.py --send
```

## 内容原则

- 只整理公开资讯，不写买入、卖出、加仓、减仓建议。
- 优先展示最近交易日的价格、涨跌幅、成交额、换手率和大盘参考。
- 用“发生了什么、为什么相关、可以留意什么”的方式解释消息。
- 如果当天没有明显相关新闻，也会继续展示行情观察，避免页面没有实际内容。

## 手机桌面图标

把 `site/` 部署到 GitHub Pages、Netlify、Vercel 或自己的服务器后，用妈妈手机打开那个固定链接：

- iPhone：Safari 打开网页，点分享按钮，选择“添加到主屏幕”。
- Android：Chrome 打开网页，点菜单，选择“添加到主屏幕”或“安装应用”。

之后妈妈点桌面上的“理财早报”图标，就会看到最新生成的内容。

## 发布到 GitHub Pages

这个项目已经带了 GitHub Pages 工作流：`.github/workflows/pages.yml`。推送到 GitHub 后，它会：

- 每次推送到 `main` 自动重新生成网页。
- 每天北京时间 8:00 自动重新生成网页。
- 把 `site/` 发布成 GitHub Pages。

建议创建一个随机难猜的私用仓库名，例如：

```text
morning-note-a8k3q9
```

发布步骤：

1. 在 GitHub 新建一个仓库，仓库名使用随机难猜名称。
2. 把本地项目推送到这个仓库。
3. 打开 GitHub 仓库的 `Settings` → `Pages`。
4. 在 `Build and deployment` 里把 `Source` 设为 `GitHub Actions`。
5. 到 `Actions` 页面手动运行一次 `Build and publish finance digest`。
6. 运行成功后，GitHub 会给出一个固定链接，发给妈妈即可。

注意：GitHub Pages 默认是公网可访问。随机链接能降低被别人偶然发现的概率，但不是严格密码保护。

## 可选增强

- 安装依赖后，`PyYAML` 会用于更完整地读取 YAML。
- 安装 `akshare` 后，会尝试拉取 A 股行情和主要指数表现。
- 配置 `TUSHARE_TOKEN` 并安装 `tushare` 后，会额外尝试从 Tushare 新闻接口补充资讯。
