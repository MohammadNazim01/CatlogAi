"""AI domain types (Step 10): the extraction contract and its invariants."""

import pytest
from pydantic import ValidationError

from app.ai.base import ImageInput, Usage
from app.ai.extraction import (
    AIJobResult,
    Confidence,
    Evidence,
    ExtractedAttribute,
    ExtractionRequest,
    ExtractionResult,
    ExtractionWarning,
)


class TestExtractedAttribute:
    def test_a_known_value_with_evidence_is_valid(self) -> None:
        attr = ExtractedAttribute(
            value="Blue", evidence=Evidence.IMAGE_VISIBLE, confidence=Confidence.HIGH
        )
        assert attr.value == "Blue"

    def test_unknown_with_a_value_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="null exactly when"):
            ExtractedAttribute(value="Blue", evidence=Evidence.UNKNOWN)

    def test_non_unknown_with_a_null_value_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="null exactly when"):
            ExtractedAttribute(value=None, evidence=Evidence.IMAGE_TEXT)

    def test_unknown_with_no_value_is_valid(self) -> None:
        attr = ExtractedAttribute(value=None, evidence=Evidence.UNKNOWN)
        assert attr.value is None

    def test_extra_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExtractedAttribute.model_validate(
                {"value": "x", "evidence": "IMAGE_VISIBLE", "extra": "nope"}
            )


class TestExtractionResult:
    def test_minimal_result_uses_defaults(self) -> None:
        result = ExtractionResult()
        assert result.attributes == {}
        assert result.visible_text == []

    def test_round_trips_through_json(self) -> None:
        result = ExtractionResult(
            product_type="headphones",
            category_id="electronics.audio.headphones",
            attributes={
                "color": ExtractedAttribute(value="black", evidence=Evidence.IMAGE_VISIBLE),
                "material": ExtractedAttribute(value=None, evidence=Evidence.UNKNOWN),
            },
            visible_text=["Model X200"],
            notes="box partially obscures the left earcup",
        )
        again = ExtractionResult.model_validate_json(result.model_dump_json())
        assert again == result

    def test_extra_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionResult.model_validate({"unexpected_field": True})


class TestExtractionRequest:
    def test_holds_images_and_seller_context(self) -> None:
        req = ExtractionRequest(
            product_name="Wireless Mouse",
            images=[ImageInput(data=b"\xff\xd8", mime_type="image/jpeg")],
            seller_notes="ships in plain box",
            seller_attributes={"color": "black"},
        )
        assert req.product_name == "Wireless Mouse"
        assert len(req.images) == 1
        assert req.seller_attributes == {"color": "black"}

    def test_seller_context_is_optional(self) -> None:
        req = ExtractionRequest(product_name="Mug", images=[])
        assert req.seller_notes is None
        assert req.seller_attributes == {}


class TestAIJobResult:
    def test_successful_result_requires_an_extraction(self) -> None:
        with pytest.raises(ValidationError, match="must include an extraction"):
            AIJobResult(success=True)

    def test_successful_result_cannot_carry_an_error(self) -> None:
        with pytest.raises(ValidationError, match="must not carry an error"):
            AIJobResult(success=True, extraction=ExtractionResult(), error_code="X")

    def test_failed_result_requires_an_error_code(self) -> None:
        with pytest.raises(ValidationError, match="must include an error_code"):
            AIJobResult(success=False)

    def test_failed_result_cannot_include_an_extraction(self) -> None:
        with pytest.raises(ValidationError, match="must not include an extraction"):
            AIJobResult(success=False, error_code="X", extraction=ExtractionResult())

    def test_valid_success(self) -> None:
        result = AIJobResult(
            success=True,
            extraction=ExtractionResult(product_type="mug"),
            warnings=[ExtractionWarning(code="LOW_CONFIDENCE", message="few attributes found")],
            usage=Usage(input_tokens=100, output_tokens=50),
        )
        assert result.extraction is not None
        assert result.extraction.product_type == "mug"
        assert result.warnings[0].code == "LOW_CONFIDENCE"

    def test_valid_failure(self) -> None:
        result = AIJobResult(
            success=False, error_code="AI_OUTPUT_INVALID", error_message="bad json"
        )
        assert result.extraction is None
