"""Versioned research contracts. No access to databases or model providers."""
from typing import Literal
from pydantic import BaseModel, Field


class ResearchModel(BaseModel):
    model_config = {"extra": "forbid", "allow_inf_nan": False}


class SimilarityPolicy(ResearchModel):
    similarity_threshold: float = Field(default=.75, ge=0, le=1)
    min_pair_symbols: int = Field(default=20, ge=2)
    min_overlap_ratio: float = Field(default=.5, ge=0, le=1)
    min_valid_months: int = Field(default=12, ge=1)
    min_month_ratio: float = Field(default=.5, ge=0, le=1)
    top_k: int = Field(default=3, ge=1, le=10)


class ResearchPolicy(ResearchModel):
    version: Literal['research-policy-2.0'] = 'research-policy-2.0'
    min_ic_months: int = Field(default=24, ge=2)
    min_factor_coverage: float = Field(default=.7, ge=0, le=1)
    min_label_coverage: float = Field(default=.9, ge=0, le=1)
    min_rank_ic: float = Field(default=0, ge=-1, le=1)
    min_annual_excess: float = Field(default=0, ge=-1, le=10)
    similarity: SimilarityPolicy = Field(default_factory=SimilarityPolicy)


class MetricReference(ResearchModel):
    experiment_id: str
    split: Literal['development', 'validation']
    metric_path: str


class SplitFeedback(ResearchModel):
    experiment_id: str | None = None
    execution_status: Literal['complete','failed','not_evaluated']
    period: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
    missing_reasons: list[str] = Field(default_factory=list)
    dataset_version: str | None = None
    engine_signature: str | None = None


class PairSimilarity(ResearchModel):
    target_id: str
    reference_id: str
    relationship: Literal['baseline','historical','peer','ancestor']
    split: Literal['development','validation']
    status: Literal['complete','insufficient']
    mean_abs_correlation: float | None = None
    mean_correlation: float | None = None
    valid_months: int = 0
    planned_months: int = 0
    mean_overlap: float | None = None
    reason: str = ''


class SimilarityReport(ResearchModel):
    snapshot_hash: str = ''
    pairs: list[PairSimilarity] = Field(default_factory=list)
    warnings: list[dict] = Field(default_factory=list)
    references: list[dict] = Field(default_factory=list)
    insufficient_reasons: list[str] = Field(default_factory=list)


class ResearchFeedback(ResearchModel):
    feedback_version: str = 'feedback-2.0'
    task_id: str
    hypothesis_id: str
    lineage_id: str = ''
    round_no: int
    parent_id: str | None = None
    formula: str
    direction: Literal[1,-1] = 1
    policy_hash: str
    development: SplitFeedback
    validation: SplitFeedback
    changes: dict = Field(default_factory=dict)
    similarity: SimilarityReport = Field(default_factory=SimilarityReport)
    quality: dict = Field(default_factory=dict)


class CandidateDecision(ResearchModel):
    definition_id: str
    hypothesis_ids: list[str]
    execution_status: Literal['complete','failed','not_evaluated']
    quality_status: Literal['passed','failed','insufficient']
    selection_status: Literal['pending','selected','quality_rejected','similarity_rejected','insufficient','not_evaluated']
    reasons: list[dict] = Field(default_factory=list)
    covered_by: str | None = None


class SelectionResult(ResearchModel):
    decisions: list[CandidateDecision] = Field(default_factory=list)
    selected_ids: list[str] = Field(default_factory=list)
    policy_hash: str
    snapshot_hash: str
    stop_reason: str = ''


class LineageState(ResearchModel):
    lineage_id: str
    origin_role: str
    current_author: str
    head_id: str
    round_no: int = 0
    active: bool = True
    stop_reason: str = ''


class RoundManifest(ResearchModel):
    task_id: str
    round_no: int
    hypothesis_ids: list[str]
    snapshot_id: str
    feedback_ids: list[str]
    role_groups: dict[str,list[str]]
    input_hash: str
