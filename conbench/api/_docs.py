import apispec
import apispec.ext.marshmallow
import apispec_webframeworks.flask

from ..api import _examples as ex
from ..config import Config

# `api_server_url` is used for populating the 'Servers' dropdown in the
# Swagger/OpenAPI docs website. The default value is tailored to the Flask
# development HTTP server defaults (non-TLS, binds on 127.0.0.1, port 5000).
# Can be adjusted via the `--host` and `--port` command line flags when
# invoking `flask run`.
api_server_url = "http://127.0.0.1:5000/"
if Config.INTENDED_BASE_URL is not None:
    api_server_url = Config.INTENDED_BASE_URL


spec = apispec.APISpec(
    title=Config.APPLICATION_NAME,
    version="1.0.0",
    openapi_version="3.0.2",
    plugins=[
        apispec_webframeworks.flask.FlaskPlugin(),
        apispec.ext.marshmallow.MarshmallowPlugin(),
    ],
    servers=[{"url": api_server_url}],
)


example2 = {
    "code": 400,
    "description": {"extra": ["Unknown field."]},
    "name": "Bad Request",
}


def _error(error, example, schema=None):
    content = {"example": example}
    if schema:
        content = {"schema": schema, "example": example}
    return {"description": error, "content": {"application/json": content}}


def _200_ok(example, schema=None):
    content = {"example": example}
    if schema:
        content = {"schema": schema, "example": example}
    return {"description": "OK", "content": {"application/json": content}}


def _201_created(example, schema=None):
    content = {"example": example}
    if schema:
        content = {"schema": schema, "example": example}
    return {
        "description": "Created \n\n The resulting entity URL is returned in the Location header.",
        "content": {"application/json": content},
        #         "headers": {
        #             "Location": {"description": "The new entity URL.", "type": "url"}
        #         },
    }


spec.components.response("200", {"description": "OK"})
spec.components.response("201", {"description": "Created"})
spec.components.response("202", {"description": "No Content (accepted)"})
spec.components.response("204", {"description": "No Content (success)"})
spec.components.response("302", {"description": "Found"})
spec.components.response("400", _error("Bad Request", ex.API_400, "ErrorBadRequest"))
spec.components.response("401", _error("Unauthorized", ex.API_401, "Error"))
spec.components.response("404", _error("Not Found", ex.API_404, "Error"))
spec.components.response("Ping", _200_ok(ex.API_PING, "Ping"))
spec.components.response("Index", _200_ok(ex.API_INDEX))
spec.components.response("BenchmarkEntity", _200_ok(ex.BENCHMARK_ENTITY))
spec.components.response(
    "BenchmarkList",
    _200_ok({"data": [ex.BENCHMARK_ENTITY], "metadata": {"next_page_cursor": None}}),
)
spec.components.response("BenchmarkResultCreated", _201_created(ex.BENCHMARK_ENTITY))
spec.components.response("CommitEntity", _200_ok(ex.COMMIT_ENTITY))
spec.components.response("CommitList", _200_ok([ex.COMMIT_ENTITY]))
spec.components.response("CompareEntity", _200_ok(ex.COMPARE_ENTITY))
spec.components.response("CompareList", _200_ok(ex.COMPARE_LIST))
spec.components.response("ContextEntity", _200_ok(ex.CONTEXT_ENTITY))
spec.components.response("ContextList", _200_ok([ex.CONTEXT_ENTITY]))
spec.components.response("InfoList", _200_ok([ex.INFO_ENTITY]))
spec.components.response(
    "HistoryList",
    _200_ok({"data": [ex.HISTORY_ENTITY], "metadata": {"next_page_cursor": None}}),
)
spec.components.response("InfoEntity", _200_ok(ex.INFO_ENTITY))
spec.components.response("HardwareEntity", _200_ok(ex.HARDWARE_ENTITY))
spec.components.response("HardwareList", _200_ok([ex.HARDWARE_ENTITY]))
spec.components.response(
    "RunEntityWithBaselines", _200_ok(ex.RUN_ENTITY_WITH_BASELINES)
)
spec.components.response(
    "RunEntityWithoutBaselines", _200_ok(ex.RUN_ENTITY_WITHOUT_BASELINES)
)
spec.components.response(
    "RunList", _200_ok({"data": ex.RUN_LIST, "metadata": {"next_page_cursor": None}})
)
spec.components.response("RunCreated", _201_created({}))
spec.components.response("UserEntity", _200_ok(ex.USER_ENTITY))
spec.components.response("UserList", _200_ok(ex.USER_LIST))
spec.components.response("UserCreated", _201_created(ex.USER_ENTITY))


tags = [
    {"name": "Authentication"},
    {"name": "Index", "description": "List of endpoints"},
    {"name": "Users", "description": "Manage users"},
    {"name": "Benchmarks", "description": "Record benchmarks"},
    {"name": "Commits", "description": "Benchmarked commits"},
    {"name": "Comparisons", "description": "Benchmark comparisons"},
    {"name": "Info", "description": "Extra benchmark information"},
    {"name": "Contexts", "description": "Benchmark contexts"},
    {"name": "History", "description": "Benchmark history"},
    {"name": "Hardware", "description": "Benchmark hardware"},
    {"name": "Runs", "description": "Benchmark runs"},
    {"name": "Ping", "description": "Monitor status"},
]


for tag in tags:
    spec.tag(tag)
