from __future__ import annotations

from fastapi.openapi.utils import get_openapi

from app import main
from app.openapi_docs import OPERATION_DOCS


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


def _operations(schema: dict) -> dict[tuple[str, str], dict]:
    return {
        (method.lower(), path): operation
        for path, path_item in schema["paths"].items()
        for method, operation in path_item.items()
        if method.lower() in HTTP_METHODS
    }


def _app(monkeypatch):
    # Schema generation does not need application logging or its filesystem.
    monkeypatch.setattr(main, "_setup_logging", lambda: None)
    return main.create_app()


def test_every_http_operation_has_agent_oriented_documentation(monkeypatch) -> None:
    schema = _app(monkeypatch).openapi()
    operations = _operations(schema)

    assert set(operations) == set(OPERATION_DOCS)
    assert all(operation.get("summary", "").strip() for operation in operations.values())
    assert all(operation.get("description", "").strip() for operation in operations.values())


def test_documentation_does_not_change_paths_methods_or_operation_ids(monkeypatch) -> None:
    app = _app(monkeypatch)
    documented = _operations(app.openapi())
    generated_without_catalogue = _operations(
        get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            description=app.description,
            routes=app.routes,
        )
    )

    assert set(documented) == set(generated_without_catalogue)
    assert {
        key: operation["operationId"] for key, operation in documented.items()
    } == {
        key: operation["operationId"]
        for key, operation in generated_without_catalogue.items()
    }


def test_representative_descriptions_explain_agent_relevant_semantics(monkeypatch) -> None:
    operations = _operations(_app(monkeypatch).openapi())

    assert "does not inspect runtime containers or change lab state" in operations[
        ("get", "/api/labs/")
    ]["description"]
    assert "admin-only" in operations[
        ("post", "/api/image-sync/reconcile")
    ]["description"]
    assert "job identifier" in operations[
        ("post", "/api/admin/image-build/jobs")
    ]["description"]
    assert "cannot be undone" in operations[
        ("delete", "/api/labs/{lab_id}")
    ]["description"]
    assert "not a lab management API" in operations[
        ("get", "/webui/{token}/{path}")
    ]["description"]
