"""GraphQL schema for the same v1 application operations as REST.

Typed root operations/arguments provide discoverability. The JSON scalar
preserves provider-specific heterogeneous metadata without inventing fields.
A fixed query budget is enforced *before* GraphQL validation or execution.
"""
from __future__ import annotations

from collections.abc import Mapping
import inspect
from typing import Any

from graphql import (
    GraphQLError,
    GraphQLScalarType,
    build_schema,
    execute,
    parse,
    specified_rules,
    validate,
)
from graphql.language import ast as gql_ast
from graphql.validation.rules.custom.no_schema_introspection import (
    NoSchemaIntrospectionCustomRule,
)

from api.jobs import LONG_OPERATIONS, JobManager
from api.operations import ApiOperationError, OPERATIONS, execute_operation


SDL = """
scalar JSON

type SearchItem {
  index: Int
  title: String
  year: Int
  externalIds: JSON
  scores: JSON
}

type SearchResponse {
  query: String
  count: Int
  complete: Boolean
  errors: JSON
  items: [SearchItem!]
}

type AnimeCard {
  query: String!
  section: String!
  data: JSON
}

type Query {
  apiVersion: String!
  ping: JSON
  status: JSON
  sources: JSON
  sourceHealth: JSON
  metrics: JSON
  search(query: String!, source: String, limit: Int = 20): SearchResponse!
  lookup(identity: String!): SearchResponse!
  anime(query: String!, section: String = "main", source: String,
        episode: Int = 1, page: Int = 1, limit: Int = 20): AnimeCard!
  recognizeImage(contentBase64: String!, contentType: String = "image/jpeg",
                 provider: String, limit: Int = 10): JSON
  imageProviders: JSON
  users: JSON
  user(userId: Int!): JSON
  language(userId: Int!): JSON
  mappingStatus: JSON
  mappingOverrides: JSON
  mappingHistory(limit: Int = 20): JSON
  configList: JSON
  configCheck: JSON
  configGet(key: String!): JSON
  configExplain(key: String!): JSON
  logs(file: String = "launcher", lines: Int = 60): JSON
  jobs: JSON
  job(id: String!): JSON
  operation(name: String!, input: JSON): JSON
  sourceCatalog: JSON
  rawSearch(query: String!, source: String = "all", page: Int = 1, limit: Int = 20): JSON
  rawId(source: String!, id: String!): JSON
  rawMetadata(source: String = "all", id: String, ids: JSON, resolve: Boolean = false): JSON
  rawWire(source: String = "all", id: String, ids: JSON, resolve: Boolean = false): JSON
  rawVoices(source: String = "all", id: String, ids: JSON, resolve: Boolean = false, detail: String = "manifest"): JSON
  rawSeiyuu(source: String = "all", id: String, ids: JSON, resolve: Boolean = false, detail: String = "manifest"): JSON
  rawStaff(source: String = "all", id: String, ids: JSON, resolve: Boolean = false, detail: String = "manifest"): JSON
  rawCharacters(source: String = "all", id: String, ids: JSON, resolve: Boolean = false): JSON
  rawEpisodes(source: String = "all", id: String, ids: JSON, resolve: Boolean = false, page: Int = 1): JSON
  rawEpisode(source: String = "all", id: String, ids: JSON, resolve: Boolean = false, episode: Int = 1): JSON
  rawTimestamps(source: String = "all", id: String, ids: JSON, resolve: Boolean = false, episode: Int = 1): JSON
  rawThemes(source: String = "all", id: String, ids: JSON, resolve: Boolean = false, episode: Int = 1): JSON
  rawTrailers(source: String = "all", id: String, ids: JSON, resolve: Boolean = false): JSON
  rawScreenshots(source: String = "all", id: String, ids: JSON, resolve: Boolean = false): JSON
  rawStatistics(source: String = "all", id: String, ids: JSON, resolve: Boolean = false): JSON
  rawDescription(source: String = "all", id: String, ids: JSON, resolve: Boolean = false): JSON
  rawRelations(source: String = "all", id: String, ids: JSON, resolve: Boolean = false): JSON
  rawRecommendations(source: String = "all", id: String, ids: JSON, resolve: Boolean = false): JSON
  rawImage(source: String = "all", contentBase64: String!, contentType: String = "image/jpeg"): JSON
  rawBundle(source: String = "all", id: String, ids: JSON, resolve: Boolean = false, capabilities: [String!]!): JSON
  rawAll(source: String = "all", id: String, ids: JSON, resolve: Boolean = false): JSON
}

type Mutation {
  setLanguage(userId: Int!, locale: String!, confirm: Boolean!): JSON
  banUser(userId: Int!, seconds: Int, reason: String, confirm: Boolean!): JSON
  unbanUser(userId: Int!, confirm: Boolean!): JSON
  updateMappings(force: Boolean = false, confirm: Boolean!): JSON
  setMapping(action: String!, source: String!, sourceId: String!,
             target: String!, targetId: String!, reason: String!,
             confirm: Boolean!): JSON
  deleteMapping(source: String!, sourceId: String!, target: String!,
                targetId: String!, reason: String!, confirm: Boolean!): JSON
  setConfig(key: String!, value: String!, confirm: Boolean!): JSON
  resetConfig(key: String!, confirm: Boolean!): JSON
  buildImdbIndex(sourceFile: String, url: String, mappingsDb: String,
                 output: String, confirm: Boolean!): JSON
  buildBahaIndex(mappingsDb: String, output: String, dataUrl: String,
                 bahamutApiUrl: String, concurrency: Int = 12,
                 confirm: Boolean!): JSON
  operation(name: String!, input: JSON, confirm: Boolean!): JSON
}
"""


