import logging

import flask as f
import flask_login
import orjson
from sqlalchemy import select

import conbench.metrics
from conbench.dbsession import current_session

from ..api import rule
from ..api._docs import spec
from ..api._endpoint import ApiEndpoint, maybe_login_required
from ..entities._entity import NotFound
from ..entities.benchmark_result import (
    BenchmarkResult,
    BenchmarkResultFacadeSchema,
    BenchmarkResultSerializer,
    BenchmarkResultValidationError,
)
from ._resp import json_response_for_byte_sequence, resp400

log = logging.getLogger(__name__)


class BenchmarkValidationMixin:
    def validate_benchmark(self, schema):
        return self.validate(schema)


class BenchmarkEntityAPI(ApiEndpoint, BenchmarkValidationMixin):
    serializer = BenchmarkResultSerializer()
    schema = BenchmarkResultFacadeSchema()

    def _get(self, benchmark_result_id):
        try:
            benchmark_result = BenchmarkResult.one(id=benchmark_result_id)
        except NotFound:
            self.abort_404_not_found()
        return benchmark_result

    @maybe_login_required
    def get(self, benchmark_result_id):
        """
        ---
        description: |
            Get a specific benchmark result.
        responses:
            "200": "BenchmarkEntity"
            "401": "401"
            "404": "404"
        parameters:
          - name: benchmark_result_id
            in: path
            schema:
                type: string
        tags:
          - Benchmarks
        """
        benchmark_result = self._get(benchmark_result_id)
        return self.serializer.one.dump(benchmark_result)

    @flask_login.login_required
    def put(self, benchmark_result_id):
        """
        ---
        description: Edit a benchmark result.
        responses:
            "200": "BenchmarkEntity"
            "401": "401"
            "404": "404"
        parameters:
          - name: benchmark_result_id
            in: path
            schema:
                type: string
        requestBody:
            content:
                application/json:
                    schema: BenchmarkResultUpdate
        tags:
          - Benchmarks
        """
        benchmark_result = self._get(benchmark_result_id)
        data = self.validate_benchmark(self.schema.update)
        benchmark_result.update(data)
        return self.serializer.one.dump(benchmark_result)

    @flask_login.login_required
    def delete(self, benchmark_result_id):
        """
        ---
        description: Delete a benchmark result.
        responses:
            "204": "204"
            "401": "401"
            "404": "404"
        parameters:
          - name: benchmark_result_id
            in: path
            schema:
                type: string
        tags:
          - Benchmarks
        """
        benchmark_result = self._get(benchmark_result_id)
        benchmark_result.delete()
        return self.response_204_no_content()


