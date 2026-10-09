"""Validated contracts for the LangGraph workflow.

The graph uses a TypedDict for partial node updates; these Pydantic models
validate the input boundary and every LLM response before it enters state.
"""

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RouteOutput(StrictModel):
    kind: Literal["NEW_TICKET", "MODIFICATION", "QUESTION"]
    reason: str = Field(min_length=1, max_length=500)
    search_query: str | None = Field(default=None, max_length=300)


class AcceptanceCriterion(StrictModel):
    description: str = Field(min_length=1, max_length=1000)
    verification: str = Field(min_length=1, max_length=1000)


class PlanOutput(StrictModel):
    kind: Literal["code", "clarification"]
    summary: str = Field(min_length=1, max_length=1000)
    criteria: list[AcceptanceCriterion] = Field(default_factory=list, max_length=20)
    clarification_question: str | None = Field(default=None, max_length=1000)
    research_query: str | None = Field(default=None, max_length=300)
    scout_query: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def complete_plan(self):
        if self.kind == "code" and not self.criteria:
            raise ValueError("code plan requires acceptance criteria")
        if self.kind == "clarification" and not self.clarification_question:
            raise ValueError("clarification requires a question")
        return self


class AnswerOutput(StrictModel):
    answer: str = Field(min_length=1, max_length=12000)


class GeneratedFile(StrictModel):
    path: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=200000)

    @model_validator(mode="after")
    def safe_path(self):
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or "\\" in self.path:
            raise ValueError("unsafe path")
        return self


class CodeOutput(StrictModel):
    summary: str = Field(min_length=1, max_length=1000)
    files: list[GeneratedFile] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def unique_paths(self):
        if len({item.path for item in self.files}) != len(self.files):
            raise ValueError("duplicate file path")
        if sum(len(item.content) for item in self.files) > 150000:
            raise ValueError("deliverable exceeds context budget")
        return self


class ReviewOutput(StrictModel):
    approved: bool
    summary: str = Field(min_length=1, max_length=1000)
    feedback: list[str] = Field(default_factory=list, max_length=20)


class WorkflowState(StrictModel):
    message: str = Field(min_length=1, max_length=20000)
    history: list[dict] = Field(default_factory=list, max_length=20)
    previous_files: list[GeneratedFile] = Field(default_factory=list)
    pending_clarification: str | None = None
    route: RouteOutput | None = None
    plan: PlanOutput | None = None
    research_results: list[dict] = Field(default_factory=list)
    research_done: bool = False
    scout_references: list[dict] = Field(default_factory=list)
    scout_done: bool = False
    code: CodeOutput | None = None
    review: ReviewOutput | None = None
    validation: dict | None = None
    attempts: int = Field(default=0, ge=0)
    status: str = ""
    result: str = ""
