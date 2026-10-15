"""
Script to visualize the LangGraph architecture for synlio-ai.

Usage:
    .\\venv\\Scripts\\python visualize\\visualize_graph.py
"""
import os
import sys
from pathlib import Path

# Add project root to sys.path so 'app' package can be imported regardless of working directory
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent if SCRIPT_DIR.parent.name == "scripts" else SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_openai import ChatOpenAI
from app.agent.graph import create_graph

def main():
    print("Compiling LangGraph state graph...")
    # Dummy LLM instance is sufficient to construct the graph schema
    dummy_llm = ChatOpenAI(model="gemini-2.0-flash-001", api_key=os.getenv("OPENROUTER_API_KEY", "dummy"))
    compiled_graph = create_graph(dummy_llm)
    graph = compiled_graph.get_graph()

    png_path = SCRIPT_DIR / "graph.png"
    md_path = SCRIPT_DIR / "graph_mermaid.md"

    # 1. Save PNG
    try:
        png_bytes = graph.draw_mermaid_png()
        with open(png_path, "wb") as f:
            f.write(png_bytes)
        print(f" Successfully exported graph diagram to '{png_path.name}' in {SCRIPT_DIR.name}/!")
    except Exception as e:
        print(f" Could not generate PNG (network/service error): {e}")

    # 2. Save Mermaid markdown file
    try:
        mermaid_code = graph.draw_mermaid()
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("```mermaid\n" + mermaid_code + "\n```\n")
        print(f" Successfully exported Mermaid syntax to '{md_path.name}' in {SCRIPT_DIR.name}/!")
    except Exception as e:
        print(f" Could not generate Mermaid syntax: {e}")

    print("\nMermaid Definition:")
    print("--------------------------------------------------")
    print(graph.draw_mermaid())
    print("--------------------------------------------------")

if __name__ == "__main__":
    main()
