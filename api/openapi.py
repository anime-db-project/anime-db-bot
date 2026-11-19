"""OpenAPI 3.1 contract for public v1 REST endpoints.

The operation catalog is shared with GraphQL and the operator console. Schema
is immutable for a release; future incompatible changes use /api/v2.
"""
from __future__ import annotations

from api.operations import OPERATIONS


def _response(description: str = "Successful JSON response") -> dict:
    return {
        "200": {
            "description": description,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/Envelope"}
                }
            },
        },
        "400": {"description": "Validation failed"},
        "401": {"description": "Authentication required"},
        "403": {"description": "Insufficient permissions"},
        "429": {"description": "Request quota exceeded"},
        "503": {"description": "Service busy"},
    }


def _operation(
    summary: str, scope: str = "read", *, body: bool = False,
    params: list[dict] | None = None,
) -> dict:
    value: dict = {
        "summary": summary,
        "security": [
            {"ApiKey": []}, {"BearerKey": []}, {"BasicAuth": []}
        ],
        "responses": _response(),
        "x-required-role": scope,
    }
    if params:
        value["parameters"] = params
    if body:
        value["requestBody"] = {
            "required": True,
            "content": {"application/json": {
                "schema": {"type": "object", "additionalProperties": True}
            }},
        }
    return value


def _query(name: str, *, required: bool = False) -> dict:
    return {
        "name": name, "in": "query", "required": required,
        "schema": {"type": "string"},
    }


def _path(name: str) -> dict:
    return {
        "name": name, "in": "path", "required": True,
        "schema": {"type": "string"},
    }


def openapi_document(public_url: str = "") -> dict:
    prefix = "/api/v1"
    paths: dict[str, dict] = {
        "/api/versions": {
            "get": {"summary": "List supported API versions",
                    "security": [], "responses": _response()}
        },
        f"{prefix}/health": {
            "get": {"summary": "Minimal public liveness probe",
                    "security": [], "responses": _response()}
        },
        f"{prefix}/": {"get": _operation("List API capabilities")},
        f"{prefix}/openapi.json": {
            "get": _operation("Download versioned OpenAPI schema")
        },
        f"{prefix}/operations": {
            "get": _operation("List registered read/admin operations")
        },
        f"{prefix}/actions/{{operation}}": {
            "post": _operation(
                "Execute one registered v1 operation", body=True,
                params=[_path("operation")],
            )
        },
        f"{prefix}/search": {
            "get": _operation("Federated anime title search", params=[
                _query("query", required=True), _query("source"),
                _query("limit"),
            ])
        },
        f"{prefix}/lookup": {
            "get": _operation("Find by confirmed source ID", params=[
                _query("identity", required=True)
            ])
        },
        f"{prefix}/anime": {
            "get": _operation("Anime metadata and lazy section", params=[
                _query("query", required=True), _query("section"),
                _query("episode"), _query("page"), _query("limit"),
                _query("source"),
            ])
        },
        f"{prefix}/anime/{{source}}/{{id}}": {
            "get": _operation("Anime card by source-scoped ID",
                              params=[_path("source"), _path("id"),
                                      _query("section"), _query("episode"),
                                      _query("page"), _query("limit")]),
        },
        f"{prefix}/anime/{{source}}/{{id}}/{{section}}": {
            "get": _operation("Lazy anime section by confirmed ID",
                              params=[_path("source"), _path("id"),
                                      _path("section"), _query("episode"),
                                      _query("page"), _query("limit")]),
        },
        f"{prefix}/images/recognize": {
            "post": _operation("Anime recognition from base64 image", body=True)
        },
    }

    read_routes = {
        "ping": "ping", "status": "status", "sources": "sources",
        "source-health": "source-health", "metrics": "metrics",
        "image-providers": "image-providers",
    }
    for path, action in read_routes.items():
        paths[f"{prefix}/{path}"] = {
            "get": _operation(OPERATIONS[action].description)
        }
    paths.update({
        f"{prefix}/users": {"get": _operation("List users", "admin")},
        f"{prefix}/users/{{user_id}}": {
            "get": _operation("Get user", "admin", params=[_path("user_id")])
        },
        f"{prefix}/users/{{user_id}}/language": {
            "get": _operation("Read language", "admin",
                              params=[_path("user_id")]),
            "put": _operation("Set language", "admin", body=True,
                              params=[_path("user_id")]),
        },
        f"{prefix}/users/{{user_id}}/ban": {
            "post": _operation("Ban user", "admin", body=True,
                               params=[_path("user_id")]),
            "delete": _operation("Unban user", "admin", body=True,
                                 params=[_path("user_id")]),
        },
        f"{prefix}/mappings/status": {
            "get": _operation("Mapping catalog status")
        },
        f"{prefix}/mappings/overrides": {
            "get": _operation("Manual overrides", "admin"),
            "post": _operation("Set mapping override", "admin", body=True),
            "delete": _operation("Delete mapping override", "admin", body=True),
        },
        f"{prefix}/mappings/history": {
            "get": _operation("Mapping audit history", "admin",
                              params=[_query("limit")])
        },
        f"{prefix}/mappings/update": {
            "post": _operation("Refresh mapping catalog", "admin", body=True)
        },
        f"{prefix}/config": {
            "get": _operation("List documented keys", "admin")
        },
        f"{prefix}/config/check": {
            "get": _operation("Validate configuration", "admin")
        },
        f"{prefix}/config/{{key}}": {
            "get": _operation("Inspect masked config value", "admin",
                              params=[_path("key")]),
            "put": _operation("Persist validated config value", "admin",
                              body=True, params=[_path("key")]),
            "delete": _operation("Remove local config override", "admin",
                                 body=True, params=[_path("key")]),
        },
        f"{prefix}/logs": {
            "get": _operation("Bounded log tail", "admin",
                              params=[_query("file"), _query("lines")])
        },
        f"{prefix}/indexes/{{kind}}": {
            "post": _operation("Build a local data index", "admin", body=True,
                               params=[_path("kind")])
        },
        "/graphql": {
            "post": _operation("GraphQL v1 typed queries and mutations",
                               body=True)
        },
    })

    doc = {
        "openapi": "3.1.0",
        "info": {
            "title": "anime-db-bot API",
            "version": "1.0.0",
            "description": (
                "Versioned application API over the same anime/identity "
                "services as Telegram and the operator console. "
                "Default language: English. All state-changing operations "
                "require administrator scope and confirm=true."
            ),
        },
        "servers": [{"url": public_url.rstrip("/")}] if public_url else [
            {"url": "http://127.0.0.1:8181"}
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "ApiKey": {"type": "apiKey", "in": "header",
                           "name": "X-API-Key"},
                "BearerKey": {"type": "http", "scheme": "bearer"},
                "BasicAuth": {"type": "http", "scheme": "basic"},
            },
            "schemas": {
                "Envelope": {
                    "type": "object",
                    "required": ["data", "meta"],
                    "properties": {
                        "data": {},
                        "meta": {
                            "type": "object",
                            "properties": {
                                "version": {"const": "v1"},
                                "request_id": {"type": "string"},
                            },
                        },
                    },
                },
                "Error": {
                    "type": "object",
                    "properties": {
                        "error": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "integer"},
                                "message": {"type": "string"},
                                "request_id": {"type": "string"},
                            },
                        }
                    },
                },
            },
        },
        "tags": [
            {"name": "Anime", "description": "Search and lazy anime metadata"},
            {"name": "Operations", "description": "Shared console API commands"},
            {"name": "Administration", "description": "Admin-only mutations"},
        ],
    }
    return doc
