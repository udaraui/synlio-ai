"""
Synlio Co-Pilot Unified Architecture Visualization Generator

Generates a single comprehensive LangGraph architecture diagram (graph.png & graph_mermaid.md)
expanding both subgraphs (Automation and Analytics) into one unified flowchart.

Usage:
    .\\venv\\Scripts\\python scripts\\visualize\\visualize_graph.py
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
from app.agent.master_graph import create_master_graph


def main():
    print("=" * 80)
    print("Synlio Co-Pilot Unified Architecture Visualization")
    print("=" * 80)

    api_key = os.getenv("GAMMA_API_KEY") or os.getenv("JEV_API_KEY") or "dummy_key"
    dummy_llm = ChatOpenAI(model="gemini-2.0-flash-001", api_key=api_key)

    print("\nCompiling Master Graph with expanded subgraphs (Automation + Analytics)...")
    compiled = create_master_graph(dummy_llm, orchestrator_llm=dummy_llm, formatter_llm=dummy_llm)
    
    # xray=True recursively expands all subgraphs into one unified diagram
    unified_graph = compiled.get_graph(xray=True)
    mermaid_code = unified_graph.draw_mermaid()

    png_path = SCRIPT_DIR / "graph.png"
    md_path = SCRIPT_DIR / "graph_mermaid.md"

    # 1. Export unified PNG diagram
    try:
        png_bytes = unified_graph.draw_mermaid_png()
        with open(png_path, "wb") as f:
            f.write(png_bytes)
        print(f" [PNG] Successfully generated unified diagram: '{png_path.name}' ({len(png_bytes):,} bytes)")
    except Exception as e:
        print(f" [PNG] Could not generate {png_path.name} (network error): {e}")

    # 2. Export unified Mermaid Markdown
    try:
        md_content = f"""# Synlio Co-Pilot Architecture

Unified architecture diagram for the Synlio Co-Pilot AI engine.
Shows end-to-end request flow through GateKeeper, Intent Dispatcher, Automation Subgraph, and Analytics Subgraph.

![Architecture Graph](./graph.png)

```mermaid
{mermaid_code}
```
"""
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f" [MD]  Successfully generated markdown: '{md_path.name}'")
    except Exception as e:
        print(f" [MD]  Could not generate {md_path.name}: {e}")

    print("\n" + "=" * 80)
    print("Unified graph generated successfully in scripts/visualize/!")
    print("=" * 80)


if __name__ == "__main__":
    main()
