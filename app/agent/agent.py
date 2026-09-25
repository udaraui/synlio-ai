import os
from typing import Annotated, TypedDict, Sequence
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import BaseModel
from sqlalchemy import create_engine, text

# Load environment variables from .env file
load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
pg_db_url = os.getenv("PG_DB_URL")

# --- Step 1: State and Tools ---
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]

def _execute_sql(sql_query: str) -> str:
    print(f"\n[AGENT EXECUTING SQL] -> {sql_query}\n")
    if not pg_db_url:
        return "Error: PG_DB_URL not configured."
    if any(keyword in sql_query.upper() for keyword in ["INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER"]):
        return "Error: Only SELECT queries are allowed."
    try:
        engine = create_engine(pg_db_url)
        with engine.connect() as conn:
            result = conn.execute(text(sql_query))
            rows = result.fetchall()
            if not rows:
                return "Query returned zero rows."
            columns = result.keys()
            output = [", ".join(columns)]
            for row in rows:
                output.append(", ".join(str(val) for val in row))
            return "\n".join(output)
    except Exception as e:
        return f"Error executing query: {e}"

@tool
def query_project_data(sql_query: str) -> str:
    """Execute a read-only SQL query against the Postgres database specifically for Project and Task Analytics (e.g. vw_project_dashboard, vw_project_task_detail)."""
    print(f"\n🚀 [ORCHESTRATOR ROUTING] -> Selected PROJECT DATA Tool")
    return _execute_sql(sql_query)

@tool
def query_ticket_data(sql_query: str) -> str:
    """Execute a read-only SQL query against the Postgres database specifically for Ticket and Support Analytics (e.g. vw_ticket_dashboard, vw_ticket_recent_list)."""
    print(f"\n🎫 [ORCHESTRATOR ROUTING] -> Selected TICKET DATA Tool")
    return _execute_sql(sql_query)

@tool
def query_resource_data(sql_query: str) -> str:
    """Execute a read-only SQL query against the Postgres database specifically for Resource and Team Analytics (e.g. vw_resource_dashboard, vw_resource_skill_gap)."""
    print(f"\n👥 [ORCHESTRATOR ROUTING] -> Selected RESOURCE DATA Tool")
    return _execute_sql(sql_query)

def build_agent_prompt() -> str:
    system_prompt_parts = []
    context_files = [
        "app/system_prompt.md", 
        "app/guard_rails.md", 
        "app/skills.md", 
        "app/semantic_layer.yml"
    ]
    
    for file_name in context_files:
        if os.path.exists(file_name):
            with open(file_name, 'r', encoding='utf-8') as f:
                system_prompt_parts.append(f.read())
        else:
            print(f"Warning: Prompt file {file_name} not found.")

    return "\n\n".join(system_prompt_parts) or "You are a helpful project management AI."


class AgentResult(BaseModel):
    data: str

class InsightAgent:
    def __init__(self, key: str):
        self.llm = ChatOpenAI(
            model="gpt-5.6-luna",
            api_key=key,
            temperature=0,
            model_kwargs={"reasoning_effort": "none"}
        )
        
        self.system_prompt = build_agent_prompt()
        self.tools = [query_project_data, query_ticket_data, query_resource_data]
        
        # Bind tools to the LLM so it knows it can call them
        self.llm_with_tools = self.llm.bind_tools(self.tools)
        
        # --- Step 2: Nodes ---
        def orchestrator_node(state: AgentState):
            messages = state["messages"]
            # Prepend system prompt if it's not already in the state
            if not messages or not isinstance(messages[0], SystemMessage):
                messages = [SystemMessage(content=self.system_prompt)] + list(messages)
                
            # Invoke the LLM with the injected system prompt and conversation history
            response = self.llm_with_tools.invoke(messages)
            return {"messages": [response]}

        # LangGraph's built-in ToolNode handles execution of the bound tools
        tool_node = ToolNode(self.tools)
        
        def formatter_node(state: AgentState):
            messages = state["messages"]
            last_message = messages[-1]
            
            from langchain_core.messages import HumanMessage
            formatting_prompt = SystemMessage(content=(
                "You are an expert formatting assistant for PMO Executives. "
                "Take the raw analysis provided by the user and format it beautifully in Markdown.\n\n"
                "STRICT FORMATTING RULES:\n"
                "1. Use Markdown headers (###) for major sections like 'Overview', 'Key Observations', or 'Blockers'.\n"
                "2. NEVER output dense paragraphs. Convert all insights, summaries, and lists of names into bulleted lists (-).\n"
                "3. Use bold text (**text**) to highlight key metrics, names, and important numbers.\n"
                "4. Keep the table exactly as is, but ensure text columns are left-aligned and number/currency columns are right-aligned.\n"
                "5. Add empty lines between sections for readability.\n"
                "6. Do NOT change any numbers or facts. Output ONLY the beautifully formatted Markdown."
            ))
            
            # Pass the raw AI answer as a HumanMessage to the formatting LLM
            response = self.llm.invoke([formatting_prompt, HumanMessage(content=last_message.content)])
            return {"messages": [response]}

        # --- Step 3: Graph Wiring ---
        builder = StateGraph(AgentState)
        
        # Add the nodes to the graph
        builder.add_node("orchestrator", orchestrator_node)
        builder.add_node("db_query_tool", tool_node)
        builder.add_node("formatter", formatter_node)
        
        # Custom routing logic
        def route_agent_output(state: AgentState):
            messages = state["messages"]
            last_message = messages[-1]
            if getattr(last_message, "tool_calls", None):
                return "db_query_tool"
            return "formatter"
        
        # Define the flow (Edges)
        builder.add_edge(START, "orchestrator")
        
        # Conditional routing: if tools needed, go to tools. Otherwise, go format the final answer.
        builder.add_conditional_edges("orchestrator", route_agent_output)
        
        # After a tool finishes executing, loop back to the orchestrator to synthesize the data
        builder.add_edge("db_query_tool", "orchestrator")
        
        # After formatting, the graph ends.
        builder.add_edge("formatter", END)
        
        # Compile the graph into an executable application
        self.agent_executor = builder.compile()
        
    async def arun(self, query: str) -> AgentResult:
        # Run through the AgentExecutor
        response = await self.agent_executor.ainvoke({"messages": [("user", query)]})
        # Extract the final AI response content
        output = response["messages"][-1].content
        
        # Google GenAI sometimes returns a list of blocks instead of a string
        if isinstance(output, list):
            text_parts = [block.get("text", "") for block in output if block.get("type") == "text"]
            output = "".join(text_parts)
            
        return AgentResult(data=str(output))

    async def astream_run(self, query: str):
        # Stream chunks from the agent
        async for chunk, metadata in self.agent_executor.astream({"messages": [("user", query)]}, stream_mode="messages"):
            if metadata.get("langgraph_node") == "orchestrator":
                content = chunk.content
                if isinstance(content, list):
                    parsed = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            parsed.append(c.get("text", ""))
                        elif isinstance(c, str):
                            parsed.append(c)
                    content = "".join(parsed)
                if content:
                    yield content

# Initialize the agent if the API key is present
insight_agent = None
if api_key:
    try:
        insight_agent = InsightAgent(api_key)
    except Exception as e:
        print(f"Error initializing InsightAgent: {e}")
