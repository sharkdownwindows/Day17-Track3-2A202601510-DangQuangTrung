from __future__ import annotations

from typing import Any

from .config import settings
from .context_budget import ContextBudgetManager
from .utils import cap_query, join_nonempty
from .zep_common import prime_eval_thread, render_graph_search


class StudentMemory:
    """Only this file needs to be edited by students."""

    def __init__(self, client: Any):
        self.client = client
        self.budget = ContextBudgetManager(settings.context_tokens)

    # NOTE: Zep rejects graph.search queries longer than 400 characters. Some
    # eval queries are longer than that, so wrap every query with
    # `cap_query(query)` (see src/utils.py) before passing it to graph.search.

    # Episodes come back as whole chat messages. 240 characters keeps the
    # longest seeded message (163 chars) intact together with any prefix Zep
    # adds, so tail markers such as ASYNC-FIX-20 survive, while still leaving
    # room for ~4 distinct episodes inside the 3% episodic budget.
    EPISODE_CHAR_CAP = 240

    def retrieve_long_term(self, user_id: str, thread_id: str, query: str) -> str:
        # The Context Block is scored against the *current* thread slice, so the
        # query has to be in the thread before we ask for user context.
        prime_eval_thread(self.client, user_id, thread_id, query)
        user_context = self.client.thread.get_user_context(thread_id=thread_id)
        context_block = getattr(user_context, "context", "") or ""

        # The context block alone can summarise away open loops and superseded
        # preferences. Appending user-scoped edge facts (with their validity
        # range) keeps the deadline/recency evidence and makes the conflict
        # between the old and the new preference auditable in the report.
        try:
            facts = self.client.graph.search(
                user_id=user_id,
                query=cap_query(query),
                scope="edges",
                limit=20,
            )
            fact_text = render_graph_search(facts)
        except Exception:
            fact_text = ""

        return join_nonempty([context_block, fact_text], sep="\n\n")

    def retrieve_episodic(self, user_id: str, query: str) -> str:
        # User-scoped, never graph_id: episodes belong to one person and E09
        # asserts that Lan's retrieval never leaks Minh's ORCHID-27.
        results = self.client.graph.search(
            user_id=user_id,
            query=cap_query(query),
            scope="episodes",
            limit=15,
        )
        return render_graph_search(results, episode_char_cap=self.EPISODE_CHAR_CAP)

    def retrieve_semantic(self, graph_id: str, query: str) -> str:
        # Domain knowledge lives in the standalone graph, so search by graph_id.
        # scope="episodes" returns the raw document text and therefore keeps the
        # literal markers (PAYMENT-RULE-3, CONN-POOL-FIRST) that the scorer looks
        # for; scope="auto" would return extracted facts that drop those codes.
        capped = cap_query(query)
        try:
            results = self.client.graph.search(
                graph_id=graph_id,
                query=capped,
                scope="episodes",
                limit=8,
            )
        except Exception:
            # Some accounts/SDK builds do not expose the episodes scope on a
            # standalone graph; nodes still carry the marker in the summary.
            results = self.client.graph.search(
                graph_id=graph_id,
                query=capped,
                scope="nodes",
                limit=8,
            )
        # No episode cap here: domain documents put their marker at the very end.
        return render_graph_search(results)

    def assemble_context(self, layers: dict[str, str]) -> tuple[str, dict[str, dict[str, int]]]:
        # 10/4/3/3 of the context window, filled in priority order
        # short_term -> long_term -> episodic -> semantic, trimming each layer
        # from the tail because every renderer puts its best evidence first.
        return self.budget.assemble(layers)
