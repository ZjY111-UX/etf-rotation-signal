# ETF 动量轮动信号

每个交易日 **北京时间 14:50**，自动比较 **上证指数(000001)**、**创业板50指数(399673)**、**国泰纳指ETF(513100)** 近 **20 个交易日**的涨跌幅，生成操作信号：

| 条件 | 信号 |
|---|---|
| 创业板50最大，领先纳指ETF ≥ 0.1%，且涨幅 > 0 | **买入创业板50** |
| 纳指ETF最大，领先创业板50 ≥ 0.1%，且涨幅 > 0 | **买入纳指国泰ETF** |
| 其他所有情况（上证最强 / 差距不足0.1% / 涨幅为负） | **空仓** |

统计完成后自动：
1. 📧 把近 20 日涨跌明细 + 信号结果 **邮件发送** 给你
2. 🌐 更新 **GitHub Pages 网站**（含累计涨跌对比、走势图、每日明细、历史信号）

全部跑在 GitHub Actions 上，**完全免费，无需服务器**。

---

## 部署步骤（约 10 分钟）

### 第 1 步：创建 GitHub 仓库并推送代码

1. 登录 GitHub → 右上角 `+` → **New repository**
   - 仓库名随意，例如 `etf-rotation-signal`
   - 选 **Public**（Private 也可以，但 GitHub Pages 免费版需要 Public）
   - 不要勾选任何初始化选项，直接 **Create repository**
2. 在本项目文件夹打开终端，执行（把 `你的用户名` 换成实际的）：

```bash
git init
git add .
git commit -m "init: ETF rotation signal"
git branch -M main
git remote add origin https://github.com/你的用户名/etf-rotation-signal.git
git push -u origin main
```

### 第 2 步：获取 QQ 邮箱 SMTP 授权码

1. 网页登录 [QQ邮箱](https://mail.qq.com) → **设置** → **账号**
2. 找到「POP3/IMAP/SMTP…服务」→ 开启 **SMTP 服务**
3. 按提示短信验证后，会得到一个 **16 位授权码**（注意：不是QQ密码），复制保存

### 第 3 步：配置 GitHub Secrets

进入你的仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，依次添加 3 个：

| Name | Value（示例） |
|---|---|
| `MAIL_USER` | `12345678@qq.com`（发件QQ邮箱） |
| `MAIL_PASS` | `abcdabcdabcdabcd`（16位SMTP授权码） |
| `MAIL_TO` | `12345678@qq.com`（收件邮箱，可以填自己；多个用英文逗号分隔） |

### 第 4 步：开启 GitHub Pages

仓库 → **Settings** → **Pages**：
- Source 选 **Deploy from a branch**
- Branch 选 `main`，目录选 **`/docs`** → **Save**

约 1 分钟后网站地址为：`https://你的用户名.github.io/etf-rotation-signal/`

### 第 5 步：手动触发一次测试

仓库 → **Actions** → 左侧 **ETF Rotation Signal** → 右侧 **Run workflow** → **Run workflow**

- 手动触发为“强制模式”：即使是周末也会按最近一个交易日统计并发邮件，方便验证
- 运行成功后：检查邮箱是否收到邮件、网站是否显示数据

---

## 运行机制说明

- **定时**：工作流在 UTC 06:35（北京 14:35）触发。GitHub 定时任务常有 5~20 分钟排队延迟，脚本会**等到 14:50 才抓数据**，保证统计时点准确；若触发已晚于 14:50 则立即执行。
- **交易日判断**：抓取上证指数日K，若最后一根K线不是当天 → 判定为节假日/周末，自动跳过（法定节假日不会误发）。
- **涨跌幅口径**：当日 14:50 现价 ÷ 20 个交易日前的收盘价 − 1。ETF 使用前复权价，消除分红除权的影响。
- **数据源**：东方财富行情接口，失败自动切换腾讯行情接口互备。
- **数据落盘**：每次运行把 `docs/data.json` 提交回仓库，网站读取它渲染页面，并保留最近 60 个信号的历史记录。

## 本地调试

```bash
pip install -r requirements.txt
python main.py --no-mail --no-wait --force   # 不发邮件、不等14:50、非交易日也强制统计
```

发邮件测试（临时设置环境变量）：

```bash
export MAIL_USER=xxx@qq.com MAIL_PASS=授权码 MAIL_TO=xxx@qq.com
python main.py --no-wait --force
```

## 免责声明

数据来自第三方公开接口，可能存在延迟或错误；本项目仅为技术演示，不构成任何投资建议。
