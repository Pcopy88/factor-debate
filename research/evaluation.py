"""Deterministic evaluation. No model can change the metrics or read final results."""
import math
from pathlib import Path
import numpy as np
import pandas as pd
from . import db
from .data import DataStore
from .factors import evaluate_formula, canonical, source_code, BASELINES

ENGINE_VERSION = "pit-monthly-2.0"


def engine_sources():
    return {name: Path(__file__).with_name(name).read_text(encoding='utf-8')
            for name in ['data.py','factors.py','evaluation.py']}


def engine_signature():
    return db.digest(engine_sources())


def finite(value):
    if value is None or not np.isfinite(value): return None
    return round(float(value), 8)


def correlation(a, b):
    pairs = pd.concat([a, b], axis=1).dropna()
    if len(pairs) < 5 or (pairs.nunique() < 2).any(): return None
    return finite(pairs.rank().corr().iloc[0, 1])


def hac_test(values, lag=3):
    values = np.asarray(values, dtype=float)
    n = len(values)
    std = float(values.std(ddof=1)) if n > 1 else 0.
    icir = finite(values.mean()/std) if std > 1e-12 else None
    if n < 12: return {"mean": finite(values.mean()) if n else None, "t": None, "p_adjusted": None, "n": n, "icir":icir}
    z = values - values.mean()
    variance = float(z @ z / n)
    for k in range(1, min(lag, n - 1) + 1):
        variance += 2 * (1 - k / (lag + 1)) * float(z[k:] @ z[:-k] / n)
    se = math.sqrt(max(variance, 0) / n)
    t = float(values.mean() / se) if se > 1e-12 else 0.
    return {"mean": finite(values.mean()), "t": finite(t), "p_raw": math.erfc(abs(t) / math.sqrt(2)), "n": n, "icir":icir}


