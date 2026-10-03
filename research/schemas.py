from datetime import date
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from .research_types import ResearchPolicy, MetricReference


class StrictModel(BaseModel):
    model_config = {"extra": "forbid"}


class TaskConfig(StrictModel):
    workflow_version: Literal[1, 2] = 2
    research_policy: ResearchPolicy = Field(default_factory=ResearchPolicy)
    name: str = Field(min_length=1, max_length=100)
    objective: str = Field(default="寻找有经济解释、可复现的 A 股基本面因子", max_length=2000)
    mode: Literal["demo", "live"] = "demo"
    architecture: Literal["debate", "independent", "single"] = "debate"
    dataset_id: str = "demo-v1"
    model_profile_id: str | None = None
    engineer_profile_id: str | None = None
    budget_cny: float = Field(default=0, ge=0, le=10000)
    proposals_per_agent: int = Field(default=3, ge=1, le=3)
    debate_rounds: int = Field(default=2, ge=0, le=2)
    revision_rounds: int = Field(default=2, ge=0, le=2)
    max_experiments: int = Field(default=20, ge=1, le=20)
    literature_cutoff: date = date(2022, 12, 31)
    develop_start: date = date(2014, 1, 1)
    develop_end: date = date(2019, 12, 31)
    validation_start: date = date(2020, 1, 1)
    validation_end: date = date(2022, 12, 31)
    final_start: date = date(2023, 1, 1)
    final_end: date = date(2025, 12, 31)
    horizons: list[int] = Field(default_factory=lambda: [20, 60, 120])
    commission_bps: float = Field(default=3, ge=0, le=100)
    slippage_bps: float = Field(default=10, ge=0, le=200)
    min_listing_days: int = Field(default=730, ge=0)
    min_turnover_cny: float = Field(default=10000000, ge=0)
    min_cross_section: int = Field(default=20, ge=10)

    @model_validator(mode="after")
    def check_dates(self):
        if not (self.develop_start < self.develop_end < self.validation_start
                < self.validation_end < self.final_start < self.final_end):
            raise ValueError("开发、验证、最终测试区间必须按时间排列且不重叠")
        if self.literature_cutoff > self.validation_end:
            raise ValueError("研究资料截止日期不能晚于验证区结束日期")
        if self.horizons != [20, 60, 120]:
            raise ValueError("第一版固定检验 20、60、120 个交易日，以 60 日为主")
        if self.mode == "live" and (not self.model_profile_id or self.budget_cny <= 0):
            raise ValueError("真实研究需要选择模型并设置本批费用上限")
        return self


class Hypothesis(StrictModel):
    title: str = Field(min_length=2, max_length=120)
    mechanism: str = Field(min_length=10, max_length=2400)
    formula: str = Field(min_length=1, max_length=1000)
    direction: Literal[1, -1] = 1
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)
    failure_condition: str = Field(min_length=5, max_length=1000)
    change_reason: str = Field(default="首次提出", max_length=1000)


class Critique(StrictModel):
    target_id: str
    issue: str = Field(min_length=5, max_length=1600)
    proposed_test: str = Field(min_length=5, max_length=1600)
    evidence_ids: list[str] = Field(default_factory=list)
    issue_type: Literal['mechanism','coverage','cost_risk','stability','similarity','implementation'] = 'mechanism'
    metric_refs: list[MetricReference] = Field(default_factory=list)


class EmpiricalRevision(StrictModel):
    parent_id: str
    action: Literal['revise','keep','drop']
    hypothesis: Hypothesis | None = None
    addressed_critique_ids: list[str] = Field(default_factory=list)
    addressed_warning_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=5, max_length=2400)
    expected_effects: list[str] = Field(default_factory=list)
    unresolved_issues: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_action(self):
        if (self.action == 'revise') != (self.hypothesis is not None):
            raise ValueError('只有 revise 必须提供新假设；keep/drop 不得提供新定义')
        return self


class AgentOutput(StrictModel):
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=6)
    critiques: list[Critique] = Field(default_factory=list, max_length=12)
    explanation: str = Field(default="", max_length=4000)
    revisions: list[EmpiricalRevision] = Field(default_factory=list, max_length=6)


class Decision(StrictModel):
    gate: Literal[1, 5, 10]
    action: Literal["approve", "return", "reject", "observe", "retain"]
    expected_version: int
    request_id: str = Field(min_length=8, max_length=100)
    reason: str = Field(default="", max_length=2000)


class ModelProfile(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    protocol: Literal["responses", "chat_completions"] = "responses"
    base_url: str = "https://api.openai.com/v1"
    model: str = Field(min_length=1, max_length=150)
    secret_name: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,80}$")
    input_cny_per_million: float = Field(ge=0)
    output_cny_per_million: float = Field(ge=0)
    max_output_tokens: int = Field(default=6000, ge=1000, le=16000)

    @model_validator(mode="after")
    def known_price(self):
        if self.input_cny_per_million == self.output_cny_per_million == 0:
            raise ValueError("请填写已核对的模型报价；第一版不以零报价代替未知价格")
        return self


class DocumentInput(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(max_length=2000)
    published_date: date
    version_date: date
    publisher: str = Field(min_length=1, max_length=200)
    evidence_note: str = Field(default="", max_length=2000)
    text: str = Field(default="", max_length=300000)
    verified: bool = False


class SearchInput(StrictModel):
    query: str = Field(min_length=2, max_length=300)
    cutoff: date = date(2022, 12, 31)
