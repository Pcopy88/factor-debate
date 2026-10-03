"""A deliberately small expression language; no eval, imports, or generated shell code."""
import ast
import operator
from dataclasses import dataclass
import numpy as np
import pandas as pd

FINANCIAL_FIELDS = {
    "revenue": "营业收入", "net_profit": "归母净利润", "cfo": "经营现金流净额",
    "operating_cost": "营业成本", "total_assets": "总资产", "total_equity": "净资产",
    "accounts_receivable": "应收账款", "total_liab": "总负债", "cash": "货币资金",
    "capex": "购建长期资产现金支出",
}
FIELDS = {**FINANCIAL_FIELDS, "market_cap": "当时总市值"}
FLOWS = {"revenue", "net_profit", "cfo", "operating_cost", "capex"}
TIME_FUNCS = {"lag", "delta", "yoy", "ttm", "mean"}
CROSS_FUNCS = {"rank", "zscore", "industry_neutralize", "winsorize"}
FUNCS = TIME_FUNCS | CROSS_FUNCS | {"abs", "log1p"}
BASELINES = {
    "价值基线": "ttm(net_profit) / market_cap",
    "质量基线": "ttm(cfo) / total_assets",
    "成长基线": "yoy(revenue)",
}


class FormulaError(ValueError):
    pass


def parse(formula):
    try:
        tree = ast.parse(formula, mode="eval")
    except (SyntaxError, ValueError) as e:
        raise FormulaError("公式语法不完整") from e
    if len(list(ast.walk(tree))) > 100:
        raise FormulaError("公式超过第一版复杂度上限")

    def check(node, temporal=False):
        if isinstance(node, ast.Expression):
            check(node.body)
        elif isinstance(node, ast.Constant):
            if type(node.value) not in (int, float) or not np.isfinite(node.value) or abs(node.value) > 1e12:
                raise FormulaError("仅允许有限数值常量")
        elif isinstance(node, ast.Name):
            if node.id not in FIELDS:
                raise FormulaError(f"未知财务字段：{node.id}")
            if temporal and node.id == "market_cap":
                raise FormulaError("第一版市值仅用于当时估值，不能在季度时间算子中使用")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            check(node.left, temporal)
            check(node.right, temporal)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            check(node.operand, temporal)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS:
            name = node.func.id
            if node.keywords or len(node.args) not in ({1, 2} if name in {"lag", "delta", "mean"} else {1}):
                raise FormulaError(f"{name} 参数数量不正确")
            if temporal and name in CROSS_FUNCS:
                raise FormulaError("行业调整和截面排序只能在季度时间运算之后使用")
            check(node.args[0], temporal or name in TIME_FUNCS)
            if len(node.args) == 2:
                n = node.args[1]
                if not isinstance(n, ast.Constant) or type(n.value) is not int or not 1 <= n.value <= 12:
                    raise FormulaError("季度窗口必须是 1 到 12 的整数")
        else:
            raise FormulaError("公式包含未授权操作；只允许财务字段与公开算子")

    check(tree)
    return tree


def canonical(formula):
    return ast.unparse(parse(formula))


@dataclass
class Snapshot:
    panels: dict[str, pd.DataFrame]
    latest_period: pd.Series
    market: pd.DataFrame

    def latest(self, value):
        symbols = self.market.index
        if np.isscalar(value):
            return pd.Series(float(value), index=symbols)
        if isinstance(value, pd.Series):
            return value.reindex(symbols)
        return pd.Series({symbol: value.at[period, symbol]
                          if period in value.index and symbol in value.columns else np.nan
                          for symbol, period in self.latest_period.items()}).reindex(symbols)


def divide(a, b):
    if np.isscalar(b):
        return a * np.nan if abs(b) < 1e-10 else a / b
    return a / b.where(b.abs() > 1e-10)


def evaluate_formula(formula: str, snapshot: Snapshot) -> pd.Series:
    tree = parse(formula)

    def binary(a, b, op):
        if isinstance(a, pd.Series) and isinstance(b, pd.DataFrame):
            a = snapshot.latest(a)
            b = snapshot.latest(b)
        elif isinstance(a, pd.DataFrame) and isinstance(b, pd.Series):
            a = snapshot.latest(a)
        fn = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: divide}[type(op)]
        return fn(a, b)

    def visit(n):
        if isinstance(n, ast.Constant):
            return float(n.value)
        if isinstance(n, ast.Name):
            return snapshot.market["market_cap"] if n.id == "market_cap" else snapshot.panels[n.id]
        if isinstance(n, ast.UnaryOp):
            return -visit(n.operand) if isinstance(n.op, ast.USub) else visit(n.operand)
        if isinstance(n, ast.BinOp):
            return binary(visit(n.left), visit(n.right), n.op)
        if isinstance(n, ast.Call):
            name = n.func.id
            a = visit(n.args[0])
            k = n.args[1].value if len(n.args) == 2 else (4 if name == "mean" else 1)
            if name in TIME_FUNCS:
                if not isinstance(a, pd.DataFrame):
                    raise FormulaError("季度运算需要财务时间序列")
                if name == "lag": return a.shift(k)
                if name == "delta": return a - a.shift(k)
                if name == "yoy": return divide(a - a.shift(4), a.shift(4).abs())
                if name == "ttm": return a.rolling(4, min_periods=4).sum()
                if name == "mean": return a.rolling(k, min_periods=k).mean()
            if name == "abs": return abs(a)
            if name == "log1p": return np.log1p(a.where(a > -1)) if not np.isscalar(a) else (np.log1p(a) if a > -1 else np.nan)
            a = snapshot.latest(a).replace([np.inf, -np.inf], np.nan)
            if name == "rank": return a.rank(pct=True)
            if name == "zscore": return divide(a - a.mean(), a.std())
            if name == "winsorize": return a.clip(a.quantile(.01), a.quantile(.99))
            if name == "industry_neutralize":
                return a - a.groupby(snapshot.market["industry"]).transform("mean")
        raise FormulaError("无法计算该公式")

    result = snapshot.latest(visit(tree.body))
    return result.replace([np.inf, -np.inf], np.nan).astype(float)


def source_code(formula):
    expression = canonical(formula)
    return ('# 生成的因子入口；只调用经过校验的财务算子解释器\n'
            'from research.factors import evaluate_formula\n\n'
            'def factor(snapshot):\n'
            f'    return evaluate_formula({expression!r}, snapshot)\n')

