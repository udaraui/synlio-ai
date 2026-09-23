from typing import TypedDict, List, Annotated
import operator

class GraphState(TypedDict):
    messages: Annotated[List[str], operator.add]
    response: str