def _serialize_json(value: Any) -> Any:
    from scripts.operator_console import _serializable
    return _serializable(value)


def _parse_json_literal(node: gql_ast.ValueNode, variables: Any = None) -> Any:
    if isinstance(node, gql_ast.NullValueNode):
        return None
    if isinstance(node, gql_ast.StringValueNode | gql_ast.EnumValueNode):
        return node.value
    if isinstance(node, gql_ast.BooleanValueNode):
        return node.value
    if isinstance(node, gql_ast.IntValueNode):
        return int(node.value)
    if isinstance(node, gql_ast.FloatValueNode):
        return float(node.value)
    if isinstance(node, gql_ast.ListValueNode):
        return [_parse_json_literal(child, variables) for child in node.values]
    if isinstance(node, gql_ast.ObjectValueNode):
        return {
            field.name.value: _parse_json_literal(field.value, variables)
            for field in node.fields
        }
    if isinstance(node, gql_ast.VariableNode):
        return (variables or {}).get(node.name.value)
    return None


SCHEMA = build_schema(SDL)
json_scalar = SCHEMA.type_map["JSON"]
assert isinstance(json_scalar, GraphQLScalarType)
json_scalar.serialize = _serialize_json
json_scalar.parse_value = lambda value: value
json_scalar.parse_literal = _parse_json_literal


def _params(**kwargs: Any) -> dict[str, Any]:
    """Convert GraphQL camelCase to the shared API operation parameter names."""
    return {
        {
            "userId": "user_id",
            "sourceId": "source_id",
            "targetId": "target_id",
            "sourceFile": "source_file",
            "mappingsDb": "mappings_db",
            "contentBase64": "content_base64",
            "contentType": "content_type",
            "dataUrl": "data_url",
            "bahamutApiUrl": "bahamut_api_url",
        }.get(key, key): value
        for key, value in kwargs.items()
        if value is not None
    }


def _bind(
    gql_type: str, field_name: str, operation: str,
    *, result_type: str = "json",
) -> None:
    async def resolver(_root: Any, info: Any, **kwargs: Any) -> Any:
        data = _params(**kwargs)
        try:
            if operation in LONG_OPERATIONS:
                result = info.context["jobs"].submit(
                    info.context["app_context"], operation, data,
                    info.context["role"],
                )
            else:
                result = await execute_operation(
                    info.context["app_context"],
                    operation,
                    data,
                    role=info.context["role"],
                )
        except ApiOperationError as exc:
            raise GraphQLError(
                exc.message, extensions={"status": exc.code}
            ) from exc
        if result_type == "anime":
            return {
                "query": data["query"],
                "section": data.get("section", "main"),
                "data": result,
            }
        if result_type == "search":
            # The console uses snake_case; GraphQL exposes common camelCase.
            items = [
                {**item, "externalIds": item.get("external_ids")}
                for item in result.get("items", [])
            ]
            return {**result, "items": items}
        return result

    field = SCHEMA.type_map[gql_type].fields[field_name]
    field.resolve = resolver


