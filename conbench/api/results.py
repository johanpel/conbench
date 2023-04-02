import logging

import flask as f
import flask_login
import orjson
from sqlalchemy import select

from ..api import rule
from ..api._docs import spec
from ..api._endpoint import ApiEndpoint, maybe_login_required
from ..db import Session
from ..entities._entity import NotFound
from ..entities.benchmark_result import (
    BenchmarkResult,
    BenchmarkResultFacadeSchema,
    BenchmarkResultSerializer,
)
from ..entities.case import Case
from ..entities.history import set_z_scores

log = logging.getLogger(__name__)


class BenchmarkValidationMixin:
    def validate_benchmark(self, schema):
        return self.validate(schema)


class BenchmarkEntityAPI(ApiEndpoint, BenchmarkValidationMixin):
    serializer = BenchmarkResultSerializer()
    schema = BenchmarkResultFacadeSchema()

    def _get(self, benchmark_id):
        try:
            benchmark_result = BenchmarkResult.one(id=benchmark_id)
        except NotFound:
            self.abort_404_not_found()
        return benchmark_result

    @maybe_login_required
    def get(self, benchmark_id):
        """
        ---
        description: Get a benchmark result.
        responses:
            "200": "BenchmarkEntity"
            "401": "401"
            "404": "404"
        parameters:
          - name: benchmark_id
            in: path
            schema:
                type: string
        tags:
          - Benchmarks
        """
        benchmark_result = self._get(benchmark_id)
        set_z_scores([benchmark_result])
        return self.serializer.one.dump(benchmark_result)

    @flask_login.login_required
    def put(self, benchmark_id):
        """
        ---
        description: Edit a benchmark result.
        responses:
            "200": "BenchmarkEntity"
            "401": "401"
            "404": "404"
        parameters:
          - name: benchmark_id
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
        benchmark_result = self._get(benchmark_id)
        data = self.validate_benchmark(self.schema.update)
        benchmark_result.update(data)
        set_z_scores([benchmark_result])
        return self.serializer.one.dump(benchmark_result)

    @flask_login.login_required
    def delete(self, benchmark_id):
        """
        ---
        description: Delete a benchmark result.
        responses:
            "204": "204"
            "401": "401"
            "404": "404"
        parameters:
          - name: benchmark_id
            in: path
            schema:
                type: string
        tags:
          - Benchmarks
        """
        benchmark_result = self._get(benchmark_id)
        benchmark_result.delete()
        return self.response_204_no_content()


class BenchmarkListAPI(ApiEndpoint, BenchmarkValidationMixin):
    serializer = BenchmarkResultSerializer()
    schema = BenchmarkResultFacadeSchema()

    @maybe_login_required
    def get(self):
        """
        ---
        description: Get a list of benchmarks.
        responses:
            "200": "BenchmarkList"
            "401": "401"
        parameters:
          - in: query
            name: name
            schema:
              type: string
          - in: query
            name: batch_id
            schema:
              type: string
          - in: query
            name: run_id
            schema:
              type: string
        tags:
          - Benchmarks
        """
        # Note(JP): "case name" is the conceptual benchmark name. Interesting,
        # so this is like asking "give me results for this benchmark".
        if name_arg := f.request.args.get("name"):
            # TODO: This needs a limit, and sorting behavior.
            # arbitrary limit for now.
            benchmark_results = BenchmarkResult.search(
                filters=[Case.name == name_arg],
                joins=[Case],
            )

        elif batch_id_arg := f.request.args.get("batch_id"):
            batch_ids = batch_id_arg.split(",")
            benchmark_results = BenchmarkResult.search(
                filters=[BenchmarkResult.batch_id.in_(batch_ids)]
            )
            # When asking for a specific batch_id then perform the lookback
            # z-score method on the fly (this is costly!)
            set_z_scores(benchmark_results)

        elif run_id_arg := f.request.args.get("run_id"):
            # Note(JP): https://github.com/conbench/conbench/issues/978 Given
            # Conbench's data model we want to limit the number of run_ids that
            # can be provided here. Maybe to 1, maybe to 5. Querying results
            # for 100 runs (seen in practice) is for now difficult to support.
            run_ids = run_id_arg.split(",")
            if len(run_ids) > 5:
                log.warning(
                    "suspicious query /api/benchmarks for many run_ids -- see conbench/conbench/issues/978"
                )

            benchmark_results = Session.scalars(
                select(BenchmarkResult).where(BenchmarkResult.run_id.in_(run_ids))
            ).all()

        else:
            benchmark_results = BenchmarkResult.all(
                order_by=BenchmarkResult.timestamp.desc(), limit=500
            )

        # See https://github.com/conbench/conbench/issues/999 -- for rather
        # typical queries, using orjson instead of stdlib can significantly
        # cut JSON serialization time.

        jsonbytes: bytes = orjson.dumps(
            [r.to_dict_for_json_api() for r in benchmark_results],
            option=orjson.OPT_INDENT_2,
        )

        return make_json_response(jsonbytes, 200)

    @flask_login.login_required
    def post(self) -> None:
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
        benchmark_result = BenchmarkResult.create(data)
        set_z_scores([benchmark_result])
        return self.response_201_created(self.serializer.one.dump(benchmark_result))


def make_json_response(data: bytes, status_code: int) -> f.Response:
    # Note(JP): it's documented that a byte sequence can be passed in:
    # https://flask.palletsprojects.com/en/2.2.x/api/#flask.Flask.make_response
    return f.make_response((data, status_code, {"content-type": "application/json"}))


benchmark_entity_view = BenchmarkEntityAPI.as_view("benchmark")
benchmark_list_view = BenchmarkListAPI.as_view("benchmarks")

rule(
    "/benchmarks/",
    view_func=benchmark_list_view,
    methods=["GET", "POST"],
)
rule(
    "/benchmarks/<benchmark_id>/",
    view_func=benchmark_entity_view,
    methods=["GET", "DELETE", "PUT"],
)
spec.components.schema(
    "BenchmarkResultCreate", schema=BenchmarkResultFacadeSchema.create
)
spec.components.schema(
    "BenchmarkResultUpdate", schema=BenchmarkResultFacadeSchema.update
)