class Evaluator:
    def __init__(self, store: DataStore, config):
        self.store, self.config = store, config
        self.market = store.market.copy()
        self.dates = store.dates
        self.frames = {d: group.set_index("symbol") for d, group in self.market.groupby("date")}
        self.closes = self.market.assign(adjusted=self.market.close * self.market.adj_factor).pivot(index="date", columns="symbol", values="adjusted")
        self.opens = self.market.assign(adjusted=self.market.open * self.market.adj_factor).pivot(index="date", columns="symbol", values="adjusted")
        self.terminal = {}
        if "delist_cash" in self.market:
            for row in self.market[self.market.delist_cash.notna()].itertuples():
                self.terminal[row.symbol] = (row.date, float(row.delist_cash * row.adj_factor))
                self.closes.loc[self.closes.index >= row.date, row.symbol] = row.delist_cash * row.adj_factor
        self.snapshots = {}

    def snapshot(self, day):
        if day not in self.snapshots:
            self.snapshots[day] = self.store.snapshot(day, self.config.min_listing_days, self.config.min_turnover_cny)
        return self.snapshots[day]

    def signal_panel(self, formula, start, end, direction=1):
        dates = self.dates[(self.dates >= pd.Timestamp(start)) & (self.dates <= pd.Timestamp(end))]
        if not len(dates): raise ValueError("评估区间没有行情")
        # Use actual last trading day of each calendar month, never a partial month.
        all_month_ends = pd.Series(self.dates, index=self.dates).groupby(self.dates.to_period("M")).max()
        signal_days = [d for d in all_month_ends if dates[0] <= d <= dates[-1]
                       and d.to_period('M').end_time.normalize() <= self.dates[-1]]
        chunks = []
        for day in signal_days:
            snap = self.snapshot(day)
            score = evaluate_formula(formula, snap) * direction
            chunks.append(pd.DataFrame({'signal_date':day,'symbol':score.index,'value':score.values,'eligible':True})
                          if len(score) else pd.DataFrame([{'signal_date':day,'symbol':'','value':np.nan,'eligible':False}]))
        return pd.concat(chunks,ignore_index=True) if chunks else pd.DataFrame(columns=['signal_date','symbol','value','eligible'])

    def signals(self, formula, start, end, direction=1):
        return self.signals_from_panel(self.signal_panel(formula,start,end,direction))

    def signals_from_panel(self, panel):
        result={}
        for day,group in panel.groupby('signal_date'):
            scores=group[group.eligible].set_index('symbol').value.dropna()
            if len(scores) >= self.config.min_cross_section: result[day]=scores
        return result

    def forward_return(self, signal_day, horizon, end):
        pos = self.dates.get_loc(signal_day)
        if pos + horizon >= len(self.dates) or pos + 1 >= len(self.dates): return None
        exit_day, entry_day = self.dates[pos + horizon], self.dates[pos + 1]
        if exit_day > pd.Timestamp(end): return None
        entry = self.opens.loc[entry_day]
        exit_ = self.closes.loc[exit_day]
        # Delisting without documented settlement remains missing rather than disappearing as a zero return.
        return exit_ / entry - 1

    def portfolio(self, signals, start, end, benchmark=False, cost_multiplier=1., rebalance_months=1):
        days = self.dates[(self.dates >= pd.Timestamp(start)) & (self.dates <= pd.Timestamp(end))]
        targets = {}
        for day, scores in signals.items():
            if rebalance_months == 3 and day.month % 3: continue
            p = self.dates.get_loc(day)
            if p + 1 >= len(self.dates): continue
            if benchmark:
                symbols = self.snapshot(day).market.index.tolist()
            else:
                n = max(1, int(math.ceil(len(scores) * .2)))
                symbols = scores.sort_values(ascending=False, kind="stable").head(n).index.tolist()
            targets[self.dates[p+1]] = symbols
        cash, units = 1_000_000., {}
        navs, turnover, costs, blocked = [], 0., 0., 0
        last_price = {}
        commission = self.config.commission_bps * 1e-4 * cost_multiplier
        slip = self.config.slippage_bps * 1e-4 * cost_multiplier
        for day in days:
            frame = self.frames[day]
            for symbol in list(units):
                if symbol not in frame.index:
                    raise ValueError(f"持仓 {symbol} 在 {day.date()} 缺少价格／退市结算记录，停止计算")
            adjusted_open = frame.open * frame.adj_factor
            nav_open = cash + sum(q * adjusted_open[s] for s, q in units.items())
            if day in targets:
                selected = targets[day]
                weight = 1 / len(selected) if selected else 0
                target_value = {s: nav_open * weight for s in selected}
                for buying in [False, True]:
                    for symbol in sorted(set(units) | set(selected)):
                        if symbol not in frame.index:
                            if buying: blocked += 1
                            continue
                        row = frame.loc[symbol]
                        price = float(adjusted_open[symbol])
                        held = units.get(symbol, 0.)
                        delta = (target_value.get(symbol, 0) - held * price) / price
                        if (delta > 1e-8) != buying or abs(delta) < 1e-8: continue
                        if not bool(row.can_buy if buying else row.can_sell):
                            blocked += 1
                            continue
                        delta = min(abs(delta), float(row.amount) * .01 / price) * (1 if buying else -1)
                        if buying:
                            lot = 100 / float(row.adj_factor)
                            delta = math.floor(delta / lot) * lot
                        else:
                            delta = -min(abs(delta), held)
                        if abs(delta) < 1e-10: continue
                        execution = price * (1 + slip if buying else 1 - slip)
                        # PRC stock transaction stamp tax, seller side; halved effective 2023-08-28.
                        tax_rate = (0.001 if day < pd.Timestamp("2023-08-28") else .0005) if not buying else 0
                        minimum_fee=5.*cost_multiplier
                        fee = max(minimum_fee, abs(delta * execution) * commission) + abs(delta * execution) * tax_rate * cost_multiplier
                        if buying and delta * execution + fee > cash:
                            lot = 100 / float(row.adj_factor)
                            delta = math.floor(max(0., cash - minimum_fee) / (execution * (1 + commission)) / lot) * lot
                            fee = max(minimum_fee, abs(delta * execution) * commission)
                        if abs(delta) < 1e-10: continue
                        cash -= delta * execution + fee
                        units[symbol] = held + delta
                        if units[symbol] < 1e-8: units.pop(symbol, None)
                        costs += fee + abs(delta) * price * slip
                        turnover += abs(delta) * price / max(nav_open, 1)
            for symbol in list(units):
                if symbol in self.terminal and self.terminal[symbol][0] == day:
                    cash += units.pop(symbol) * self.terminal[symbol][1]
            nav = cash + sum(q * float(frame.loc[s, "close"] * frame.loc[s, "adj_factor"]) for s, q in units.items())
            navs.append({"date": str(day.date()), "nav": nav / 1_000_000})
        if not navs: raise ValueError("组合回测没有足够的交易日期")
        series = pd.Series([x["nav"] for x in navs], index=pd.to_datetime([x["date"] for x in navs]))
        returns = series.pct_change().fillna(series.iloc[0] - 1)
        years = len(series) / 252
        annual = series.iloc[-1] ** (1 / years) - 1 if series.iloc[-1] > 0 else -1
        drawdown = (series / series.cummax().clip(lower=1) - 1).min()
        yearly = {}
        previous = 1.
        for year, chunk in series.groupby(series.index.year):
            yearly[str(year)] = finite(chunk.iloc[-1] / previous - 1)
            previous = chunk.iloc[-1]
        monthly = series.resample("ME").last()
        return {"annual_return": finite(annual), "total_return": finite(series.iloc[-1]-1),
                "max_drawdown": finite(drawdown), "sharpe": finite(returns.mean()/returns.std()*np.sqrt(252)) if returns.std() else None,
                "annual_turnover": finite(turnover/max(years, .01)), "cost_cny": finite(costs), "blocked_orders": blocked,
                "yearly": yearly, "curve": [{"date": str(d.date()), "nav": finite(v)} for d, v in monthly.items()]}

    def run(self, hypothesis, start, end, minimum_signals=12):
        formula = canonical(hypothesis["formula"])
        panel = self.signal_panel(formula,start,end,hypothesis.get('direction',1))
        signals = self.signals_from_panel(panel)
        if len(signals) < minimum_signals:
            raise ValueError(f"有效月度截面只有 {len(signals)} 个，少于最低 {minimum_signals} 个；检查字段、公式与数据覆盖")
        ic_by_horizon, groups, industries = {}, [], {}
        for horizon in self.config.horizons:
            values, points, label_total, label_observed = [], [], 0, 0
            for day, scores in signals.items():
                ret = self.forward_return(day, horizon, end)
                if ret is None: continue
                label_total += len(scores)
                label_observed += int(ret.reindex(scores.index).notna().sum())
                ic = correlation(scores, ret)
                if ic is None: continue
                values.append(ic)
                points.append({"date": str(day.date()), "ic": ic})
                if horizon == 60:
                    ranked = scores.rank(method="first", pct=True)
                    group = np.ceil(ranked * 5).clip(1, 5)
                    for g in range(1, 6):
                        group_return = ret.reindex(scores.index[group == g]).mean()
                        if pd.notna(group_return): groups.append({"group": g, "return": float(group_return)})
                    labels = self.snapshot(day).market.industry
                    for industry in labels.unique():
                        sub = scores.reindex(labels.index[labels == industry]).dropna()
                        value = correlation(sub, ret)
                        if value is not None: industries.setdefault(industry, []).append(value)
            stats = hac_test(values, lag=math.ceil(horizon/20))
            stats["p_adjusted"] = finite(min(1., stats['p_raw'] * self.config.max_experiments * 3)) if 'p_raw' in stats else None
            stats["positive_ratio"] = finite(np.mean(np.asarray(values) > 0)) if values else None
            stats["points"] = points
            stats['label_coverage']=finite(label_observed/label_total) if label_total else None
            stats['missing_labels']=label_total-label_observed
            ic_by_horizon[str(horizon)] = stats
        portfolio = self.portfolio(signals, start, end)
        benchmark = self.portfolio(signals, start, end, benchmark=True)
        stress = self.portfolio(signals, start, end, cost_multiplier=2)
        stress_benchmark = self.portfolio(signals,start,end,benchmark=True,cost_multiplier=2)
        quarterly = self.portfolio(signals, start, end, rebalance_months=3)
        total = int(panel.eligible.sum())
        baseline_correlations = {name: [] for name in BASELINES}
        residual_ics = []
        for day,scores in signals.items():
            snap = self.snapshot(day)
            controls = pd.DataFrame({name:evaluate_formula(f,snap) for name,f in BASELINES.items()})
            for name in BASELINES:
                value=correlation(scores,controls[name])
                if value is not None: baseline_correlations[name].append(value)
            ret=self.forward_return(day,60,end)
            if ret is None: continue
            control_panel=controls.rank(pct=True).assign(score=scores.rank(pct=True),ret=ret.rank(pct=True),
                                                size=np.log(snap.market.market_cap),industry=snap.market.industry).dropna()
            x=pd.get_dummies(control_panel[[*BASELINES,'size','industry']],columns=['industry'],drop_first=True,dtype=float)
            x.insert(0,'intercept',1.)
            if len(control_panel) < x.shape[1]+10: continue
            residuals={}
            for name in ['score','ret']:
                y=control_panel[name].to_numpy()
                residuals[name]=pd.Series(y-x.to_numpy()@np.linalg.lstsq(x.to_numpy(),y,rcond=None)[0],index=control_panel.index)
            value=correlation(residuals['score'],residuals['ret'])
            if value is not None:residual_ics.append(value)
        return {"engine_version": ENGINE_VERSION, "formula": formula, "source_code": source_code(formula),
                "engine_signature":engine_signature(),"short_observation":len(signals)<12,
                "baseline_comparison":[{"name":k,"rank_correlation":finite(np.mean(v)) if v else None,"months":len(v)} for k,v in baseline_correlations.items()],
                "controlled_ic":hac_test(residual_ics,lag=3),
                "dataset_version": self.store.manifest["version"], "demo": self.store.manifest["demo"],
                "period": [str(start), str(end)], "signals": len(signals),
                "planned_signals":int(panel.signal_date.nunique()),"coverage_version":"all-planned-months-2.0",
                "signal_month_ratio":finite(len(signals)/max(1,panel.signal_date.nunique())),
                "coverage": finite(int((panel.eligible & panel.value.notna()).sum()) / total) if total else None,
                "ic": ic_by_horizon, "portfolio": portfolio, "benchmark": benchmark,
                "double_cost": stress,"double_cost_benchmark":stress_benchmark,
                "double_cost_annual_excess":finite(stress['annual_return']-stress_benchmark['annual_return']), "quarterly": quarterly,
                "annual_excess": finite(portfolio["annual_return"] - benchmark["annual_return"]),
                "groups": [{"group": g, "mean_forward_return": finite(np.mean([x["return"] for x in groups if x["group"] == g]))}
                           for g in range(1, 6)],
                "industries": [{"name": key, "rank_ic": finite(np.mean(vals)), "months": len(vals)} for key, vals in industries.items()],
                "limitations": ["分组收益为重叠的 60 日标签，不能作为可执行月度组合收益",
                                "统计检验使用 HAC 近似与预设实验上限的 Bonferroni 校正；未覆盖跨批次累计选择偏差",
                                "增量诊断同时剔除三个基础因子、行业与规模线性暴露；不证明因果关系",
                                "少于 12 个月的近期或前瞻样本只作描述，不报告显著性",
                                "交易模拟：100 股买入单位、1% 成交额上限、最低佣金 5 元；不模拟逐笔订单簿",
                                "复权价格用于经济收益与持仓单位；未纳入个人差异化股息税"]}


