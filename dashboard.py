import streamlit as st

from dashboard_data import (backtest_state, carry_state, data_health, gmgn_state,
                            governance_state, paper_state, polymarket_state, replay_state)

st.set_page_config(page_title="加密量化监控台", page_icon="CQ", layout="wide")
st.markdown(
    """
    <style>
    [data-testid="stMetric"] {
        min-width: 0;
    }
    [data-testid="stMetricValue"] {
        font-size: 1.35rem;
        line-height: 1.2;
        white-space: normal;
        overflow: visible;
        text-overflow: clip;
        overflow-wrap: anywhere;
    }
    [data-testid="stMetricLabel"] {
        white-space: normal;
        overflow-wrap: anywhere;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

STATUS = {"flat": "空仓", "open": "持仓", "halt": "暂停", "unavailable": "不可用"}
ACTION = {"hold": "持有", "open": "开仓", "close": "平仓", "BUY": "买入", "SELL": "卖出", "hold(dust)": "持有（小额跳过）"}


def usd(value):
    return f"${float(value or 0):,.2f}"


def pct(value):
    return f"{float(value or 0):+.2%}"


def status(value):
    return STATUS.get(value, value or "不可用")


def action(value):
    return ACTION.get(value, value or "不可用")


def show_error(label, error):
    if error:
        st.warning(f"{label}：{error}")


def chinese_columns(frame, names):
    return frame.rename(columns={key: value for key, value in names.items() if key in frame})


paper = paper_state()
carry = carry_state()
polymarket = polymarket_state()
gmgn = gmgn_state()
governance = governance_state()
replay = replay_state()
backtest = backtest_state()

with st.sidebar:
    page = st.radio("查看页面", ["总览", "资金费率套利", "BTC 方向基准", "Polymarket 15分钟研究", "GMGN Solana 聪明钱", "策略治理", "历史回放与研究", "数据健康"])
    if st.button("刷新本地数据"):
        st.rerun()
    st.caption("页面仅读取本地文件。每小时任务独立更新数据和模拟账本。")

if page == "总览":
    st.subheader("两套模拟账本，收益严格分开")
    left, right = st.columns(2)
    with left:
        st.markdown("### 资金费率 Carry 模拟账本")
        show_error("Carry 账户", carry["error"])
        a, latest = carry["account"], carry["last"]
        annual = float(latest.get("funding_rate", 0)) * 3 * 365
        c1, c2, c3 = st.columns(3)
        c1.metric("账本权益", usd(a.get("cash")), pct(float(a.get("cash", 0)) / float(a.get("initial_equity", 500)) - 1))
        c2.metric("状态", status(a.get("status", "unavailable")))
        c3.metric("净 Edge", pct(annual - 0.1582))
        st.caption(f"Funding 年化 {pct(annual)} | 基差 {pct(latest.get('basis'))} | 已结算资金费 {usd(a.get('realized_funding'))}")
    with right:
        st.markdown("### BTC 方向基准盘")
        show_error("方向账户", paper["error"])
        a, latest = paper["account"], paper["last"]
        equity = latest.get("equity", a.get("cash", 0))
        c1, c2, c3 = st.columns(3)
        c1.metric("账本权益", usd(equity), pct(float(equity) / 500 - 1))
        c2.metric("目标仓位", pct(latest.get("target_w")))
        c3.metric("本轮动作", action(latest.get("action", "unavailable")))
        accuracy = "待积累" if paper["accuracy"] is None else pct(paper["accuracy"])
        st.caption(f"信号 {float(latest.get('s', 0)):+.3f} | BTC {usd(latest.get('price'))} | 方向预测命中率：{accuracy}")
    st.info("Carry、方向盘和 Polymarket 三套账本的净值与收益绝不合并。")

elif page == "资金费率套利":
    st.subheader("资金费率 Carry 监控")
    show_error("Carry 账户", carry["error"])
    show_error("Carry 行情快照", carry["snapshot_error"])
    a, latest = carry["account"], carry["last"]
    annual = float(latest.get("funding_rate", 0)) * 3 * 365
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("状态", status(a.get("status", "unavailable")))
    c2.metric("Funding 年化", pct(annual))
    c3.metric("净 Edge", pct(annual - 0.1582))
    c4.metric("基差", pct(latest.get("basis")))
    c5.metric("账本权益", usd(a.get("cash")))
    if a.get("halt_reason"):
        st.error(f"暂停原因：{a['halt_reason']}")
    if not carry["snapshots"].empty:
        st.markdown("#### 现货与永续 Mark 价格")
        st.line_chart(chinese_columns(carry["snapshots"].set_index("time")[["spot_last", "swap_mark"]], {"spot_last": "现货", "swap_mark": "永续 Mark"}).tail(200), height=280)
        st.markdown("#### 基差与 Funding")
        st.line_chart(chinese_columns(carry["snapshots"].set_index("time")[["basis", "funding_rate"]], {"basis": "基差", "funding_rate": "Funding 费率"}).tail(200), height=220)
    if not carry["history"].empty:
        st.markdown("#### Carry 权益曲线")
        st.line_chart(carry["history"].set_index("time")[["equity"]].rename(columns={"equity": "权益"}), height=180)
    st.markdown("#### Funding 结算记录")
    show_error("Funding 账本", carry["funding_error"])
    st.dataframe(chinese_columns(carry["funding"].tail(50), {"ts": "结算时间", "rate": "费率", "notional": "名义金额", "cashflow": "现金流", "src": "来源"}), use_container_width=True, hide_index=True)
    st.markdown("#### Carry 事件记录")
    st.dataframe(chinese_columns(carry["events"].tail(50), {"type": "类型", "ts": "时间", "action": "动作", "equity": "权益", "basis_pnl": "基差残差"}), use_container_width=True, hide_index=True)

elif page == "BTC 方向基准":
    st.subheader("BTC 方向基准盘")
    show_error("方向账户", paper["error"])
    latest = paper["last"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("BTC 最新价", usd(latest.get("price")))
    c2.metric("信号", f"{float(latest.get('s', 0)):+.3f}")
    c3.metric("目标仓位", pct(latest.get("target_w")))
    c4.metric("方向命中率", "待积累" if paper["accuracy"] is None else pct(paper["accuracy"]))
    if not paper["history"].empty:
        st.markdown("#### 方向盘权益曲线")
        st.line_chart(paper["history"].set_index("time")[["equity"]].rename(columns={"equity": "权益"}), height=260)
    st.markdown("#### 决策记录")
    st.dataframe(chinese_columns(paper["decisions"].tail(100), {"ts": "时间", "s": "信号", "total": "总分", "target_w": "目标仓位", "cur_w": "当前仓位", "action": "动作", "price": "BTC 价格", "equity": "权益"}), use_container_width=True, hide_index=True)
    st.markdown("#### 24 小时预测结算")
    show_error("预测数据", paper["prediction_error"])
    st.dataframe(chinese_columns(paper["predictions"].tail(100), {"ts": "预测时间", "price": "预测价", "s": "信号", "s_trend": "趋势对照", "scored": "已结算", "realized": "实际涨跌", "pred": "预测方向", "real_dir": "实际方向", "hit": "命中"}), use_container_width=True, hide_index=True)

elif page == "Polymarket 15分钟研究":
    st.subheader("Polymarket 15 分钟套利研究")
    pm = polymarket
    show_error("Polymarket 账户", pm["account_error"])
    show_error("Polymarket 状态", pm["status_error"])
    account, state = pm["account"], pm["status"]
    latest = pm["quotes"].iloc[-1].to_dict() if not pm["quotes"].empty else {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("研究状态", "只读观察")
    c2.metric("累计观察市场", account.get("markets_observed", 0))
    c3.metric("候选机会", account.get("candidates", 0))
    c4.metric("纸面权益", usd(account.get("cash")))
    st.caption(f"本轮观察 {state.get('markets_observed', 0)} 个 | 候选 {state.get('opportunities', 0)} 个 | 接口错误 {len(state.get('errors', []))} 个")
    if latest:
        st.markdown(f"#### 最近市场：{latest.get('question', latest.get('market_id', '未知'))}")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("完整集合成本", f"{float(latest.get('complete_set_cost', 0)):.4f}")
        c2.metric("毛 Edge", pct(latest.get("gross_edge")))
        c3.metric("扣成本净 Edge", pct(latest.get("net_edge")))
        c4.metric("模式", "仅观察")
        st.json(latest.get("token_quotes", []))
    if not pm["quotes"].empty:
        st.markdown("#### Edge 变化")
        st.line_chart(pm["quotes"].set_index("time")[["gross_edge", "net_edge"]].rename(columns={"gross_edge": "毛 Edge", "net_edge": "净 Edge"}).tail(300), height=240)
    if not pm["decisions"].empty:
        st.markdown("#### 研究决策")
        st.dataframe(chinese_columns(pm["decisions"].tail(100), {"time": "观测时间", "market_id": "市场 ID", "action": "处理", "gross_edge": "毛 Edge", "net_edge": "净 Edge"}), use_container_width=True, hide_index=True)
    st.markdown("#### 研究边界")
    st.info("当前只读取公开 Gamma/CLOB 数据并记录可成交报价；不连接钱包、不签名、不下单。仅接受明确 BTC/ETH 且 30 分钟内到期的二元 Up/Down 市场；历史错误样本不计入评估。完整集合套利与 Temporal Arb 分开，后者当前关闭。")

elif page == "GMGN Solana 聪明钱":
    st.subheader("GMGN Solana 聪明钱纸面跟随")
    state = gmgn
    show_error("GMGN 账户", state["account_error"])
    show_error("GMGN 状态", state["status_error"])
    account, run = state["account"], state["status"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("模式", run.get("mode", "未初始化"))
    c2.metric("运行状态", status(account.get("status", run.get("status", "unavailable"))))
    c3.metric("纸面权益", usd(account.get("cash")))
    c4.metric("冻结钱包池", account.get("wallet_pool_size", 0))
    if account.get("halt_reason") or run.get("errors"):
        st.warning(f"运行阻断：{account.get('halt_reason') or run.get('errors')}")
    st.info("只读取 GMGN 官方授权数据或本地 fixture；不连接钱包、不签名、不下单。每日冻结 Top 100，当前只处理 Solana 买入信号。")
    if not state["history"].empty:
        st.line_chart(state["history"].set_index("time")[["equity"]].rename(columns={"equity": "权益"}), height=220)
    st.markdown("#### 最近跟随决策")
    show_error("GMGN 决策", state["decisions_error"])
    st.dataframe(chinese_columns(state["decisions"].tail(100), {"time": "观察时间", "event_id": "源事件", "wallet_address": "钱包", "asset_mint": "代币", "action": "动作", "reason": "原因", "latency_ms": "延迟毫秒"}), use_container_width=True, hide_index=True)

elif page == "策略治理":
    st.subheader("策略治理与轮换")
    show_error("治理状态", governance["error"])
    rows = governance["status"].get("strategies", [])
    st.info("治理只评估证据、数据完整性与预设门槛，不会自动调参、切换资金或执行交易。")
    for row in rows:
        st.markdown(f"#### {row['strategy_id']}")
        c1, c2 = st.columns(2)
        c1.metric("当前状态", row["status"])
        c2.metric("下次复核", row["review_due"])
        for concern in row.get("concerns", []):
            st.warning(concern)
        st.caption("晋级：" + "；".join(row.get("promotion_rules", [])))
        st.caption("暂停/淘汰：" + "；".join(row.get("kill_rules", [])))

elif page == "历史回放与研究":
    st.subheader("近期 OKX Carry 历史回放")
    show_error("回放数据", replay["error"])
    rows = replay["rows"]
    if not rows.empty:
        c1, c2, c3 = st.columns(3)
        c1.metric("结算样本", len(rows))
        c2.metric("模拟开仓次数", int((rows["action"] == "open").sum()))
        c3.metric("回放期末权益", usd(rows["equity"].iloc[-1]))
        st.line_chart(chinese_columns(rows.set_index("time")[["annual_funding", "net_edge", "basis"]], {"annual_funding": "Funding 年化", "net_edge": "净 Edge", "basis": "基差"}), height=250)
        st.dataframe(chinese_columns(rows.tail(100), {"ts": "结算时间", "funding_rate": "Funding 费率", "annual_funding": "Funding 年化", "net_edge": "净 Edge", "basis": "基差", "action": "动作", "funding_cash": "资金费现金流", "trade_pnl": "交易损益", "equity": "权益", "position_open": "是否持仓"}), use_container_width=True, hide_index=True)
    st.divider()
    st.subheader("研究结果，不是收益预测")
    show_error("回测摘要", backtest["error"])
    st.dataframe(backtest["summary"], use_container_width=True, hide_index=True)
    selected = st.multiselect("选择权益曲线", list(backtest["curves"]), default=list(backtest["curves"]))
    for name in selected:
        curve = backtest["curves"][name]
        if not curve.empty:
            st.markdown(f"#### {name}")
            st.line_chart(curve.set_index("time")[["equity"]].rename(columns={"equity": "权益"}), height=180)

else:
    st.subheader("数据健康状态")
    health = data_health()
    st.caption("OKX Funding 是真实交易所数据；Deribit Funding 仅为研究代理，绝不计入 Carry 已实现收益。")
    st.dataframe(chinese_columns(health, {"file": "数据文件", "status": "状态", "rows": "行数", "source": "来源", "from": "开始时间", "to": "结束时间", "gaps": "缺口", "modified": "文件更新时间"}), use_container_width=True, hide_index=True)
