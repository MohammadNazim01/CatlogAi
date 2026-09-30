"""Stage-1 extraction domain types (docs/03 §11). The pipeline that produces and consumes
these isn't built yet — that's a later milestone — but the contract is stable and tested now,
so that milestone builds on top of it instead of inventing one under time pressure."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from app.ai.base import ImageInput, Usage


class Evidence(StrEnum):
    """Where an extracted fact came from. Distinct from (and more granular than) the
    IMAGE|SELLER|UNKNOWN `AttrSource` used for the final catalog content (schemas/catalog.py)
    — this is what stage 1 itself reports, before seller facts are merged in."""

    IMAGE_VISIBLE = "IMAGE_VISIBLE"  # apparent from the photo itself (shape, colour, style)
    IMAGE_TEXT = "IMAGE_TEXT"  # read off packaging/label text visible in a photo
    UNKNOWN = "UNKNOWN"


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ExtractedAttribute(BaseModel):
    """One attribute as stage 1 reports it. `value` is null exactly when nothing was
    observed — the model is never allowed to both guess a value and mark it UNKNOWN, or claim
    a value with no evidence tag at all."""

    model_config = ConfigDict(extra="forbid")

    value: str | None
    evidence: Evidence
    confidence: Confidence | None = None

    @model_validator(mode="after")
    def _unknown_means_null(self) -> "ExtractedAttribute":
        if (self.evidence is Evidence.UNKNOWN) != (self.value is None):
            raise ValueError("value must be null exactly when evidence is UNKNOWN")
        return self


class ExtractionResult(BaseModel):
    """Stage 1's output shape — the `schema` passed to `LLMProvider.generate_structured` for
    the vision call. `category_id` is checked against the taxonomy by the (future) pipeline,
    not here: this type only knows its own shape, not the taxonomy's contents (G2)."""

    model_config = ConfigDict(extra="forbid")

    product_type: str | None = None
    category_id: str | None = None
    attributes: dict[str, ExtractedAttribute] = {}
    visible_text: list[str] = []  # strings read from packaging/labels in the photos
    notes: str | None = None


@dataclass(frozen=True)
class ExtractionRequest:
    """Everything stage 1 needs for one product. Seller-supplied facts travel with the request
    so the prompt can tell the model they exist and must not be re-derived (docs/03 §11) — they
    are merged into the final catalog content afterward, never asked of the model itself."""

    product_name: str
    images: Sequence[ImageInput]
    seller_notes: str | None = None
    seller_attributes: dict[str, str] = field(default_factory=dict)


class ExtractionWarning(BaseModel):
    """A non-fatal finding. The guardrail pipeline (not built yet) will be the main producer of
    these, but the shape is stable now so callers already have something concrete to store and
    render — matches the `catalog_versions.warnings` column's row shape."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class AIJobResult(BaseModel):
    """What one extraction attempt produced, success or failure — the pipeline's own output
    contract. Distinct from the `ai_jobs` ORM row: that's persistence, this is the value the
    pipeline hands back to whatever called it (the worker, or a test)."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    extraction: ExtractionResult | None = None
    warnings: list[ExtractionWarning] = []
    usage: Usage | None = None
    error_code: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def _success_matches_payload(self) -> "AIJobResult":
        if self.success:
            if self.extraction is None:
                raise ValueError("a successful result must include an extraction")
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("a successful result must not carry an error")
        else:
            if self.extraction is not None:
                raise ValueError("a failed result must not include an extraction")
            if self.error_code is None:
                raise ValueError("a failed result must include an error_code")
        return self