def run_cached(task_id, hypothesis_id, hypothesis, split, evaluator, start, end, final=False):
    fingerprint = db.digest({"formula": canonical(hypothesis["formula"]), "direction": hypothesis.get("direction", 1),
                             "data": evaluator.store.manifest["version"], "config": evaluator.config.model_dump(mode="json"),
                             "engine": engine_signature(),"period":[str(start),str(end)]})
    table = "results" if final else "experiments"
    existing = db.one(f"SELECT * FROM {table} WHERE task_id=? AND fingerprint=? AND split=?", (task_id, fingerprint, split), final)
    if existing:
        existing["payload"] = __import__("json").loads(existing["payload"])
        if not final: db.bind_evaluation(task_id,hypothesis_id,split,existing['id'])
        return existing
    try:
        result = evaluator.run(hypothesis,start,end,minimum_signals=1) if split in ('recent','forward') else evaluator.run(hypothesis,start,end)
        status = "complete"
    except (ValueError, KeyError, ArithmeticError) as e:
        result, status = {"error": str(e), "formula": hypothesis["formula"], "demo": evaluator.store.manifest["demo"]}, "failed"
    row = {"id": db.uid(), "task_id": task_id, "hypothesis_id": hypothesis_id, "fingerprint": fingerprint,
           "split": split, "payload": result, "status": status, "created_at": db.now()}
    with db.connect(final) as c:
        c.execute(f"INSERT OR IGNORE INTO {table} VALUES(?,?,?,?,?,?,?,?)",
                  (*[row[k] for k in ["id", "task_id", "hypothesis_id", "fingerprint", "split"]], db.dump(result), status, row["created_at"]))
    saved=db.one(f'SELECT * FROM {table} WHERE task_id=? AND fingerprint=? AND split=?',(task_id,fingerprint,split),final)
    saved['payload']=__import__('json').loads(saved['payload'])
    if not final: db.bind_evaluation(task_id,hypothesis_id,split,saved['id'])
    return saved
