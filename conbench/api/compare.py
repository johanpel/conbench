import collections
import logging
import math
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import flask as f
import sqlalchemy as s

import conbench.units
from conbench.dbsession import current_session
from conbench.numstr import numstr
from conbench.types import THistFingerprint

from ..api import rule
from ..api._endpoint import ApiEndpoint, maybe_login_required
from ..api._resp import resp429
from ..entities.benchmark_result import BenchmarkResult
from ..entities.history import set_z_scores
from ..hacks import set_display_benchmark_name, set_display_case_permutation

log = logging.getLogger(__name__)

DEFAULT_PAIRWISE_PERCENT_THRESHOLD = 5.0
DEFAULT_Z_SCORE_THRESHOLD = 5.0


# Context: https://github.com/voltrondata-labs/arrow-benchmarks-ci/issues/124
# The compare endpoint can be rather resource-heavy. This here is a pragmatic
# QoS solution / DoS protection: only ever have one of the request-handling
# threads work on an /api/compare/... request.
# Under a lot of API pressure this helps keep the system going. In that case,
# however, individual requests may be responded to after a longer waiting time.
# In those contexts, it makes sense to apply large HTTP request timeout
# constants.
_semaphore_compare_get = threading.BoundedSemaphore(1)


@contextmanager
def _sem_acquire(sem: threading.BoundedSemaphore, timeout: float):
    """
    The stdlib-provided context manager does not allow for setting a timeout.
    Build our own context manager that.
    """
    acquired = sem.acquire(timeout=timeout)
    try:
        yield acquired
    finally:
        if acquired:
            sem.release()


def _parse_two_ids_or_abort(compare_ids: str) -> Tuple[str, str]:
    """Split a string of the form "<id>...<id>" into two strings, or abort if it's
    not of the correct form.

    The input string is the standard way to specify two IDs for the compare API.

    The two IDs can be either two run IDs or two benchmark result IDs.
    """
    if "..." not in compare_ids:
        f.abort(404, description="last URL path segment must be of pattern <id>...<id>")

    baseline_id, contender_id = compare_ids.split("...", 1)

    if not baseline_id:
        f.abort(404, description="empty baseline ID")

    if not contender_id:
        f.abort(404, description="empty contender ID")

    return baseline_id, contender_id


def _get_threshold_args_from_request() -> Tuple[Optional[float], Optional[float]]:
    """Attempt to read query parameters from the request context.

    Returns a tuple of (threshold, threshold_z), where each value is either
    a float or None.
    """
    threshold = f.request.args.get("threshold")
    if threshold is not None:
        threshold = float(threshold)

    threshold_z = f.request.args.get("threshold_z")
    if threshold_z is not None:
        threshold_z = float(threshold_z)

    return threshold, threshold_z


def _round(value: float) -> Optional[float]:
    """
    Round a float to 4 significant figures, or NaN if the input is NaN

    Note(JP): Returning math.nan translates to `null` in JSON output, but to
    empty string "" in naive jinja template interpolation. Using `None` achieves
    `null` in JSON, too, but can better we dealt with in jinja template
    interpolation. For the UI we should really do
    https://github.com/conbench/conbench/issues/1334 though.

    """
    return None if math.isnan(value) else float(numstr(value, sigfigs=4))


if TYPE_CHECKING:

    class AugmentedBenchmarkResult(BenchmarkResult):
        """For type hints only. This represents a BenchmarkResult that has been
        augmented in-place with the following augmentation functions, as is done in this
        module:

        - set_z_scores()
        - set_display_benchmark_name()
        - set_display_case_permutation()

        TODO: remove this and replace with actual BenchmarkResult properties.
        """

        display_bmname: str
        display_case_perm: str
        z_score: Optional[float]


class UnmatchingUnitsError(Exception):
    pass