def _bind_generic(gql_type: str, *, mutating: bool) -> None:
    async def resolver(_root: Any, info: Any, **kwargs: Any) -> Any:
        action = kwargs["name"]
        spec = OPERATIONS.get(action)
        if spec is None:
            raise GraphQLError("Unknown API operation")
        if spec.writes != mutating:
            raise GraphQLError(
                "Use a GraphQL mutation for write operations"
                if spec.writes else "Use a GraphQL query for read operations"
            )
        payload = kwargs.get("input") or {}
        if not isinstance(payload, Mapping):
            raise GraphQLError("Operation input must be a JSON object")
        payload = dict(payload)
        if mutating:
            payload["confirm"] = kwargs.get("confirm")
        try:
            if action in LONG_OPERATIONS:
                return info.context["jobs"].submit(
                    info.context["app_context"], action, payload,
                    info.context["role"],
                )
            return await execute_operation(
                info.context["app_context"], action, payload,
                role=info.context["role"],
            )
        except ApiOperationError as exc:
            raise GraphQLError(
                exc.message, extensions={"status": exc.code}
            ) from exc

    SCHEMA.type_map[gql_type].fields["operation"].resolve = resolver


SCHEMA.type_map["Query"].fields["apiVersion"].resolve = lambda *_: "v1"

for _field, _op, _kind in (
    ("ping", "ping", "json"),
    ("status", "status", "json"),
    ("sources", "sources", "json"),
    ("sourceHealth", "source-health", "json"),
    ("metrics", "metrics", "json"),
    ("search", "search", "search"),
    ("lookup", "lookup", "search"),
    ("anime", "anime", "anime"),
    ("recognizeImage", "image", "json"),
    ("imageProviders", "image-providers", "json"),
    ("users", "users", "json"),
    ("user", "user", "json"),
    ("language", "language", "json"),
    ("mappingStatus", "mapping.status", "json"),
    ("mappingOverrides", "mapping.list", "json"),
    ("mappingHistory", "mapping.history", "json"),
    ("configList", "config.list", "json"),
    ("configCheck", "config.check", "json"),
    ("configGet", "config.get", "json"),
    ("configExplain", "config.explain", "json"),
    ("logs", "logs", "json"),
):
    _bind("Query", _field, _op, result_type=_kind)

for _field, _op in (
    ("setLanguage", "language.set"),
    ("banUser", "ban"),
    ("unbanUser", "unban"),
    ("updateMappings", "mapping.update"),
    ("setMapping", "mapping.set"),
    ("deleteMapping", "mapping.delete"),
    ("setConfig", "config.set"),
    ("resetConfig", "config.reset"),
    ("buildImdbIndex", "index.imdb"),
    ("buildBahaIndex", "index.baha"),
):
    _bind("Mutation", _field, _op)

def _jobs_resolver(_root: Any, info: Any) -> Any:
    return info.context["jobs"].list_jobs(info.context["role"])


def _job_resolver(_root: Any, info: Any, id: str) -> Any:
    return info.context["jobs"].job(id, info.context["role"])


SCHEMA.type_map["Query"].fields["jobs"].resolve = _jobs_resolver
SCHEMA.type_map["Query"].fields["job"].resolve = _job_resolver

