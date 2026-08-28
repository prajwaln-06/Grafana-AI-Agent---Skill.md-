"""
scripts/evaluate_catalog_retrieval.py

Phase 7: measures app/catalog/search.py's retrieval quality against a
representative question set, per the reference doc's Layer 2 testing
strategy ("correct metric retained; candidate count; ranking; ambiguous
questions; catalog miss") and the "10-metric onboarding experiment"
philosophy of measuring before tuning.

The question set (QUESTION_SET below) is NOT invented for this harness --
every (question, expected_metric) pair is copied verbatim from this
project's own source documentation: the "Typical User Questions" column
(node-exporter) and "Typical Query Intent" column (DCGM) of
prometheus_metrics.docx, the same document generator.py's Markdown
ultimately derives from. Using the project's own already-written intent
descriptions as the evaluation set means this harness measures against
real, pre-existing statements of what each metric is for, not questions
this script's author picked to make the results look good.

This is deliberately a MEASUREMENT tool, not a test with a pass/fail
recall threshold. Per the explicit Batch 2 review guidance: "do not tune
weights or add query-specific exceptions yet ... use the upcoming
evaluation/shadow-mode phase to measure retrieval recall and candidate
quality against a representative question set before changing the
scoring model." tests/test_catalog_retrieval_eval.py (Phase 7's test
file) asserts this harness runs and produces well-formed output; it does
NOT assert a minimum recall number, so that changing search.py's weights
later doesn't require silently loosening a test to keep it passing.

Usage: `python -m scripts.evaluate_catalog_retrieval` (from the repo
root) prints a full report; import `evaluate()` to use the numbers
programmatically (e.g. from a test, or a future shadow-mode dashboard).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.catalog.generator import generate_catalog
from app.catalog.rules import apply_rules
from app.catalog.schema import Catalog
from app.catalog.search import search
from app.config import Settings
from app.pipeline import NarrowingDecision, catalog_narrowing_candidates

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SKILLS_ROOT = REPO_ROOT / "skills"

# Batch 4 (Phase 10) default confidence floor -- see app/config.py's
# `catalog_narrow_min_score` for the full reasoning. Read off the real
# Settings default here (not a second hard-coded literal) so this harness
# always measures against whatever production would actually do.
DEFAULT_MIN_CONFIDENT_SCORE = Settings.model_fields["catalog_narrow_min_score"].default

# (question, expected_metric_name) -- verbatim from prometheus_metrics.docx's
# "Typical User Questions" (node-exporter) / "Typical Query Intent" (DCGM)
# columns. Where one metric has multiple comma-separated intents listed in
# the source table, each is kept as its own separate question so the
# question set's granularity matches how a real user would actually phrase
# one question at a time.
QUESTION_SET: tuple[tuple[str, str], ...] = (
    # -- node-exporter --
    ("CPU utilization", "node_cpu_seconds_total"),
    ("CPU busy", "node_cpu_seconds_total"),
    ("CPU idle", "node_cpu_seconds_total"),
    ("Is the system overloaded?", "node_load1"),
    ("Sustained system load", "node_load5"),
    ("Long-term load trend", "node_load15"),
    ("High scheduling activity", "node_context_switches_total"),
    ("Interrupt-heavy workloads", "node_intr_total"),
    ("Total RAM", "node_memory_MemTotal_bytes"),
    ("Available memory", "node_memory_MemAvailable_bytes"),
    ("Free RAM", "node_memory_MemFree_bytes"),
    ("Cached memory", "node_memory_Cached_bytes"),
    ("Buffer cache usage", "node_memory_Buffers_bytes"),
    ("Total swap", "node_memory_SwapTotal_bytes"),
    ("Swap usage", "node_memory_SwapFree_bytes"),
    ("Total disk capacity", "node_filesystem_size_bytes"),
    ("Free disk space", "node_filesystem_avail_bytes"),
    ("Remaining capacity", "node_filesystem_free_bytes"),
    # -- dcgm-exporter --
    ("GPU utilization", "DCGM_FI_DEV_GPU_UTIL"),
    ("idle GPU", "DCGM_FI_DEV_GPU_UTIL"),
    ("Compute engine utilization", "DCGM_FI_PROF_GR_ENGINE_ACTIVE"),
    ("GPU memory usage", "DCGM_FI_DEV_FB_USED"),
    ("Available GPU memory", "DCGM_FI_DEV_FB_FREE"),
    ("Memory bandwidth utilization", "DCGM_FI_DEV_MEM_COPY_UTIL"),
    ("GPU temperature", "DCGM_FI_DEV_GPU_TEMP"),
    ("Memory temperature", "DCGM_FI_DEV_MEMORY_TEMP"),
    ("Power consumption", "DCGM_FI_DEV_POWER_USAGE"),
    ("Detect power throttling over time", "DCGM_FI_DEV_POWER_VIOLATION"),
    ("Current GPU frequency", "DCGM_FI_DEV_SM_CLOCK"),
    ("Current memory frequency", "DCGM_FI_DEV_MEM_CLOCK"),
    ("PCIe transmit bandwidth", "DCGM_FI_PROF_PCIE_TX_BYTES"),
    ("PCIe receive bandwidth", "DCGM_FI_PROF_PCIE_RX_BYTES"),
    ("NVLink transmit bandwidth", "DCGM_FI_PROF_NVLINK_TX_BYTES"),
    ("NVLink receive bandwidth", "DCGM_FI_PROF_NVLINK_RX_BYTES"),
    ("Tensor Core activity", "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"),
    ("Double-precision workload", "DCGM_FI_PROF_PIPE_FP64_ACTIVE"),
    ("Single-precision workload", "DCGM_FI_PROF_PIPE_FP32_ACTIVE"),
    ("Mixed-precision workload", "DCGM_FI_PROF_PIPE_FP16_ACTIVE"),
    ("Memory-bound workload", "DCGM_FI_PROF_DRAM_ACTIVE"),
    ("Error trend", "DCGM_FI_DEV_ECC_SBE_VOL_TOTAL"),
    ("Critical hardware errors", "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL"),
    ("Memory degradation", "DCGM_FI_DEV_RETIRED_SBE"),
    ("Serious hardware degradation", "DCGM_FI_DEV_RETIRED_DBE"),
    ("Pending memory failures", "DCGM_FI_DEV_RETIRED_PENDING"),
    ("NVLink health", "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL"),
    ("Link stability", "DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL"),
)

# Batch 4 (Phase 10): representative COMPOUND questions -- per the review
# note that Batch 4 must investigate "representative multi-metric
# questions". Each one is two of QUESTION_SET's own phrases above joined
# with "and", not invented wording, covering same-exporter/same-category,
# same-exporter/cross-category, and cross-exporter combinations, since a
# real compound question (per pipeline.py's own "Partial datasource
# coverage" handling) isn't guaranteed to stay within one domain file.
MULTI_METRIC_QUESTION_SET: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("CPU utilization and available memory",
     ("node_cpu_seconds_total", "node_memory_MemAvailable_bytes")),
    ("Free disk space and swap usage",
     ("node_filesystem_avail_bytes", "node_memory_SwapFree_bytes")),
    ("Interrupt-heavy workloads and high scheduling activity",
     ("node_intr_total", "node_context_switches_total")),
    ("Is the system overloaded? Total RAM",
     ("node_load1", "node_memory_MemTotal_bytes")),
    ("GPU utilization and GPU temperature",
     ("DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_GPU_TEMP")),
    ("GPU memory usage and power consumption",
     ("DCGM_FI_DEV_FB_USED", "DCGM_FI_DEV_POWER_USAGE")),
    ("PCIe transmit bandwidth and NVLink transmit bandwidth",
     ("DCGM_FI_PROF_PCIE_TX_BYTES", "DCGM_FI_PROF_NVLINK_TX_BYTES")),
    ("CPU utilization and GPU utilization",
     ("node_cpu_seconds_total", "DCGM_FI_DEV_GPU_UTIL")),
)


@dataclass(frozen=True)
class QuestionResult:
    question: str
    expected_metric: str
    candidate_names: tuple[str, ...]
    rank: int | None  # 1-based position of expected_metric in candidates, or None


@dataclass(frozen=True)
class EvaluationReport:
    top_n: int
    results: tuple[QuestionResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def hit_at_top_n(self) -> int:
        return sum(1 for r in self.results if r.rank is not None)

    @property
    def hit_at_1(self) -> int:
        return sum(1 for r in self.results if r.rank == 1)

    @property
    def misses(self) -> tuple[QuestionResult, ...]:
        return tuple(r for r in self.results if r.rank is None)

    @property
    def recall_at_top_n(self) -> float:
        return self.hit_at_top_n / self.total if self.total else 0.0

    @property
    def recall_at_1(self) -> float:
        return self.hit_at_1 / self.total if self.total else 0.0

    @property
    def mean_reciprocal_rank(self) -> float:
        if not self.results:
            return 0.0
        return sum((1.0 / r.rank) if r.rank else 0.0 for r in self.results) / len(self.results)


def evaluate(
    catalog: Catalog,
    question_set: tuple[tuple[str, str], ...] = QUESTION_SET,
    top_n: int = 5,
) -> EvaluationReport:
    """Runs every (question, expected_metric) pair in `question_set`
    through search.py's search(), recording where (if anywhere) within the
    top `top_n` candidates the expected metric landed. Pure measurement --
    does not modify the catalog or search.py, and asserts nothing itself."""
    results = []
    for question, expected in question_set:
        candidates = search(catalog, question, top_n=top_n)
        candidate_names = tuple(c.entry.name for c in candidates)
        rank = candidate_names.index(expected) + 1 if expected in candidate_names else None
        results.append(QuestionResult(
            question=question, expected_metric=expected,
            candidate_names=candidate_names, rank=rank,
        ))
    return EvaluationReport(top_n=top_n, results=tuple(results))


def format_report(report: EvaluationReport) -> str:
    lines = [
        f"Catalog retrieval evaluation (top_n={report.top_n})",
        f"  questions:        {report.total}",
        f"  recall@1:         {report.hit_at_1}/{report.total} ({report.recall_at_1:.0%})",
        f"  recall@{report.top_n}:         {report.hit_at_top_n}/{report.total} ({report.recall_at_top_n:.0%})",
        f"  mean reciprocal rank: {report.mean_reciprocal_rank:.3f}",
    ]
    if report.misses:
        lines.append(f"  misses ({len(report.misses)}):")
        for r in report.misses:
            lines.append(f"    - {r.question!r} (expected {r.expected_metric}) -> {list(r.candidate_names)}")
    return "\n".join(lines)


# ---- Batch 4 (Phase 10): reference-level recall & false-positive/confidence -----
#
# The metric-level `evaluate()` above answers "did search() rank the right
# METRIC in the top N candidates" -- useful for judging search.py in
# isolation, but it is not what actually determines whether Phase 9's
# assisted routing stays safe, because narrowing acts on reference_path
# sets, not metric names, and (per Batch 4's review finding) a non-empty
# search result is not automatically a safe basis for narrowing. The
# functions below measure the ACTUAL narrowing decision
# (`app.pipeline.catalog_narrowing_candidates`) rather than re-deriving a
# parallel approximation of it, so this harness can never silently drift
# from what production does.


@dataclass(frozen=True)
class ReferenceLevelResult:
    question: str
    expected_metric: str
    expected_reference_path: str | None
    decision: NarrowingDecision

    @property
    def outcome(self) -> str:
        """One of:
          - "no_reference": the expected metric itself has no reference_path
            in the catalog (shouldn't happen for the approved question set;
            kept distinct rather than silently miscounted as something else).
          - "safe_hit": narrowing engaged (confident) AND kept the correct
            reference row.
          - "unsafe_narrow": narrowing engaged (confident) but DROPPED the
            correct reference row -- the exact danger Batch 4's finding
            described: a confident-looking, non-empty result that would
            still steer the Router away from the only right answer.
          - "true_miss": zero candidates at all -- already safe by
            construction (falls back to the full table).
          - "low_confidence_fallback": candidates existed but none cleared
            `min_confident_score` -- also safe (same fallback), but worth
            counting separately from a true miss to see how often the
            confidence floor is the thing doing the protecting.
        """
        if self.expected_reference_path is None:
            return "no_reference"
        if not self.decision.confident:
            return "true_miss" if self.decision.top_score is None else "low_confidence_fallback"
        if self.expected_reference_path in self.decision.suggested_paths:
            return "safe_hit"
        return "unsafe_narrow"


@dataclass(frozen=True)
class ReferenceLevelReport:
    min_confident_score: float
    results: tuple[ReferenceLevelResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    def _count(self, outcome: str) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    @property
    def safe_hits(self) -> int:
        return self._count("safe_hit")

    @property
    def unsafe_narrows(self) -> int:
        return self._count("unsafe_narrow")

    @property
    def true_misses(self) -> int:
        return self._count("true_miss")

    @property
    def low_confidence_fallbacks(self) -> int:
        return self._count("low_confidence_fallback")

    @property
    def reference_visible_rate(self) -> float:
        """Fraction of questions where the Router still gets to see the
        correct reference row after narrowing -- via a correct suggestion,
        or via deferring to the full table on a miss/low-confidence hit.
        The headline safety number: 1.0 minus this is exactly the rate at
        which assisted routing could steer the Router away from the only
        correct reference."""
        if not self.total:
            return 0.0
        return (self.safe_hits + self.true_misses + self.low_confidence_fallbacks) / self.total

    @property
    def unsafe_narrow_rate(self) -> float:
        return self.unsafe_narrows / self.total if self.total else 0.0

    @property
    def unsafe_narrow_cases(self) -> tuple[ReferenceLevelResult, ...]:
        return tuple(r for r in self.results if r.outcome == "unsafe_narrow")


def evaluate_reference_level(
    catalog: Catalog,
    question_set: tuple[tuple[str, str], ...] = QUESTION_SET,
    min_confident_score: float = DEFAULT_MIN_CONFIDENT_SCORE,
) -> ReferenceLevelReport:
    """Runs every (question, expected_metric) pair through the real
    production narrowing decision (`catalog_narrowing_candidates`) and
    classifies the outcome per `ReferenceLevelResult.outcome`. Pure
    measurement -- asserts nothing, modifies nothing."""
    results = []
    for question, expected in question_set:
        entry = catalog.get(expected)
        expected_reference_path = entry.reference_path if entry else None
        decision = catalog_narrowing_candidates(catalog, question, min_confident_score)
        results.append(ReferenceLevelResult(
            question=question, expected_metric=expected,
            expected_reference_path=expected_reference_path, decision=decision,
        ))
    return ReferenceLevelReport(min_confident_score=min_confident_score, results=tuple(results))


def format_reference_level_report(report: ReferenceLevelReport) -> str:
    lines = [
        f"Reference-level narrowing safety (min_confident_score={report.min_confident_score})",
        f"  questions:                 {report.total}",
        f"  reference visible rate:    {report.safe_hits + report.true_misses + report.low_confidence_fallbacks}/{report.total} ({report.reference_visible_rate:.0%})",
        f"    - safe_hit (confident, correct):        {report.safe_hits}",
        f"    - true_miss (0 candidates, fallback):   {report.true_misses}",
        f"    - low_confidence (fallback):            {report.low_confidence_fallbacks}",
        f"  unsafe narrows (confident but WRONG):      {report.unsafe_narrows}/{report.total} ({report.unsafe_narrow_rate:.0%})",
    ]
    if report.unsafe_narrow_cases:
        lines.append("  unsafe narrow cases (correct reference dropped despite a confident hit):")
        for r in report.unsafe_narrow_cases:
            lines.append(
                f"    - {r.question!r} (expected {r.expected_metric} -> {r.expected_reference_path}) "
                f"top_score={r.decision.top_score} suggested={sorted(r.decision.suggested_paths)}"
            )
    return "\n".join(lines)


@dataclass(frozen=True)
class MultiMetricResult:
    question: str
    expected_metrics: tuple[str, ...]
    decision: NarrowingDecision
    missing_reference_paths: tuple[str, ...]

    @property
    def all_visible(self) -> bool:
        return len(self.missing_reference_paths) == 0


@dataclass(frozen=True)
class MultiMetricReport:
    results: tuple[MultiMetricResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def fully_covered(self) -> int:
        return sum(1 for r in self.results if r.all_visible)

    @property
    def coverage_rate(self) -> float:
        return self.fully_covered / self.total if self.total else 0.0

    @property
    def incomplete_cases(self) -> tuple[MultiMetricResult, ...]:
        return tuple(r for r in self.results if not r.all_visible)


def evaluate_multi_metric(
    catalog: Catalog,
    question_set: tuple[tuple[str, tuple[str, ...]], ...] = MULTI_METRIC_QUESTION_SET,
    min_confident_score: float = DEFAULT_MIN_CONFIDENT_SCORE,
) -> MultiMetricReport:
    """For each compound question, checks whether EVERY expected metric's
    reference row survives the real narrowing decision -- a confident hit
    that only keeps some of a compound question's needed rows is exactly
    as unsafe as dropping the only row a single-metric question needed."""
    results = []
    for question, expected_metrics in question_set:
        expected_refs = tuple(
            entry.reference_path
            for entry in (catalog.get(m) for m in expected_metrics)
            if entry and entry.reference_path
        )
        decision = catalog_narrowing_candidates(catalog, question, min_confident_score)
        if decision.confident:
            missing = tuple(r for r in expected_refs if r not in decision.suggested_paths)
        else:
            missing = ()  # fallback to full table -- every reference is trivially visible
        results.append(MultiMetricResult(
            question=question, expected_metrics=expected_metrics,
            decision=decision, missing_reference_paths=missing,
        ))
    return MultiMetricReport(results=tuple(results))


def format_multi_metric_report(report: MultiMetricReport) -> str:
    lines = [
        "Multi-metric (compound question) narrowing coverage",
        f"  questions:        {report.total}",
        f"  fully covered:    {report.fully_covered}/{report.total} ({report.coverage_rate:.0%})",
    ]
    if report.incomplete_cases:
        lines.append("  incomplete cases (at least one expected reference not visible):")
        for r in report.incomplete_cases:
            lines.append(
                f"    - {r.question!r} (expected {list(r.expected_metrics)}) "
                f"missing={list(r.missing_reference_paths)} top_score={r.decision.top_score}"
            )
    return "\n".join(lines)


def main() -> int:  # pragma: no cover -- thin CLI wrapper
    catalog = apply_rules(generate_catalog(DEFAULT_SKILLS_ROOT))
    print(format_report(evaluate(catalog)))
    print()
    print(format_reference_level_report(evaluate_reference_level(catalog)))
    print()
    print(format_multi_metric_report(evaluate_multi_metric(catalog)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