class BenchmarkListAPI(ApiEndpoint, BenchmarkValidationMixin):
    serializer = BenchmarkResultSerializer()
    schema = BenchmarkResultFacadeSchema()

    @maybe_login_required
    def get(self) -> f.Response:
        """
        ---
        description: |
            Return benchmark results.

            Note that this endpoint does not provide on-the-fly change detection
            analysis (lookback z-score method) since the "baseline" is ill-defined.

            This endpoint implements pagination; see the `cursor` and `page_size` query
            parameters for how it works.

            For legacy reasons, this endpoint will not return results from before
            `2023-06-03 UTC`, unless the `run_id` query parameter is used to filter
            benchmark results.
        responses:
            "200": "BenchmarkList"
            "401": "401"
        parameters:
          - in: query
            name: run_id
            schema:
              type: string
            description: |
                Filter results to one specific `run_id`. Using this argument allows the
                response to return results from before `2023-06-03 UTC`.
          - in: query
            name: run_reason
            schema:
              type: string
            description: Filter results to one specific `run_reason`.
          - in: query
            name: cursor
            schema:
              type: string
              nullable: true
            description: |
                A cursor for pagination through matching results in reverse DB insertion
                order.

                To get the first page of results, leave out this query parameter or
                submit `null`. The response's `metadata` key will contain a
                `next_page_cursor` key, which will contain the cursor to provide to this
                query parameter in order to get the next page. (If there is expected to
                be no data in the next page, the `next_page_cursor` will be `null`.)

                The first page will contain the `page_size` most recent results matching
                the given filter(s). Each subsequent page will have up to `page_size`
                results, going backwards in time in DB insertion order, until there are
                no more matching results or the benchmark result timestamps reach
                `2023-06-03 UTC` (if the `run_id` filter isn't used; see above).

                Implementation detail: currently, the next page's cursor value is equal
                to the ID of the earliest result in the current page. A page of results
                is therefore defined as the `page_size` latest results with an ID
                lexicographically less than the cursor value.
          - in: query
            name: page_size
            schema:
              type: integer
              minimum: 1
              maximum: 1000
            description: |
                The size of pages for pagination (see `cursor`). Default 100. Max 1000.
          - in: query
            name: earliest_timestamp
            schema:
              type: string
              format: date-time
            description: |
                The earliest (least recent) benchmark result timestamp to return. (Note
                that this parameter does not affect the behavior of returning only
                results after `2023-06-03 UTC` without a `run_id` provided.)
          - in: query
            name: latest_timestamp
            schema:
              type: string
              format: date-time
            description: The latest (most recent) benchmark result timestamp to return.
        tags:
          - Benchmarks
        """
        filters = []

        if run_id_arg := f.request.args.get("run_id"):
            # It's assumed that the number of benchmark results corresponding to one
            # run_id won't increase unbounded over time (since runs end at some point).
            # So we don't have to filter out "old" results.
            filters.append(BenchmarkResult.run_id == run_id_arg)
        else:
            # All Conbench instances used a non-UUID7 primary key for benchmark results
            # before this date. We need to filter those out or they will be mixed in to
            # the results here, which will mess up the ordering.
            filters.append(BenchmarkResult.timestamp >= "2023-06-03")

        if earliest_timestamp_arg := f.request.args.get("earliest_timestamp"):
            filters.append(BenchmarkResult.timestamp >= earliest_timestamp_arg)

        if latest_timestamp_arg := f.request.args.get("latest_timestamp"):
            filters.append(BenchmarkResult.timestamp <= latest_timestamp_arg)

        if run_reason_arg := f.request.args.get("run_reason"):
            filters.append(BenchmarkResult.run_reason == run_reason_arg)

        cursor_arg = f.request.args.get("cursor")
        if cursor_arg and cursor_arg != "null":
            filters.append(BenchmarkResult.id < cursor_arg)

        page_size = f.request.args.get("page_size", 100)
        try:
            page_size = int(page_size)
            assert 1 <= page_size <= 1000
        except Exception:
            self.abort_400_bad_request(
                "page_size must be a positive integer no greater than 1000"
            )

        query = (
            select(BenchmarkResult)
            .filter(*filters)
            .order_by(BenchmarkResult.id.desc())
            .limit(page_size)
        )
        benchmark_results = current_session.scalars(query).all()

        if len(benchmark_results) == page_size:
            next_page_cursor = benchmark_results[-1].id
            # There's an edge case here where the last page happens to have exactly
            # page_size results. So the client will grab one more (empty) page. The
            # alternative would be to query the DB here, every single time, to *make
            # sure* the next page will contain results... but that feels very expensive.
        else:
            # If there were fewer than page_size results, the next page should be empty
            next_page_cursor = None

        # See https://github.com/conbench/conbench/issues/999 -- for rather
        # typical queries, using orjson instead of stdlib can significantly
        # cut JSON serialization time.
        jsonbytes: bytes = orjson.dumps(
            {
                "data": [r.to_dict_for_json_api() for r in benchmark_results],
                "metadata": {"next_page_cursor": next_page_cursor},
            },
            option=orjson.OPT_INDENT_2,
        )

        return json_response_for_byte_sequence(jsonbytes, 200)

    @flask_login.login_required
    def post(self) -> f.Response:
        """
        ---
        description:
            Submit a BenchmarkResult within a specific Run.

            If the Run (as defined by its Run ID) is not known yet in the
            database it gets implicitly created, using details provided in this
            request. If the Run ID matches an existing run, then the rest of
            the fields describing the Run (such as name, hardware info, ...}
            are silently ignored.
        responses:
            "201": "BenchmarkResultCreated"
            "400": "400"
            "401": "401"
        requestBody:
            content:
                application/json:
                    schema: BenchmarkResultCreate
        tags:
          - Benchmarks
        """
        # Here it should be easy to make `data` have a precise type (that mypy
        # can use) based on the schema that we validate against.
        data = self.validate_benchmark(self.schema.create)

        try:
            benchmark_result = BenchmarkResult.create(data)
        except BenchmarkResultValidationError as exc:
            return resp400(str(exc))

        # Rely on the idea that the lookup
        # `benchmark_result.commit_repo_url` always succeeds
        conbench.metrics.COUNTER_BENCHMARK_RESULTS_INGESTED.labels(
            repourl=benchmark_result.commit_repo_url
        ).inc()
        return self.response_201_created(self.serializer.one.dump(benchmark_result))


benchmark_entity_view = BenchmarkEntityAPI.as_view("benchmark")
benchmark_list_view = BenchmarkListAPI.as_view("benchmarks")

# Phase these out, at some point.
# https://github.com/conbench/conbench/issues/972
rule(
    "/benchmarks/",
    view_func=benchmark_list_view,
    methods=["GET", "POST"],
)
rule(
    "/benchmarks/<benchmark_result_id>/",
    view_func=benchmark_entity_view,
    methods=["GET", "DELETE", "PUT"],
)

# Towards the more explicit route path naming":
# https://github.com/conbench/conbench/issues/972
rule(
    "/benchmark-results/",
    view_func=benchmark_list_view,
    methods=["GET", "POST"],
)
rule(
    "/benchmark-results/<benchmark_result_id>/",
    view_func=benchmark_entity_view,
    methods=["GET", "DELETE", "PUT"],
)
spec.components.schema(
    "BenchmarkResultCreate", schema=BenchmarkResultFacadeSchema.create
)
spec.components.schema(
    "BenchmarkResultUpdate", schema=BenchmarkResultFacadeSchema.update
)