_bind("Query", "sourceCatalog", "raw.catalog")
_bind("Query", "rawSearch", "raw.search")
_bind("Query", "rawId", "raw.id")
_bind("Query", "rawMetadata", "raw.metadata")
_bind("Query", "rawWire", "raw.wire")
_bind("Query", "rawVoices", "raw.voices")
_bind("Query", "rawSeiyuu", "raw.voices")
_bind("Query", "rawStaff", "raw.staff")
_bind("Query", "rawCharacters", "raw.characters")
_bind("Query", "rawEpisodes", "raw.episodes")
_bind("Query", "rawEpisode", "raw.episode")
_bind("Query", "rawTimestamps", "raw.timestamps")
_bind("Query", "rawThemes", "raw.themes")
_bind("Query", "rawTrailers", "raw.trailers")
_bind("Query", "rawScreenshots", "raw.screenshots")
_bind("Query", "rawStatistics", "raw.statistics")
_bind("Query", "rawDescription", "raw.description")
_bind("Query", "rawRelations", "raw.relations")
_bind("Query", "rawRecommendations", "raw.recommendations")
_bind("Query", "rawImage", "raw.image")
_bind("Query", "rawBundle", "raw.bundle")
_bind("Query", "rawAll", "raw.all")

_bind_generic("Query", mutating=False)
_bind_generic("Mutation", mutating=True)


def _budget(
    document: gql_ast.DocumentNode, *,
    max_depth: int, max_fields: int,
) -> None:
    """Bound AST nodes, fragment expansion, field count and nesting."""
    fragments = {
        item.name.value: item
        for item in document.definitions
        if isinstance(item, gql_ast.FragmentDefinitionNode)
    }
    if len(fragments) > 10 or len(document.definitions) > 14:
        raise GraphQLError("GraphQL document has too many definitions")
    fields = 0
    root_fields = 0

    def walk(selection_set: gql_ast.SelectionSetNode, depth: int,
             active: frozenset[str]) -> None:
        nonlocal fields, root_fields
        if depth > max_depth:
            raise GraphQLError("GraphQL depth limit exceeded")
        for selection in selection_set.selections:
            if isinstance(selection, gql_ast.FieldNode):
                fields += 1
                if depth == 1:
                    root_fields += 1
                    if root_fields > 8:
                        raise GraphQLError("GraphQL root operation budget exceeded")
                if fields > max_fields:
                    raise GraphQLError("GraphQL field budget exceeded")
                if selection.selection_set:
                    walk(selection.selection_set, depth + 1, active)
            elif isinstance(selection, gql_ast.InlineFragmentNode):
                walk(selection.selection_set, depth + 1, active)
            elif isinstance(selection, gql_ast.FragmentSpreadNode):
                key = selection.name.value
                fragment = fragments.get(key)
                if fragment is None or key in active:
                    raise GraphQLError("Invalid or cyclic GraphQL fragment")
                walk(fragment.selection_set, depth + 1, active | {key})

    for operation in document.definitions:
        if isinstance(operation, gql_ast.OperationDefinitionNode):
            walk(operation.selection_set, 1, frozenset())


async def execute_graphql(
    app_context: Any, *, role: str, query: str,
    variables: dict[str, Any] | None, operation_name: str | None,
    jobs: JobManager,
) -> dict[str, Any]:
    settings = app_context.settings.api
    if not query or len(query) > 16384:
        return {"errors": [{"message": "GraphQL query must contain 1-16384 characters"}]}
    try:
        document = parse(query)
        _budget(
            document, max_depth=settings.graphql_max_depth,
            max_fields=settings.graphql_max_fields,
        )
        rules = list(specified_rules)
        if not settings.graphql_introspection:
            rules.append(NoSchemaIntrospectionCustomRule)
        validation_errors = validate(SCHEMA, document, rules=rules)
        if validation_errors:
            return {
                "errors": [
                    {"message": err.message} for err in validation_errors[:10]
                ]
            }
        result = execute(
            SCHEMA, document,
            variable_values=variables,
            operation_name=operation_name,
            context_value={"app_context": app_context, "role": role, "jobs": jobs},
        )
        if inspect.isawaitable(result):
            result = await result
        payload: dict[str, Any] = {}
        if result.data is not None:
            payload["data"] = result.data
        if result.errors:
            payload["errors"] = [
                {"message": err.message,
                 **({"path": err.path} if err.path else {}),
                 **({"extensions": err.extensions} if err.extensions else {})}
                for err in result.errors[:10]
            ]
        return payload
    except GraphQLError as exc:
        return {"errors": [{"message": exc.message}]}
    except Exception:
        return {"errors": [{"message": "Invalid GraphQL request"}]}
