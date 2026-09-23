from langgraph.graph import StateGraph, START, END
from app.agent.state import GraphState
from app.agent.nodes import generate_response

# Initialize graph
workflow = StateGraph(GraphState)

# Add node
workflow.add_node("generate", generate_response)

# Add edges
workflow.add_edge(START, "generate")
workflow.add_edge("generate", END)

# Compile graph
app = workflow.compile()