class BenchmarkResultComparator:
    """Data model class to hold the comparison of two BenchmarkResults."""

    def __init__(
        self,
        baseline: Optional["AugmentedBenchmarkResult"],
        contender: Optional["AugmentedBenchmarkResult"],
        threshold: Optional[float],
        threshold_z: Optional[float],
    ) -> None:
        # What do we know here? Is one of baseline and contender guaranteed
        # to not be None?

        do_comparison = True

        if not all([baseline, contender]):
            do_comparison = False

        # Can/should we assume that only non-errored results are added into a
        # comparator? Then we can assume that the unit is set. Turns out: no,
        # either might be errored. Thinking about possible code paths. Can
        # `baseline` for example be an errored benchmark result while contender
        # is `None`?

        if baseline and baseline.is_failed:
            do_comparison = False

        if contender and contender.is_failed:
            do_comparison = False

        # `self.unit` is `None` for all cases except for when both contender
        # and baseline are non-failed results and have the same unit; then it's
        # that unit.
        self.unit: Optional[conbench.units.TUnit] = None

        if do_comparison:
            assert baseline
            assert contender
            assert baseline.unit
            assert contender.unit

            if baseline.unit != contender.unit:
                raise UnmatchingUnitsError(
                    "Benchmark units do not match. Benchmark result with ID "
                    f"'{baseline.id}' has unit '{baseline.unit}' and benchmark result "
                    f"with ID '{contender.id}' has unit '{contender.unit}'."
                )

            # Take `baseline.unitsymbol` here to get the TUnit type.
            assert baseline.unitsymbol
            self.unit = baseline.unitsymbol

        # Signal to the mathy methods if all conditions are met for performing
        # numeric comparison.
        self.do_comparison = do_comparison

        self.baseline = baseline
        self.contender = contender
        self.threshold = (
            float(threshold)
            if threshold is not None
            else DEFAULT_PAIRWISE_PERCENT_THRESHOLD
        )
        self.threshold_z = (
            float(threshold_z) if threshold_z is not None else DEFAULT_Z_SCORE_THRESHOLD
        )

    @property
    def less_is_better(self) -> Optional[bool]:
        """
        This is only defined when this comparison has a unit.
        """
        if self.unit is None:
            return None
        return conbench.units.less_is_better(self.unit)

    @staticmethod
    def result_info(result: Optional["AugmentedBenchmarkResult"]) -> Optional[dict]:
        if not result:
            return None

        return {
            "benchmark_result_id": result.id,
            "benchmark_name": result.display_bmname,
            "case_permutation": result.display_case_perm,
            "language": result.context.tags.get("benchmark_language", "unknown"),
            "single_value_summary": None
            if math.isnan(result.svs)
            else _round(result.svs),
            "error": result.error,
            "batch_id": result.batch_id,
            "run_id": result.run_id,
            "tags": result.case.tags,
        }

    @property
    def pairwise_analysis(self) -> Optional[dict]:
        if not self.do_comparison:
            return None

        assert self.baseline
        assert self.contender

        if self.baseline.svs == 0:
            # Don't divide by zero.
            # On the other hand maybe we should not have results
            # reporting zero of anything:
            # https://github.com/conbench/conbench/issues/532
            return None

        relative_change = (self.contender.svs - self.baseline.svs) / abs(
            self.baseline.svs
        )
        if self.less_is_better:
            relative_change = relative_change * -1

        percent_change = relative_change * 100.0
        regression_indicated = -percent_change > self.threshold
        improvement_indicated = percent_change > self.threshold

        percent_change_for_output = _round(relative_change * 100.0)
        return {
            "percent_change": percent_change_for_output,
            "percent_threshold": self.threshold,
            "regression_indicated": regression_indicated,
            "improvement_indicated": improvement_indicated,
        }

    @property
    def lookback_z_score_analysis(self) -> Optional[dict]:
        if not self.do_comparison:
            return None

        assert self.baseline
        assert self.contender

        if self.contender.z_score is None or math.isnan(self.contender.z_score):
            return None

        regression_indicated = -self.contender.z_score > self.threshold_z
        improvement_indicated = self.contender.z_score > self.threshold_z

        return {
            "z_threshold": self.threshold_z,
            "z_score": _round(self.contender.z_score),
            "regression_indicated": regression_indicated,
            "improvement_indicated": improvement_indicated,
        }

    @property
    def _dict_for_api_json(self) -> dict:
        return {
            # How is 'unit' here specified? Let's specify it: If both results
            # are not failed and have the same unit then this here is the unit
            # symbol. Else it's null/None.
            "unit": self.unit,
            "less_is_better": self.less_is_better,
            "baseline": self.result_info(self.baseline),
            "contender": self.result_info(self.contender),
            "analysis": {
                "pairwise": self.pairwise_analysis,
                # Watch out: self.lookback_z_score_analysis can be dict or
                # None, and both needs to be handled in the UI (for now).
                "lookback_z_score": self.lookback_z_score_analysis,
            },
        }


