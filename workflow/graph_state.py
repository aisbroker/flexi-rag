from typing import List, Optional, Annotated
from typing_extensions import TypedDict

from langgraph.graph.message import MessagesState
from langchain_core.messages import AnyMessage

class GraphState(TypedDict):
    """
    Represents the state of our graph.

    Attributes:
        question: question
        generation: LLM generation
        documents: list of documents
    """

    # Messages have the type "list". The `add_messages` function
    # in the annotation defines how this state key should be updated
    # (in this case, it appends messages to the list, rather than overwriting them)
    messages: list[AnyMessage]
    documents: Optional[List[str]]
    generation: Optional[str]
    stream_generate_on_last_node: Optional[bool] = False
