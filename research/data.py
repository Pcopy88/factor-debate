import json
import hashlib
import re
from pathlib import Path
import numpy as np
import pandas as pd
import duckdb
from . import db
from .config import path_for
from .factors import FINANCIAL_FIELDS, FLOWS, Snapshot

MARKET_COLUMNS = {"symbol", "date", "open", "close", "adj_factor", "amount", "market_cap",
                  "industry", "is_st", "can_buy", "can_sell", "list_date", "delist_date"}
FIN_COLUMNS = {"symbol", "period", "announced_at", "version_date", "report_kind"} | set(FINANCIAL_FIELDS)


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def register_dataset(dataset_id, financials, market, source, pit_verified=False, demo=False):
    # The content-addressed directory makes previously registered inputs immutable.
    financials = financials.copy()
    market = market.copy()
    missing = FIN_COLUMNS - set(financials.columns)
    missing_market = MARKET_COLUMNS - set(market.columns)
    if missing or missing_market:
        raise ValueError(f"缺少必需字段：财务 {sorted(missing)}；行情 {sorted(missing_market)}")
    for col in ["period", "announced_at", "version_date"]:
        financials[col] = pd.to_datetime(financials[col], errors="raise")
    for col in ["date", "list_date", "delist_date"]:
        market[col] = pd.to_datetime(market[col], errors="raise")
    if financials.empty or market.empty:
        raise ValueError("财报与行情表不能为空")
    if not demo and any(not re.fullmatch(r'(?:[036]\d{5}\.(?:SZ|XSHE)|[6]\d{5}\.(?:SH|XSHG))',str(s)) for s in market.symbol.unique()):
        raise ValueError('正式数据仅接受沪深 A 股代码，例如 000001.SZ、600000.SH 或米筐对应格式')
    if financials[["symbol","period","announced_at","version_date"]].isna().any().any() or market[["symbol","date","list_date","industry"]].isna().any().any():
        raise ValueError("股票、日期和行业标识不能缺失")
    for col in FINANCIAL_FIELDS:
        financials[col] = pd.to_numeric(financials[col], errors="raise")
        if np.isinf(financials[col]).any(): raise ValueError("财务值不能为无穷大")
    for col in ["open","close","adj_factor","amount","market_cap"]:
        market[col] = pd.to_numeric(market[col], errors="raise")
        if not np.isfinite(market[col]).all(): raise ValueError(f"行情字段 {col} 必须为有限数值")
    if (market.amount < 0).any() or (market.market_cap <= 0).any():
        raise ValueError("成交额不能为负，总市值必须为正数")
    if "delist_cash" in market:
        market["delist_cash"] = pd.to_numeric(market["delist_cash"], errors="raise")
        if (market.delist_cash.dropna() < 0).any() or np.isinf(market.delist_cash.dropna()).any():
            raise ValueError("退市结算金额必须为非负有限数值")
    if not financials.report_kind.isin(["cumulative", "quarter"]).all():
        raise ValueError("report_kind 必须为 cumulative 或 quarter")
    if (financials.announced_at < financials.period).any() or (financials.version_date < financials.announced_at).any():
        raise ValueError("财务报告期、公告日、版本日顺序错误")
    if market.duplicated(["symbol", "date"]).any():
        raise ValueError("同一股票同一交易日存在重复行情")
    for col in ["is_st", "can_buy", "can_sell"]:
        if not market[col].isin([0, 1, True, False]).all():
            raise ValueError(f"{col} 必须明确记录 0/1，不允许未知交易状态")
        market[col] = market[col].astype(bool)
    if (market[["open", "close", "adj_factor"]] <= 0).any().any():
        raise ValueError("交易价格与复权因子必须为正数")
    if financials.duplicated(["symbol", "period", "version_date"]).any():
        raise ValueError("相同财报版本重复；请先核实报表口径")
    version = db.digest({"financials": financials.to_json(date_format="iso"), "market": market.to_json(date_format="iso")})
    folder = path_for("datasets", version, "manifest.json").parent
    fp, mp = folder / "financials.parquet", folder / "market.parquet"
    financials.sort_values(["symbol", "period", "version_date"]).to_parquet(fp, index=False)
    market.sort_values(["date", "symbol"]).to_parquet(mp, index=False)
    manifest = {
        "id": dataset_id, "version": version, "source": source, "demo": demo,
        "pit_verified": bool(pit_verified), "financials_path": str(fp), "market_path": str(mp),
        "financials_sha256": file_hash(fp), "market_sha256": file_hash(mp),
        "stocks": int(market.symbol.nunique()), "financial_rows": len(financials), "market_rows": len(market),
        "market_start": str(market.date.min().date()), "market_end": str(market.date.max().date()),
        "first_report": str(financials.period.min().date()), "last_report": str(financials.period.max().date()),
        "revisions": int(financials.duplicated(["symbol", "period"]).sum()),
        "financial_missing_rate": float(financials[list(FINANCIAL_FIELDS)].isna().mean().mean()),
        "created_at": db.now(),
    }
    with db.connect() as c:
        prior = c.execute("SELECT manifest FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if prior and json.loads(prior[0])["version"] != version:
            raise ValueError("数据集 ID 已存在，请为新版本使用新的 ID")
        c.execute("INSERT OR IGNORE INTO datasets VALUES(?,?,?)", (dataset_id, db.dump(manifest), db.now()))
    (folder / "manifest.json").write_text(db.dump(manifest), encoding="utf-8")
    return manifest


def manifest(dataset_id):
    item = db.one("SELECT manifest FROM datasets WHERE id=?", (dataset_id,))
    if not item:
        raise ValueError("尚未导入该数据集")
    return json.loads(item["manifest"])


class DataStore:
    def __init__(self, dataset_id, allowed_end=None):
        self.manifest = manifest(dataset_id)
        self.allowed_end = pd.Timestamp(allowed_end) if allowed_end else None
        for kind in ["financials", "market"]:
            if file_hash(self.manifest[f"{kind}_path"]) != self.manifest[f"{kind}_sha256"]:
                raise ValueError("数据文件与登记版本不一致，必须重新验收")
        with duckdb.connect() as c:
            if allowed_end:
                self.market = c.execute("SELECT * FROM read_parquet(?) WHERE date <= ?", [self.manifest["market_path"], str(allowed_end)]).df()
                self.financials = c.execute("SELECT * FROM read_parquet(?) WHERE version_date <= ?", [self.manifest["financials_path"], str(allowed_end)]).df()
            else:
                self.market = c.execute("SELECT * FROM read_parquet(?)", [self.manifest["market_path"]]).df()
                self.financials = c.execute("SELECT * FROM read_parquet(?)", [self.manifest["financials_path"]]).df()
        for col in ["date", "list_date", "delist_date"]: self.market[col] = pd.to_datetime(self.market[col])
        for col in ["period", "announced_at", "version_date"]: self.financials[col] = pd.to_datetime(self.financials[col])
        self.market = self.market.sort_values(["symbol", "date"])
        self.market["liquidity_60"] = self.market.groupby("symbol").amount.transform(lambda s: s.rolling(60, min_periods=20).mean())
        self.dates = pd.DatetimeIndex(sorted(self.market.date.unique()))

    def snapshot(self, as_of, min_listing_days=730, min_turnover=0):
        as_of = pd.Timestamp(as_of)
        if self.allowed_end is not None and as_of > self.allowed_end:
            raise PermissionError("研究工具不能读取最终测试区")
        m = self.market[self.market.date == as_of].set_index("symbol").copy()
        m = m[(~m.is_st) & ((as_of - m.list_date).dt.days >= min_listing_days)
              & (m.delist_date.isna() | (m.delist_date > as_of))
              & (~m.industry.astype(str).str.contains('银行|证券|保险|金融',regex=True))
              & (m.liquidity_60 >= min_turnover)]
        # Strictly before the signal date: date-only announcements become usable next session.
        f = self.financials[(self.financials.announced_at < as_of) & (self.financials.version_date < as_of)
                            & self.financials.symbol.isin(m.index)]
        f = f.sort_values(["version_date", "announced_at"]).drop_duplicates(["symbol", "period"], keep="last").copy()
        if f.empty:
            return Snapshot({k: pd.DataFrame(columns=m.index) for k in FINANCIAL_FIELDS}, pd.Series(dtype=object), m)
        f["quarter"] = f.period.dt.to_period("Q")
        latest = f.groupby("symbol").quarter.max()
        quarters = pd.period_range(f.quarter.min(), f.quarter.max(), freq="Q")
        panels = {}
        kinds = f.pivot(index="quarter", columns="symbol", values="report_kind").reindex(index=quarters, columns=m.index)
        for field in FINANCIAL_FIELDS:
            p = f.pivot(index="quarter", columns="symbol", values=field).reindex(index=quarters, columns=m.index)
            if field in FLOWS:
                diff = p - p.shift(1)
                diff.loc[diff.index.quarter == 1] = p.loc[p.index.quarter == 1]
                # A previous single-quarter number is not a cumulative base.
                invalid_base = (kinds.shift(1) != "cumulative") & np.asarray(quarters.quarter != 1)[:, None]
                diff = diff.mask(invalid_base)
                p = p.where(kinds != "cumulative", diff)
            panels[field] = p
        return Snapshot(panels, latest, m)

    def audit(self, config):
        errors = []
        if not self.manifest["demo"] and not self.manifest["pit_verified"]:
            errors.append("历史财报版本尚未验收，不能运行正式研究")
        if self.manifest["market_start"] > str(config.develop_start): errors.append("行情未覆盖开发区起点")
        if self.manifest["market_end"] < str(config.final_end): errors.append("行情未覆盖最终测试区终点")
        if self.manifest["first_report"] > "2011-12-31": errors.append("指标预热财务历史不足")
        for year in range(config.develop_start.year, config.final_end.year + 1):
            days = self.market.loc[self.market.date.dt.year == year, "date"].nunique()
            if days < 200: errors.append(f"{year} 年行情日期不足 200 个，需核验覆盖")
        if self.manifest["financial_missing_rate"] > .2: errors.append("基础财务字段缺失率超过 20%")
        return {"passed": not errors, "errors": errors, "manifest": self.manifest,
                "note": "合成数据仅用于软件验收，不能证明投资有效" if self.manifest["demo"] else "需要保留原公告抽样核验记录"}


def generate_demo(stocks=40):
    """Deterministic fixtures; synthetic symbols never masquerade as listed companies."""
    if db.one("SELECT id FROM datasets WHERE id='demo-v1'"):
        return manifest("demo-v1")
    rng = np.random.default_rng(20260914)
    dates = pd.bdate_range("2010-01-01", "2026-09-11")
    quarters = pd.period_range("2009Q1", "2026Q2", freq="Q")
    financial, market = [], []
    industries = ["制造业", "消费", "医药", "信息技术", "公用事业"]
    market_component = rng.normal(.00018, .009, len(dates))
    for i in range(stocks):
        symbol = f"DEMO{i+1:03d}"
        quality = rng.uniform(.05, .23)
        scale = rng.uniform(1e9, 8e9)
        cumulative = {k: 0. for k in FLOWS}
        for q in quarters:
            if q.quarter == 1: cumulative = {k: 0. for k in FLOWS}
            t = q.ordinal - quarters[0].ordinal
            revenue = scale * .1 * (1.015 ** t) * (1 + .06 * q.quarter) * rng.uniform(.9, 1.1)
            profit = revenue * quality * rng.uniform(.7, 1.2)
            vals = {"revenue": revenue, "net_profit": profit, "cfo": profit * rng.uniform(.6, 1.5),
                    "operating_cost": revenue * (1 - quality - .12), "capex": revenue * .07}
            for k in FLOWS: cumulative[k] += vals[k]
            period = q.end_time.normalize()
            ann = period + pd.Timedelta(days=35 if q.quarter != 4 else 95)
            record = {"symbol": symbol, "period": period, "announced_at": ann, "version_date": ann,
                      "report_kind": "cumulative", **cumulative, "total_assets": scale * 1.012 ** t,
                      "total_equity": scale * .55 * 1.012 ** t, "accounts_receivable": revenue * .25,
                      "total_liab": scale * .45 * 1.012 ** t, "cash": scale * .12 * 1.012 ** t}
            financial.append(record)
            if i % 7 == 0 and q == pd.Period("2017Q4"):
                revised = {**record, "version_date": pd.Timestamp("2019-04-30"), "net_profit": record["net_profit"] * .7}
                financial.append(revised)
        ret = market_component + rng.normal(0, .012, len(dates)) + (quality - .13) * .0005
        close = (10 + i / 2) * np.cumprod(1 + ret)
        open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, .002, len(dates)))
        delist = pd.Timestamp("2021-07-15") if i == 0 else pd.NaT
        for j, day in enumerate(dates):
            if pd.notna(delist) and day > delist: continue
            halted = i == 1 and pd.Timestamp("2018-06-01") <= day <= pd.Timestamp("2018-07-16")
            record = {"symbol": symbol, "date": day, "open": open_[j], "close": close[j],
                      "adj_factor": 1., "amount": float(rng.uniform(2e7, 2e8)),
                      "market_cap": close[j] * scale / 15, "industry": industries[i % 5],
                      "is_st": i == 0 and day >= pd.Timestamp("2020-07-01"),
                      "can_buy": not halted, "can_sell": not halted,
                      "list_date": pd.Timestamp("2005-01-01") if i != 2 else pd.Timestamp("2015-01-01"),
                      "delist_date": delist, "delist_cash": 0.0 if day == delist else np.nan}
            market.append(record)
    return register_dataset("demo-v1", pd.DataFrame(financial), pd.DataFrame(market),
                            "确定性合成数据 · 非真实 A 股行情", pit_verified=True, demo=True)