class CompareBenchmarkResultsAPI(ApiEndpoint):
    @staticmethod
    def _get_a_result(benchmark_result_id: str) -> BenchmarkResult:
        """Get a benchmark result by ID, or abort if it doesn't exist."""
        benchmark_result = BenchmarkResult.get(benchmark_result_id)
        if not benchmark_result:
            f.abort(
                404,
                description=f"no benchmark result found with ID: '{benchmark_result_id}'",
            )
        return benchmark_result

    @maybe_login_required
    def get(self, compare_ids: str) -> f.Response:
        """
        ---
        description: |
            Compare a baseline and contender benchmark result.

            Returns basic information about the baseline and contender benchmark results
            as well as some analyses comparing the performance of the contender to the
            baseline.

            The `pairwise` analysis computes the percentage difference of the
            contender's mean value to the baseline's mean value. The reported difference
            is signed such that a more negative value indicates more of a performance
            regression. This difference is then thresholded such that values more
            extreme than the threshold are marked as `regression_indicated` or
            `improvement_indicated`. The threshold is 5.0% by default, but can be
            changed via the `threshold` query parameter, which should be a positive
            percent value.

            The `pairwise` analysis may be `null` if either benchmark result does not
            have a mean value, or if the baseline result's mean value is 0.

            The `lookback_z_score` analysis compares the contender's mean value to a
            baseline distribution of benchmark result mean values (from the git history
            of the baseline result) via the so-called lookback z-score method. The
            reported z-score is also signed such that a more negative value indicates
            more of a performance regression, and thresholded. The threshold z-score is
            5.0 by default, but can be changed via the `threshold_z` query parameter,
            which should be a positive number.

            The `lookback_z_score` analysis object may be `null` if a z-score cannot be
            computed for the contender benchmark result, due to not finding a baseline
            distribution that matches the contender benchmark result. More details about
            this analysis can be found at
            https://conbench.github.io/conbench/pages/lookback_zscore.html.

            If either benchmark result is not found, this endpoint will raise a 404. If
            the benchmark results don't have the same unit, this endpoint will raise a
            400. Otherwise, you may compare any two benchmark results, no matter if
            their cases, contexts, hardwares, or even repositories don't match.
        responses:
            "200": "CompareEntity"
            "400": "400"
            "401": "401"
            "404": "404"
        parameters:
          - name: compare_ids
            in: path
            schema:
                type: string
            example: <baseline_id>...<contender_id>
          - in: query
            name: threshold
            schema:
              type: number
          - in: query
            name: threshold_z
            schema:
              type: number
        tags:
          - Comparisons
        """
        # Note(JP): a threading.BoundedSemaphore acquired and released with a
        # context manager. Assumes the web application to be deployed in a
        # model with N request-handling threads per process.
        with _sem_acquire(_semaphore_compare_get, 0.1) as acquired:
            if acquired:
                return self._get(compare_ids)

            return resp429("doing other /compare work, retry soon")

    def _get(self, compare_ids: str) -> f.Response:
        baseline_result_id, contender_result_id = _parse_two_ids_or_abort(compare_ids)
        threshold, threshold_z = _get_threshold_args_from_request()
        baseline_result = self._get_a_result(baseline_result_id)
        contender_result = self._get_a_result(contender_result_id)

        baseline_commit = baseline_result.commit

        if baseline_commit:
            set_z_scores(
                contender_benchmark_results=[contender_result],
                baseline_commit=baseline_commit,
            )
        else:
            # If the baseline run is not associated with a commit, skip z-scores. The
            # ["analysis"]["lookback_z_score"] dict will then be null in the response.
            contender_result.z_score = None

        # TODO: define dynamic properties on BenchmarkResult instead of mutating these
        # objects here in-place.
        set_display_case_permutation(baseline_result)
        set_display_case_permutation(contender_result)
        set_display_benchmark_name(baseline_result)
        set_display_benchmark_name(contender_result)

        try:
            comparator = BenchmarkResultComparator(
                baseline=baseline_result,
                contender=contender_result,
                threshold=threshold,
                threshold_z=threshold_z,
            )
        except UnmatchingUnitsError as e:
            f.abort(400, description=str(e))

        return f.jsonify(comparator._dict_for_api_json)


# from filprofiler.api import profile as filprofile


