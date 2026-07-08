# MetaAgent
MetaAgent is a desktop canvas (PySide6) where you design AI agent workflows as a node-and-edge graph, then generate a standalone, zero-dependency Python program from it
— no LangChain, no framework lock-in. Drag in agents, LLMs, tools, RAG knowledge bases, memory, skills, and human-in-the-loop checkpoints; wire them into proven patterns — chain, router, supervisor, orchestrator, fan-out/join, map-reduce, or voting — and click Generate.

It is provider-neutral (DeepSeek, OpenAI, Anthropic, and any OpenAI-compatible endpoint) with automatic LLM fallback chains, typed shared state with custom nested types, multi-engine web search, scheduled "ambient" agents, and multi-user web servers. Two built-in AI assistants work alongside you: a Tool Generator that writes Python tools on request, and a Designer Agent that turns plain-language requirements into a working graph and renders it on the canvas.

Debug runs replay live on the canvas, graphs save as portable .mta bundles, and any agent — or the designer itself — compiles to a single Windows .exe. The interface supports English and Simplified Chinese.