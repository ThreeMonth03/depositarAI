"""Shared lexical/vector retrieval with the pinned upstream RRF component."""

from haystack import Document, Pipeline, component
from haystack.components.joiners import DocumentJoiner


@component
class IndexRetriever:
    def __init__(self, client, branch):
        self.client, self.branch = client, branch
        self.ids = []

    @component.output_types(documents=list[Document])
    def run(self, query: str, conditions: dict, vector: list[float]):
        hits = self.client.retrieve(query, conditions, self.branch, vector)
        self.ids = [identifier for identifier, _ in hits]
        return {
            "documents": [
                Document(id=identifier, score=score) for identifier, score in hits
            ]
        }


def retrieve(client, query, conditions, vector, top_k):
    pipeline = Pipeline()
    pipeline.add_component(
        "fusion", DocumentJoiner(join_mode="reciprocal_rank_fusion", top_k=top_k)
    )
    branches = {}
    for branch in ("lexical", "vector"):
        branches[branch] = IndexRetriever(client, branch)
        pipeline.add_component(branch, branches[branch])
        pipeline.connect(branch + ".documents", "fusion.documents")
    result = pipeline.run(
        {
            branch: {"query": query, "conditions": conditions, "vector": vector}
            for branch in branches
        }
    )["fusion"]["documents"]
    return result, {branch: adapter.ids for branch, adapter in branches.items()}
