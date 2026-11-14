"""The search benchmark: whether ``search_operations`` ranks the operation a request asks for near the top.

It calls no model. Queries go through the same MCP tool a model calls, on a gateway built in-process,
and the rankings are scored against graded relevance judgements, as in BEIR and TREC.
"""
