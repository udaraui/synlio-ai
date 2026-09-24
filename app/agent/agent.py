import os
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel
from sqlalchemy import create_engine, text

# Load environment variables from .env file
load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
pg_db_url = os.getenv("PG_DB_URL")

# Define the read-only DB query tool
@tool
def query_database(sql_query: str) -> str:
    """Execute a read-only SQL query against the Postgres database and return the results as a string. NEVER use this for modifying data."""
    print(f"\n[AGENT EXECUTING SQL] -> {sql_query}\n")
    
    if not pg_db_url:
        return "Error: PG_DB_URL not configured."
    
    # Safeguard to prevent accidental modifications
    if any(keyword in sql_query.upper() for keyword in ["INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER"]):
        return "Error: Only SELECT queries are allowed."

    try:
        engine = create_engine(pg_db_url)
        with engine.connect() as conn:
            result = conn.execute(text(sql_query))
            rows = result.fetchall()
            if not rows:
                return "Query returned zero rows."
            
            # Format the output nicely as a CSV-like string for the LLM
            columns = result.keys()
            output = [", ".join(columns)]
            for row in rows:
                output.append(", ".join(str(val) for val in row))
                
            return "\n".join(output)
    except Exception as e:
        return f"Error executing query: {e}"

class AgentResult(BaseModel):
    data: str

class InsightAgent:
    def __init__(self, key: str):
        self.llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            api_key=key
        )
        
        # Load all context files including the semantic layer
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

        system_prompt = "\n\n".join(system_prompt_parts) or "You are a helpful project management AI."

        tools = [query_database]
        
        # Create Agent using LangGraph
        self.agent_executor = create_react_agent(self.llm, tools, prompt=system_prompt)
        
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
            if metadata.get("langgraph_node") == "agent":
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


# Trigger reload