class CompareRunsAPI(ApiEndpoint):
    @staticmethod
    def _get_all_results_for_a_run(run_id: str) -> List[BenchmarkResult]:
        """Get all benchmark results for a run. Abort if the run doesn't exist or if
        there are no results for the run.
        """
        # TODO: fairly slow; e.g. 3 seconds for 3500 results. Maybe we select only the
        # columns we need. Today it's joining to case, context, hardware, and commit
        result = current_session.scalars(
            s.select(BenchmarkResult).where(BenchmarkResult.run_id == run_id)
        ).all()

        if not result:
            f.abort(
                404, description=f"no benchmark results found for run ID: '{run_id}'"
            )

        return list(result)

    @staticmethod
    def _join_results(
        baseline_results: List[BenchmarkResult],
        contender_results: List[BenchmarkResult],
    ) -> List[Tuple[Optional[BenchmarkResult], Optional[BenchmarkResult]]]:
        """Do a full outer join of two lists of benchmark results, pairing by history
        fingerprint.

        If a history fingerprint is present in one list but not the other, it will still
        be included but the other tuple element will be None.

        If there are multiple results for a history fingerprint in both lists, a
        cartesian product of them all will be returned.
        """
        baseline_results_by_fing: Dict[
            THistFingerprint, List[Optional[BenchmarkResult]]
        ] = collections.defaultdict(list)
        contender_results_by_fing: Dict[
            THistFingerprint, List[Optional[BenchmarkResult]]
        ] = collections.defaultdict(list)

        for result in baseline_results:
            baseline_results_by_fing[result.history_fingerprint].append(result)

        for result in contender_results:
            contender_results_by_fing[result.history_fingerprint].append(result)

        joined_results: List[
            Tuple[Optional[BenchmarkResult], Optional[BenchmarkResult]]
        ] = []

        for fing in set(baseline_results_by_fing) | set(contender_results_by_fing):
            for baseline_result in baseline_results_by_fing[fing] or [None]:
                for contender_result in contender_results_by_fing[fing] or [None]:
                    joined_results.append((baseline_result, contender_result))

        return joined_results

    @maybe_login_required
    def get(self, compare_ids: str) -> f.Response:
        """
        ---
        description: |
            Compare all benchmark results between two runs.

            This endpoint will return a list of comparison objects, pairing benchmark
            results from the given baseline and contender runs that have the same
            history fingerprint. The comparison object is the same as the `GET
            /api/compare/benchmark-results/` response; see that endpoint's documentation
            for details.

            If a benchmark result from one run does not have a matching result in the
            other run, a comparison object will still be returned for it, with the other
            result's information replaced by `null` and each analysis also `null`.

            If a benchmark result from one run has multiple matching results in the
            other run, a comparison object will be returned for each match. Filtering
            must be done clientside.
        responses:
            "200": "CompareList"
            "401": "401"
            "404": "404"
        parameters:
          - name: compare_ids
            in: path
            schema:
                type: string
            example: <baseline_id>...<contender_id>
          - in: query
            name: threshold
            schema:
              type: number
          - in: query
            name: threshold_z
            schema:
              type: number
        tags:
          - Comparisons
        """
        # Note(JP): a threading.BoundedSemaphore acquired and released with a
        # context manager. Assumes the web application to be deployed in a
        # model with N request-handling threads per process. Wait in line for
        # a small number of seconds
        with _sem_acquire(_semaphore_compare_get, 0.1) as acquired:
            if acquired:
                return self._get(compare_ids)
            return resp429("doing other /compare work, retry soon")

    def _get(self, compare_ids: str) -> f.Response:
        baseline_run_id, contender_run_id = _parse_two_ids_or_abort(compare_ids)
        threshold, threshold_z = _get_threshold_args_from_request()
        baseline_results = self._get_all_results_for_a_run(baseline_run_id)
        contender_results = self._get_all_results_for_a_run(contender_run_id)

        # All baseline results share a run (and therefore a commit).
        # The baseline_results list is guaranteed to be non-empty.
        baseline_commit = baseline_results[0].commit

        if baseline_commit:
            set_z_scores(
                contender_benchmark_results=contender_results,
                baseline_commit=baseline_commit,
            )
        else:
            # If the baseline run is not associated with a commit, skip z-scores. The
            # ["analysis"]["lookback_z_score"] dict will then be null in the response.
            for result in contender_results:
                result.z_score = None

        for benchmark_result in baseline_results:
            # TODO: define dynamic properties on BenchmarkResult instead of
            # mutating these objects here in-place.
            set_display_benchmark_name(benchmark_result)
            set_display_case_permutation(benchmark_result)

        for benchmark_result in contender_results:
            set_display_benchmark_name(benchmark_result)
            set_display_case_permutation(benchmark_result)

        comparators: List[BenchmarkResultComparator] = []
        for baseline_result, contender_result in self._join_results(
            baseline_results, contender_results
        ):
            try:
                comparators.append(
                    BenchmarkResultComparator(
                        baseline=baseline_result,
                        contender=contender_result,
                        threshold=threshold,
                        threshold_z=threshold_z,
                    )
                )
            except UnmatchingUnitsError:
                # Don't return comparisons if their units mismatch.
                pass

        return f.jsonify([comparator._dict_for_api_json for comparator in comparators])


compare_benchmark_results_view = CompareBenchmarkResultsAPI.as_view(
    "compare-benchmark-results"
)
compare_runs_view = CompareRunsAPI.as_view("compare-runs")

rule(
    "/compare/benchmark-results/<compare_ids>/",
    view_func=compare_benchmark_results_view,
    methods=["GET"],
)
rule(
    "/compare/runs/<compare_ids>/",
    view_func=compare_runs_view,
    methods=["GET"],
)
