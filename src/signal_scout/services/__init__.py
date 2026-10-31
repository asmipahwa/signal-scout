"""Read and write operations over Postgres, Elasticsearch and S3, shared by the REST API and the MCP server.

Both interfaces call these functions so a query, a filter or a validation rule exists exactly once.
The functions take an `AsyncSession` and/or an `AsyncElasticsearch` client and return the Pydantic
models in `services.schemas`; they know nothing about HTTP or MCP.
"""
